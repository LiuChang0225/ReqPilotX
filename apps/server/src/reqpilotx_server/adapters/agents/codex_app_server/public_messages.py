"""Materialized public replies for reconnect; never consumes reasoning items."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

PUBLIC_PHASES = {"commentary", "final", "final_answer", "reply"}

class PublicMessageStore:
    def __init__(self, path: Path):
        self.path = path
        self.messages = read_public_messages(path)
        self.last_write = 0.0

    def observe(self, event):
        kind = event.get("type")
        changed = False
        if kind in {"agent_message_start", "agent_message_delta", "agent_message"} and event.get("phase") in PUBLIC_PHASES:
            message_id = event.get("message_id")
            if not message_id:
                return
            key = (event.get("attempt_id"), event.get("thread_id"), event.get("turn_id"), message_id)
            previous = next((message for message in self.messages if (message.get("attempt_id"), message.get("thread_id"), message.get("turn_id"), message.get("message_id")) == key), None)
            if previous is None:
                previous = {"message_id": message_id, "attempt_id": event.get("attempt_id"), "thread_id": event.get("thread_id"), "turn_id": event.get("turn_id"), "version": -1, "text": ""}
                self.messages.append(previous)
            version = event.get("version", 0)
            if version <= previous["version"]:
                return
            if kind == "agent_message_delta":
                if version != previous["version"] + 1:
                    return
                previous["text"] += str(event.get("delta") or "")
            else:
                previous["text"] = str(event.get("text") or "")
            previous.update(version=version, phase=event["phase"], status="complete" if kind == "agent_message" else "streaming", time_unix=event.get("time_unix"))
            self.messages = self.messages[-6:]
            changed = True
        elif kind in {"turn/completed", "runtime_error"}:
            for message in self.messages:
                if message.get("status") == "streaming" and (kind == "runtime_error" or message.get("turn_id") == event.get("turn_id")):
                    message["status"] = "interrupted"
                    changed = True
        elif kind == "turn/started":
            for message in self.messages:
                if message.get("status") == "streaming" and message.get("turn_id") != event.get("turn_id"):
                    message["status"] = "interrupted"
                    changed = True
        if changed and (kind != "agent_message_delta" or time.monotonic() - self.last_write >= 0.25):
            self.last_write = time.monotonic()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".public-messages-", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump({"schema_version": "public-agent-messages/v1", "messages": self.messages}, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)


def read_public_messages(path: Path):
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # JSONL remains the replay authority if a materialized view is unavailable.
    if not isinstance(value, dict) or value.get("schema_version") != "public-agent-messages/v1":
        return []
    return [message for message in value.get("messages", []) if isinstance(message, dict) and message.get("phase") in PUBLIC_PHASES][-6:]
