"""Fail-closed B1-C1 lifecycle canary for the local Codex App Server.

The canary accepts no document or user prompt input. It runs one fixed synthetic
structured-output turn from a private, repository-external ``CODEX_HOME`` and
emits only content-free protocol receipts.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from reqpilotx_server.adapters.agents.codex_app_server.probe import (
    DEFAULT_CODEX_BIN,
    DEFAULT_PREFERRED_MODEL,
    build_command,
    safe_account_summary,
    safe_model_summary,
)


LIFECYCLE_PROBE_SCHEMA = "reqpilotx-codex-lifecycle-probe/v2"
PROTOCOL_LOCK_SCHEMA = "reqpilotx-codex-protocol-lock/v1"
PROTOCOL_LOCK_PATH = Path(__file__).with_name("protocol_lock.json")
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_OUTBOUND_JSONL_BYTES = 1_048_576
MAX_INBOUND_JSONL_BYTES = 33_554_432
SYNTHETIC_MARKER = "B1_C1_NORMAL"
INTERRUPT_MARKER = "B1_C1_INTERRUPT"
RESUME_MARKER = "B1_C1_RESUMED"
CRASH_MARKER = "B1_C1_CRASH"
PDF_SKILL_MARKER = "B1_C2_PDF_SKILL"
_SYNTHETIC_MARKERS = (
    SYNTHETIC_MARKER,
    INTERRUPT_MARKER,
    RESUME_MARKER,
    CRASH_MARKER,
    PDF_SKILL_MARKER,
)
_CLIENT_NAME = "reqpilotx_lifecycle_canary"
_SERVICE_NAME = "reqpilotx_b1_lifecycle_canary"
_SYNTHETIC_DEVELOPER_INSTRUCTIONS = (
    "Synthetic protocol canary. Do not inspect files, call tools, or use external "
    "data. Return only the requested output."
)

_OUTBOUND_REQUEST_METHODS = frozenset(
    {
        "account/read",
        "command/exec",
        "config/read",
        "initialize",
        "mcpServerStatus/list",
        "model/list",
        "skills/list",
        "thread/delete",
        "thread/list",
        "thread/read",
        "thread/resume",
        "thread/start",
        "thread/turns/list",
        "turn/interrupt",
        "turn/steer",
        "turn/start",
    }
)
_OUTBOUND_NOTIFICATION_METHODS = frozenset({"initialized"})
_ALLOWED_NOTIFICATION_METHODS = frozenset(
    {
        "account/rateLimits/updated",
        "account/updated",
        "configWarning",
        "deprecationNotice",
        "error",
        "item/agentMessage/delta",
        "item/completed",
        "item/plan/delta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "item/started",
        "model/rerouted",
        "model/safetyBuffering/updated",
        "model/verification",
        "remoteControl/status/changed",
        "serverRequest/resolved",
        "thread/closed",
        "thread/deleted",
        "thread/name/updated",
        "thread/settings/updated",
        "thread/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "turn/completed",
        "turn/moderationMetadata",
        "turn/plan/updated",
        "turn/started",
        "warning",
    }
)
_ALLOWED_ITEM_TYPES = frozenset(
    {
        "agentMessage",
        "reasoning",
        "userMessage",
    }
)
_FORBIDDEN_ITEM_TYPES = frozenset(
    {
        "collabAgentToolCall",
        "collabToolCall",
        "commandExecution",
        "dynamicToolCall",
        "enteredReviewMode",
        "exitedReviewMode",
        "fileChange",
        "hookPrompt",
        "imageGeneration",
        "imageView",
        "mcpToolCall",
        "sleep",
        "subAgentActivity",
        "webSearch",
    }
)
_FAIL_NOTIFICATION_REASON_CODES = {
    "configWarning": "CONFIG_WARNING",
    "item/agentMessage/delta": "CONTENT_DELTA_NOT_OPTOUT",
    "item/plan/delta": "PLAN_EVENT_FORBIDDEN",
    "item/reasoning/summaryTextDelta": "CONTENT_DELTA_NOT_OPTOUT",
    "item/reasoning/textDelta": "CONTENT_DELTA_NOT_OPTOUT",
    "model/rerouted": "MODEL_REROUTED",
    "model/verification": "MODEL_VERIFICATION_REQUIRED",
    "turn/plan/updated": "PLAN_EVENT_FORBIDDEN",
}
_REQUIRED_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "code_mode",
    "computer_use",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_suggest",
    "unified_exec",
    "view_image",
    "workspace_dependencies",
)
_LIFECYCLE_HARDENING_OVERRIDES = (
    "analytics.enabled=false",
    "check_for_update_on_startup=false",
    "features.apps=false",
    "features.browser_use=false",
    "features.code_mode=false",
    "features.computer_use=false",
    "features.goals=false",
    "features.hooks=false",
    "features.image_generation=false",
    "features.in_app_browser=false",
    "features.memories=false",
    "features.multi_agent=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.shell_snapshot=false",
    "features.skill_search=false",
    "features.tool_suggest=false",
    "features.view_image=false",
    "features.workspace_dependencies=false",
    'forced_login_method="chatgpt"',
    "mcp_servers={}",
    "hooks={}",
    "plugins={}",
    "skills.config=[]",
    'otel.exporter="none"',
    "otel.log_user_prompt=false",
    'otel.metrics_exporter="none"',
    'otel.trace_exporter="none"',
    "tools.experimental_request_user_input.enabled=false",
    "tools.update_plan.enabled=false",
)


class LifecycleProbeError(RuntimeError):
    """A content-free lifecycle failure exposed only as a reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class CanaryRuntimeState:
    codex_home: Path
    command: tuple[str, ...]
    environment: Mapping[str, str]
    log_dir: Path
    protocol: Mapping[str, str]
    sqlite_home: Path
    state_path: Path
    sterile_cwd: Path
    tmp_dir: Path


@dataclass
class EventLedger:
    """Validate lifecycle identities while retaining no streamed content."""

    method_counts: Counter[str] = field(default_factory=Counter)
    item_type_counts: Counter[str] = field(default_factory=Counter)
    observed_thread_ids: set[str] = field(default_factory=set)
    observed_turn_ids: set[str] = field(default_factory=set)
    deleted_thread_ids: set[str] = field(default_factory=set)
    terminal_turns: dict[str, str] = field(default_factory=dict)
    started_item_ids: set[str] = field(default_factory=set)
    completed_item_ids: set[str] = field(default_factory=set)
    terminal_orphaned_item_ids: set[str] = field(default_factory=set)
    item_types_by_id: dict[str, str] = field(default_factory=dict)
    final_agent_message: str | None = None
    expected_thread_id: str | None = None
    expected_turn_id: str | None = None

    def bind_thread(self, thread_id: str) -> None:
        if not _nonempty_string(thread_id):
            raise LifecycleProbeError("THREAD_ID_INVALID")
        self.expected_thread_id = thread_id
        if self.observed_thread_ids - {thread_id}:
            raise LifecycleProbeError("EVENT_THREAD_ID_MISMATCH")

    def bind_turn(self, turn_id: str) -> None:
        if not _nonempty_string(turn_id):
            raise LifecycleProbeError("TURN_ID_INVALID")
        self.expected_turn_id = turn_id
        if self.observed_turn_ids - {turn_id}:
            raise LifecycleProbeError("EVENT_TURN_ID_MISMATCH")

    def observe(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str) or method not in _ALLOWED_NOTIFICATION_METHODS:
            raise LifecycleProbeError("UNEXPECTED_NOTIFICATION_METHOD")
        params = message.get("params")
        if not isinstance(params, Mapping):
            raise LifecycleProbeError("NOTIFICATION_PARAMS_INVALID")
        self.method_counts[method] += 1
        thread_id = _notification_thread_id(params)
        turn_id = _notification_turn_id(params)
        if thread_id is not None:
            self.observed_thread_ids.add(thread_id)
            if (
                self.expected_thread_id is not None
                and thread_id != self.expected_thread_id
            ):
                raise LifecycleProbeError("EVENT_THREAD_ID_MISMATCH")
        if turn_id is not None:
            self.observed_turn_ids.add(turn_id)
            if self.expected_turn_id is not None and turn_id != self.expected_turn_id:
                raise LifecycleProbeError("EVENT_TURN_ID_MISMATCH")
        failure_reason = _FAIL_NOTIFICATION_REASON_CODES.get(method)
        if failure_reason is not None:
            raise LifecycleProbeError(failure_reason)
        if method == "error":
            will_retry = params.get("willRetry")
            if type(will_retry) is not bool:
                raise LifecycleProbeError("ERROR_NOTIFICATION_INVALID")
            if not will_retry:
                raise LifecycleProbeError(_codex_error_reason(params))
        if method in {"item/started", "item/completed"}:
            self._observe_item(method, params)
        elif method == "turn/completed":
            self._observe_terminal(params)
        elif method == "thread/deleted" and thread_id is not None:
            self.deleted_thread_ids.add(thread_id)
        elif method == "turn/started":
            turn = params.get("turn")
            if not isinstance(turn, Mapping) or turn.get("status") != "inProgress":
                raise LifecycleProbeError("TURN_STARTED_INVALID")
        elif method == "thread/status/changed":
            self._observe_thread_status(params)

    def _observe_item(self, method: str, params: Mapping[str, Any]) -> None:
        if (
            _notification_thread_id(params) is None
            or _notification_turn_id(params) is None
        ):
            raise LifecycleProbeError("ITEM_EVENT_IDENTITY_MISSING")
        item = params.get("item")
        if not isinstance(item, Mapping):
            raise LifecycleProbeError("ITEM_EVENT_INVALID")
        item_type = item.get("type")
        item_id = item.get("id")
        if not isinstance(item_type, str) or not _nonempty_string(item_id):
            raise LifecycleProbeError("ITEM_TYPE_INVALID")
        self.item_type_counts[item_type] += 1
        if item_type in _FORBIDDEN_ITEM_TYPES:
            raise LifecycleProbeError("SECURITY_POLICY_VIOLATION")
        if item_type not in _ALLOWED_ITEM_TYPES:
            raise LifecycleProbeError("UNEXPECTED_ITEM_TYPE")
        item_id = str(item_id)
        known_type = self.item_types_by_id.setdefault(item_id, item_type)
        if known_type != item_type:
            raise LifecycleProbeError("ITEM_TYPE_DRIFT")
        if method == "item/started":
            if item_id in self.started_item_ids:
                raise LifecycleProbeError("ITEM_STARTED_DUPLICATE")
            self.started_item_ids.add(item_id)
        else:
            if item_id not in self.started_item_ids:
                raise LifecycleProbeError("ITEM_COMPLETED_WITHOUT_START")
            if item_id in self.completed_item_ids:
                raise LifecycleProbeError("ITEM_COMPLETED_DUPLICATE")
            self.completed_item_ids.add(item_id)
        if method == "item/completed" and item_type == "agentMessage":
            text = item.get("text")
            if not isinstance(text, str):
                raise LifecycleProbeError("AGENT_MESSAGE_INVALID")
            self.final_agent_message = text

    def _observe_terminal(self, params: Mapping[str, Any]) -> None:
        if _notification_thread_id(params) is None:
            raise LifecycleProbeError("TURN_COMPLETED_IDENTITY_MISSING")
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            raise LifecycleProbeError("TURN_COMPLETED_INVALID")
        turn_id = turn.get("id")
        status = turn.get("status")
        if not _nonempty_string(turn_id) or status not in {
            "completed",
            "failed",
            "interrupted",
        }:
            raise LifecycleProbeError("TURN_TERMINAL_INVALID")
        if turn_id in self.terminal_turns:
            raise LifecycleProbeError("TURN_TERMINAL_DUPLICATE")
        self.terminal_turns[turn_id] = str(status)

    def _observe_thread_status(self, params: Mapping[str, Any]) -> None:
        status = params.get("status")
        if not isinstance(status, Mapping):
            raise LifecycleProbeError("THREAD_STATUS_INVALID")
        status_type = status.get("type")
        if status_type == "systemError":
            raise LifecycleProbeError("THREAD_SYSTEM_ERROR")
        if status_type == "active":
            flags = status.get("activeFlags")
            if not isinstance(flags, list) or any(
                flag in {"waitingOnApproval", "waitingOnUserInput"} for flag in flags
            ):
                raise LifecycleProbeError("THREAD_WAITING_FOR_INPUT")
        elif status_type not in {"idle", "notLoaded"}:
            raise LifecycleProbeError("THREAD_STATUS_INVALID")

    def content_free_summary(self) -> dict[str, Any]:
        unique_item_counts = Counter(self.item_types_by_id.values())
        orphaned_item_counts = Counter(
            self.item_types_by_id.get(item_id, "unknown")
            for item_id in self.terminal_orphaned_item_ids
        )
        return {
            "event_counts": dict(sorted(self.method_counts.items())),
            "item_event_counts": dict(sorted(self.item_type_counts.items())),
            "unique_item_counts": dict(sorted(unique_item_counts.items())),
            "terminal_orphaned_item_type_counts": dict(
                sorted(orphaned_item_counts.items())
            ),
        }

    def validate_normal_sequence(
        self,
        *,
        thread_id: str,
        turn_id: str,
        require_thread_started: bool = True,
        require_thread_deleted: bool = True,
    ) -> None:
        required_methods = {
            "turn/started",
            "turn/completed",
        }
        if require_thread_started:
            required_methods.add("thread/started")
        if require_thread_deleted:
            required_methods.add("thread/deleted")
        for method in sorted(required_methods):
            if self.method_counts[method] != 1:
                raise LifecycleProbeError(
                    f"LIFECYCLE_EVENT_{_method_code(method)}_COUNT_"
                    f"{self.method_counts[method]}"
                )
        if self.expected_thread_id != thread_id or self.expected_turn_id != turn_id:
            raise LifecycleProbeError("LIFECYCLE_IDENTITY_NOT_BOUND")
        if self.terminal_turns.get(turn_id) != "completed":
            raise LifecycleProbeError("TURN_NOT_COMPLETED")
        if require_thread_deleted and thread_id not in self.deleted_thread_ids:
            raise LifecycleProbeError("THREAD_DELETE_EVENT_MISSING")
        if not require_thread_deleted and thread_id in self.deleted_thread_ids:
            raise LifecycleProbeError("THREAD_DELETED_DURING_PERSISTENT_SESSION")
        if self.item_type_counts["agentMessage"] < 1:
            raise LifecycleProbeError("AGENT_MESSAGE_EVENT_MISSING")
        if self.item_type_counts["userMessage"] < 1:
            raise LifecycleProbeError("USER_MESSAGE_EVENT_MISSING")
        if self.method_counts["item/completed"] < 2:
            raise LifecycleProbeError("ITEM_COMPLETED_EVENT_MISSING")
        if not self.terminal_orphaned_item_ids.issubset(
            self.started_item_ids
        ) or self.terminal_orphaned_item_ids.intersection(self.completed_item_ids):
            raise LifecycleProbeError("TERMINAL_ORPHANED_ITEM_SET_INVALID")
        if self.started_item_ids != (
            self.completed_item_ids | self.terminal_orphaned_item_ids
        ):
            raise LifecycleProbeError("ITEM_LIFECYCLE_INCOMPLETE")

    def validate_interrupted_sequence(self, *, thread_id: str, turn_id: str) -> None:
        required_methods = {"thread/started", "turn/started", "turn/completed"}
        for method in sorted(required_methods):
            if self.method_counts[method] != 1:
                raise LifecycleProbeError(
                    f"LIFECYCLE_EVENT_{_method_code(method)}_COUNT_"
                    f"{self.method_counts[method]}"
                )
        if self.expected_thread_id != thread_id or self.expected_turn_id != turn_id:
            raise LifecycleProbeError("LIFECYCLE_IDENTITY_NOT_BOUND")
        if self.terminal_turns.get(turn_id) != "interrupted":
            raise LifecycleProbeError("INTERRUPT_TERMINAL_INVALID")
        if not self.completed_item_ids.issubset(self.started_item_ids):
            raise LifecycleProbeError("ITEM_COMPLETED_WITHOUT_START")


class JsonlRpcSession:
    """Single-reader JSONL session with a strict outbound method allowlist."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
        ledger: EventLedger,
        experimental_api: bool = False,
        server_request_handler: Callable[[Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.ledger = ledger
        self._next_request_id = 1
        self._read_buffer = bytearray()
        self._crash_requested = False
        self._experimental_api = experimental_api
        self._server_request_handler = server_request_handler
        self.process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            cwd=str(cwd),
            env=dict(environment),
            start_new_session=True,
            bufsize=0,
        )
        if self.process.stdin is None or self.process.stdout is None:
            self.close(force=True)
            raise LifecycleProbeError("APP_SERVER_STDIO_UNAVAILABLE")
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ)

    def initialize(self) -> Mapping[str, Any]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": _CLIENT_NAME,
                    "title": "ReqPilotX Lifecycle Canary",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": self._experimental_api,
                    "optOutNotificationMethods": [
                        "item/agentMessage/delta",
                        "item/reasoning/summaryTextDelta",
                        "item/reasoning/textDelta",
                    ],
                },
            },
        )
        self.notify("initialized")
        return result

    def configure_dynamic_server_requests(
        self,
        handler: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        if self._server_request_handler is not None:
            raise LifecycleProbeError("SERVER_REQUEST_HANDLER_ALREADY_CONFIGURED")
        self._experimental_api = True
        self._server_request_handler = handler

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method not in _OUTBOUND_REQUEST_METHODS:
            raise LifecycleProbeError("OUTBOUND_METHOD_FORBIDDEN")
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write({"method": method, "id": request_id, "params": dict(params)})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._read(deadline)
            if "method" in message and "id" in message:
                self._handle_server_request(message)
                continue
            if "id" in message:
                if message.get("id") != request_id:
                    raise LifecycleProbeError("RESPONSE_ID_MISMATCH")
                error = message.get("error")
                if error is not None:
                    if (
                        method == "turn/interrupt"
                        and self.ledger.expected_turn_id is not None
                        and self.ledger.expected_turn_id in self.ledger.terminal_turns
                    ):
                        raise LifecycleProbeError("INTERRUPT_RACE")
                    error_code = (
                        error.get("code") if isinstance(error, Mapping) else None
                    )
                    code_suffix = (
                        f"_CODE_{str(error_code).replace('-', 'NEG_')}"
                        if isinstance(error_code, int)
                        else ""
                    )
                    raise LifecycleProbeError(
                        f"RPC_{_method_code(method)}_REJECTED{code_suffix}"
                        f"{_safe_rpc_error_hint(error)}"
                    )
                result = message.get("result")
                if not isinstance(result, Mapping):
                    raise LifecycleProbeError("RPC_RESULT_INVALID")
                return result
            self.ledger.observe(message)

    def expect_rejection(self, method: str, params: Mapping[str, Any]) -> None:
        if method not in _OUTBOUND_REQUEST_METHODS:
            raise LifecycleProbeError("OUTBOUND_METHOD_FORBIDDEN")
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write({"method": method, "id": request_id, "params": dict(params)})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._read(deadline)
            if "method" in message and "id" in message:
                self._handle_server_request(message)
                continue
            if "id" in message:
                if message.get("id") != request_id:
                    raise LifecycleProbeError("RESPONSE_ID_MISMATCH")
                if message.get("error") is None:
                    raise LifecycleProbeError("EXPECTED_RPC_REJECTION_MISSING")
                return
            self.ledger.observe(message)

    def notify(self, method: str) -> None:
        if method not in _OUTBOUND_NOTIFICATION_METHODS:
            raise LifecycleProbeError("OUTBOUND_NOTIFICATION_FORBIDDEN")
        self._write({"method": method})

    def wait_for_turn(
        self,
        turn_id: str,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        cancellation_poll_seconds: float = 0.25,
        steer_requested: Callable[[], tuple[Any, str] | None] | None = None,
        steer_delivered: Callable[[Any], None] | None = None,
        steer_failed: Callable[[Any, str], None] | None = None,
    ) -> str:
        existing = self.ledger.terminal_turns.get(turn_id)
        if existing is not None:
            return existing
        deadline = time.monotonic() + self.timeout_seconds
        while turn_id not in self.ledger.terminal_turns:
            if cancellation_requested is not None and cancellation_requested():
                try:
                    self.request(
                        "turn/interrupt",
                        {
                            "threadId": self.ledger.expected_thread_id,
                            "turnId": turn_id,
                        },
                    )
                except LifecycleProbeError as exc:
                    if exc.reason_code != "INTERRUPT_RACE":
                        raise
                cancellation_requested = None
                deadline = max(
                    deadline,
                    time.monotonic() + min(self.timeout_seconds, 10.0),
                )
                continue
            if steer_requested is not None:
                pending_steer = steer_requested()
                if pending_steer is not None:
                    command, steer_text = pending_steer
                    try:
                        result = self.request(
                            "turn/steer",
                            {
                                "threadId": self.ledger.expected_thread_id,
                                "input": [{"type": "text", "text": steer_text}],
                                "expectedTurnId": turn_id,
                            },
                        )
                        if result.get("turnId") != turn_id:
                            raise LifecycleProbeError("STEER_TURN_ID_MISMATCH")
                    except LifecycleProbeError as exc:
                        if steer_failed is not None:
                            steer_failed(command, exc.reason_code)
                        raise
                    if steer_delivered is None:
                        raise LifecycleProbeError("STEER_ACKNOWLEDGER_MISSING")
                    steer_delivered(command)
                    deadline = max(
                        deadline,
                        time.monotonic() + min(self.timeout_seconds, 10.0),
                    )
                    continue
            read_deadline = deadline
            if cancellation_requested is not None or steer_requested is not None:
                read_deadline = min(
                    deadline,
                    time.monotonic() + cancellation_poll_seconds,
                )
            try:
                message = self._read(read_deadline)
            except LifecycleProbeError as exc:
                if (
                    exc.reason_code == "APP_SERVER_TIMEOUT"
                    and (
                        cancellation_requested is not None
                        or steer_requested is not None
                    )
                    and time.monotonic() < deadline
                ):
                    continue
                raise
            if "id" in message:
                if "method" in message:
                    self._handle_server_request(message)
                    continue
                raise LifecycleProbeError("UNEXPECTED_RESPONSE")
            self.ledger.observe(message)
        return self.ledger.terminal_turns[turn_id]

    def wait_for_turn_started(self, turn_id: str) -> None:
        if self.ledger.method_counts["turn/started"] == 1:
            if turn_id not in self.ledger.observed_turn_ids:
                raise LifecycleProbeError("EVENT_TURN_ID_MISMATCH")
            return
        deadline = time.monotonic() + self.timeout_seconds
        while self.ledger.method_counts["turn/started"] < 1:
            if turn_id in self.ledger.terminal_turns:
                raise LifecycleProbeError("INTERRUPT_RACE")
            message = self._read(deadline)
            if "id" in message:
                if "method" in message:
                    self._handle_server_request(message)
                    continue
                raise LifecycleProbeError("UNEXPECTED_RESPONSE")
            self.ledger.observe(message)
        if self.ledger.method_counts["turn/started"] != 1:
            raise LifecycleProbeError("TURN_STARTED_DUPLICATE")

    def wait_for_thread_deleted(self, thread_id: str) -> None:
        if thread_id in self.ledger.deleted_thread_ids:
            return
        deadline = time.monotonic() + self.timeout_seconds
        while thread_id not in self.ledger.deleted_thread_ids:
            message = self._read(deadline)
            if "id" in message:
                if "method" in message:
                    self._handle_server_request(message)
                    continue
                raise LifecycleProbeError("UNEXPECTED_RESPONSE")
            self.ledger.observe(message)

    def drain_notifications_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout_seconds: float,
    ) -> None:
        if predicate():
            return
        deadline = time.monotonic() + timeout_seconds
        while not predicate():
            message = self._read(deadline)
            if "method" in message and "id" in message:
                self._handle_server_request(message)
                continue
            if "id" in message:
                raise LifecycleProbeError("UNEXPECTED_RESPONSE")
            self.ledger.observe(message)

    def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if (
            self._server_request_handler is None
            or not self._experimental_api
            or method != "item/tool/call"
            or not isinstance(request_id, (int, str))
        ):
            raise LifecycleProbeError("SERVER_REQUEST_FORBIDDEN")
        try:
            result = self._server_request_handler(message)
            if not isinstance(result, Mapping):
                raise LifecycleProbeError("SERVER_REQUEST_RESULT_INVALID")
        except BaseException:
            self._write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": "dynamic tool request rejected",
                    },
                }
            )
            raise
        self._write({"id": request_id, "result": dict(result)})

    def stop_gracefully(self, *, timeout_seconds: float = 3.0) -> None:
        if self.process.poll() is not None:
            if self.process.returncode != 0:
                raise LifecycleProbeError("GRACEFUL_STOP_FAILED")
            return
        if self.process.stdin is None or self.process.stdin.closed:
            raise LifecycleProbeError("GRACEFUL_STOP_FAILED")
        self.process.stdin.close()
        try:
            return_code = self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self.close(force=True)
            raise LifecycleProbeError("GRACEFUL_STOP_FAILED") from exc
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()
        if return_code != 0:
            raise LifecycleProbeError("GRACEFUL_STOP_FAILED")

    def kill_and_confirm_unknown(self, turn_id: str) -> None:
        if (
            self.ledger.expected_turn_id != turn_id
            or turn_id in self.ledger.terminal_turns
            or self.process.poll() is not None
        ):
            raise LifecycleProbeError("CRASH_PRECONDITION_INVALID")
        self._crash_requested = True
        _signal_process_group(self.process, signal.SIGKILL)
        try:
            return_code = self.process.wait(timeout=3)
        except subprocess.TimeoutExpired as exc:
            raise LifecycleProbeError("CRASH_KILL_FAILED") from exc
        if return_code != -signal.SIGKILL:
            raise LifecycleProbeError("CRASH_EXIT_STATUS_INVALID")
        deadline = time.monotonic() + 3.0
        while True:
            try:
                message = self._read(deadline)
            except LifecycleProbeError as exc:
                if exc.reason_code == "UNKNOWN_AFTER_CRASH":
                    return
                raise
            if "id" in message:
                raise LifecycleProbeError("CRASH_UNEXPECTED_RESPONSE")
            self.ledger.observe(message)
            if turn_id in self.ledger.terminal_turns:
                raise LifecycleProbeError("CRASH_RACE_LOST")

    def close(self, *, force: bool = False) -> None:
        selector = getattr(self, "_selector", None)
        if selector is not None:
            selector.close()
        process = getattr(self, "process", None)
        if process is None:
            return
        if process.poll() is None:
            first_signal = signal.SIGKILL if force else signal.SIGTERM
            _signal_process_group(process, first_signal)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _signal_process_group(process, signal.SIGKILL)
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()

    def _write(self, message: Mapping[str, Any]) -> None:
        if self.process.stdin is None:
            raise LifecycleProbeError("APP_SERVER_STDIN_CLOSED")
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_OUTBOUND_JSONL_BYTES:
            raise LifecycleProbeError("OUTBOUND_MESSAGE_TOO_LARGE")
        try:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
        except BrokenPipeError as exc:
            raise LifecycleProbeError("APP_SERVER_BROKEN_PIPE") from exc

    def _read(self, deadline: float) -> Mapping[str, Any]:
        if self.process.stdout is None:
            raise LifecycleProbeError("APP_SERVER_STDOUT_CLOSED")
        while b"\n" not in self._read_buffer:
            if len(self._read_buffer) > MAX_INBOUND_JSONL_BYTES:
                raise LifecycleProbeError("INBOUND_MESSAGE_TOO_LARGE")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise LifecycleProbeError("APP_SERVER_TIMEOUT")
            chunk = os.read(self.process.stdout.fileno(), 65_536)
            if not chunk:
                if self._read_buffer:
                    if self._crash_requested:
                        self._read_buffer.clear()
                        raise LifecycleProbeError("UNKNOWN_AFTER_CRASH")
                    raise LifecycleProbeError("INBOUND_LINE_INCOMPLETE")
                raise LifecycleProbeError(
                    "UNKNOWN_AFTER_CRASH" if self._crash_requested else "APP_SERVER_EOF"
                )
            self._read_buffer.extend(chunk)
        newline_index = self._read_buffer.index(b"\n")
        if newline_index + 1 > MAX_INBOUND_JSONL_BYTES:
            raise LifecycleProbeError("INBOUND_MESSAGE_TOO_LARGE")
        raw = bytes(self._read_buffer[:newline_index])
        del self._read_buffer[: newline_index + 1]
        try:
            message = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LifecycleProbeError("INBOUND_JSON_INVALID") from exc
        if not isinstance(message, Mapping):
            raise LifecycleProbeError("INBOUND_MESSAGE_INVALID")
        return message


def load_protocol_lock(path: Path = PROTOCOL_LOCK_PATH) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("PROTOCOL_LOCK_UNREADABLE") from exc
    required = {
        "binary_sha256",
        "codex_version",
        "experimental_protocol_schema_sha256",
        "experimental_protocol_v2_schema_sha256",
        "protocol_schema_sha256",
        "protocol_v2_schema_sha256",
        "schema",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise LifecycleProbeError("PROTOCOL_LOCK_INVALID")
    if payload.get("schema") != PROTOCOL_LOCK_SCHEMA or any(
        not _nonempty_string(payload.get(key)) for key in required
    ):
        raise LifecycleProbeError("PROTOCOL_LOCK_INVALID")
    return {str(key): str(value) for key, value in payload.items()}


def verify_protocol_lock(
    codex_bin: Path,
    *,
    lock: Mapping[str, str],
    scratch_dir: Path,
) -> dict[str, str]:
    verify_protocol_binary_identity(codex_bin, lock=lock)
    schema_dir = scratch_dir / "protocol-schema"
    schema_dir.mkdir(mode=0o700)
    generated = subprocess.run(
        [
            str(codex_bin),
            "app-server",
            "generate-json-schema",
            "--out",
            str(schema_dir),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if generated.returncode != 0:
        raise LifecycleProbeError("PROTOCOL_SCHEMA_GENERATION_FAILED")
    schema_hash = _sha256_file(schema_dir / "codex_app_server_protocol.schemas.json")
    v2_hash = _sha256_file(schema_dir / "codex_app_server_protocol.v2.schemas.json")
    if schema_hash != lock.get("protocol_schema_sha256"):
        raise LifecycleProbeError("PROTOCOL_SCHEMA_HASH_MISMATCH")
    if v2_hash != lock.get("protocol_v2_schema_sha256"):
        raise LifecycleProbeError("PROTOCOL_V2_SCHEMA_HASH_MISMATCH")
    experimental_schema_dir = scratch_dir / "experimental-protocol-schema"
    experimental_schema_dir.mkdir(mode=0o700)
    experimental_generated = subprocess.run(
        [
            str(codex_bin),
            "app-server",
            "generate-json-schema",
            "--experimental",
            "--out",
            str(experimental_schema_dir),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if experimental_generated.returncode != 0:
        raise LifecycleProbeError("EXPERIMENTAL_PROTOCOL_SCHEMA_GENERATION_FAILED")
    experimental_schema_hash = _sha256_file(
        experimental_schema_dir / "codex_app_server_protocol.schemas.json"
    )
    experimental_v2_hash = _sha256_file(
        experimental_schema_dir / "codex_app_server_protocol.v2.schemas.json"
    )
    if experimental_schema_hash != lock.get("experimental_protocol_schema_sha256"):
        raise LifecycleProbeError("EXPERIMENTAL_PROTOCOL_SCHEMA_HASH_MISMATCH")
    if experimental_v2_hash != lock.get("experimental_protocol_v2_schema_sha256"):
        raise LifecycleProbeError("EXPERIMENTAL_PROTOCOL_V2_SCHEMA_HASH_MISMATCH")
    restricted_read_access = _schema_supports_restricted_read_access(
        schema_dir / "codex_app_server_protocol.schemas.json"
    )
    permission_profiles = _schema_supports_permission_profiles(
        schema_dir / "codex_app_server_protocol.schemas.json"
    )
    dynamic_tools = _schema_supports_dynamic_tools(
        experimental_schema_dir / "codex_app_server_protocol.schemas.json"
    )
    return {
        "binary_sha256": str(lock["binary_sha256"]),
        "codex_version": str(lock["codex_version"]),
        "protocol_schema_sha256": schema_hash,
        "protocol_v2_schema_sha256": v2_hash,
        "experimental_protocol_schema_sha256": experimental_schema_hash,
        "experimental_protocol_v2_schema_sha256": experimental_v2_hash,
        "restricted_read_access": ("true" if restricted_read_access else "false"),
        "permission_profiles": "true" if permission_profiles else "false",
        "dynamic_tools": "true" if dynamic_tools else "false",
    }


def verify_protocol_binary_identity(
    codex_bin: Path,
    *,
    lock: Mapping[str, str],
) -> None:
    """Fail before queueing work when the installed Codex build drifted."""

    if not codex_bin.is_absolute() or not codex_bin.is_file() or codex_bin.is_symlink():
        raise LifecycleProbeError("CODEX_BINARY_INVALID")
    if _sha256_file(codex_bin) != lock.get("binary_sha256"):
        raise LifecycleProbeError("CODEX_BINARY_HASH_MISMATCH")
    version = subprocess.run(
        [str(codex_bin), "--version"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    if version.returncode != 0 or version.stdout.strip() != lock.get("codex_version"):
        raise LifecycleProbeError("CODEX_VERSION_MISMATCH")


def _schema_supports_dynamic_tools(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError(
            "EXPERIMENTAL_PROTOCOL_SCHEMA_GENERATION_FAILED"
        ) from exc
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return all(
        marker in serialized
        for marker in (
            '"dynamicTools"',
            '"dynamicToolCall"',
            '"item/tool/call"',
            '"DynamicToolCallResponse"',
        )
    )


def _schema_supports_restricted_read_access(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("PROTOCOL_SCHEMA_GENERATION_FAILED") from exc

    def visit(value: object) -> bool:
        if isinstance(value, Mapping):
            properties = value.get("properties")
            if value.get("title") == "ReadOnlySandboxPolicy" and isinstance(
                properties, Mapping
            ):
                return "access" in properties
            return any(visit(child) for child in value.values())
        if isinstance(value, list):
            return any(visit(child) for child in value)
        return False

    return visit(payload)


def _schema_supports_permission_profiles(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("PROTOCOL_SCHEMA_GENERATION_FAILED") from exc

    def visit(value: object) -> bool:
        if isinstance(value, Mapping):
            if value.get("title") == "PermissionProfileListParams":
                return True
            return any(visit(child) for child in value.values())
        if isinstance(value, list):
            return any(visit(child) for child in value)
        return False

    return visit(payload)


def validate_private_codex_home(codex_home: Path, *, repository_root: Path) -> Path:
    resolved = _validate_private_directory(
        codex_home,
        reason_prefix="CODEX_HOME",
        forbidden_roots=(repository_root,),
    )
    try:
        repository_root.resolve(strict=True).relative_to(resolved)
    except ValueError:
        return resolved
    raise LifecycleProbeError("CODEX_HOME_LOCATION_FORBIDDEN")


def build_lifecycle_environment(
    *,
    codex_home: Path,
    credential_home: Path,
    tmp_dir: Path,
) -> dict[str, str]:
    return {
        "CODEX_HOME": str(codex_home),
        "HOME": str(credential_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(tmp_dir),
    }


def build_lifecycle_command(
    codex_bin: Path,
    *,
    sqlite_home: Path,
    log_dir: Path,
) -> list[str]:
    command = build_command(codex_bin)
    overrides = (
        *_LIFECYCLE_HARDENING_OVERRIDES,
        f"sqlite_home={_toml_string(sqlite_home)}",
        f"log_dir={_toml_string(log_dir)}",
    )
    for override in overrides:
        command.extend(("--config", override))
    return command


@contextmanager
def _canary_runtime_state(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
) -> Iterator[CanaryRuntimeState]:
    repository_root = repository_root.resolve(strict=True)
    codex_home = validate_private_codex_home(
        codex_home,
        repository_root=repository_root,
    )
    state_path: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-codex-lifecycle-") as state:
            state_path = Path(state).resolve(strict=True)
            os.chmod(state_path, 0o700)
            tmp_dir = _mkdir_private(state_path / "tmp")
            sterile_cwd = _mkdir_private(state_path / "workspace")
            sqlite_home = _mkdir_private(state_path / "sqlite")
            log_dir = _mkdir_private(state_path / "logs")
            protocol = verify_protocol_lock(
                codex_bin,
                lock=load_protocol_lock(),
                scratch_dir=state_path,
            )
            environment = build_lifecycle_environment(
                codex_home=codex_home,
                credential_home=_validate_credential_home(Path.home()),
                tmp_dir=tmp_dir,
            )
            command = build_lifecycle_command(
                codex_bin,
                sqlite_home=sqlite_home,
                log_dir=log_dir,
            )
            yield CanaryRuntimeState(
                codex_home=codex_home,
                command=tuple(command),
                environment=environment,
                log_dir=log_dir,
                protocol=protocol,
                sqlite_home=sqlite_home,
                state_path=state_path,
                sterile_cwd=sterile_cwd,
                tmp_dir=tmp_dir,
            )
    finally:
        if state_path is not None and state_path.exists():
            raise LifecycleProbeError("RUN_STATE_CLEANUP_FAILED")


def _validate_initialize_result(
    result: Mapping[str, Any], *, codex_home: Path
) -> dict[str, str]:
    expected_fields = ("codexHome", "platformFamily", "platformOs", "userAgent")
    if any(not _nonempty_string(result.get(field)) for field in expected_fields):
        raise LifecycleProbeError("INITIALIZE_RESULT_INVALID")
    if Path(str(result["codexHome"])).resolve(strict=True) != codex_home:
        raise LifecycleProbeError("CODEX_HOME_BINDING_MISMATCH")
    return {
        "family": str(result["platformFamily"]),
        "os": str(result["platformOs"]),
    }


def validate_effective_config(result: Mapping[str, Any]) -> None:
    config = result.get("config")
    if not isinstance(config, Mapping):
        raise LifecycleProbeError("EFFECTIVE_CONFIG_INVALID")
    features = config.get("features")
    if not isinstance(features, Mapping) or any(
        features.get(feature) is not False for feature in _REQUIRED_DISABLED_FEATURES
    ):
        raise LifecycleProbeError("HARDENING_FEATURE_NOT_DISABLED")
    hooks = config.get("hooks", {})
    if not isinstance(hooks, Mapping) or any(value != [] for value in hooks.values()):
        raise LifecycleProbeError("AMBIENT_CAPABILITY_CONFIGURED")
    for key in ("mcp_servers", "plugins"):
        value = config.get(key, {})
        if not isinstance(value, Mapping) or value:
            raise LifecycleProbeError("AMBIENT_CAPABILITY_CONFIGURED")
    if config.get("approval_policy") != "never":
        raise LifecycleProbeError("APPROVAL_POLICY_NOT_LOCKED")
    if config.get("forced_login_method") != "chatgpt":
        raise LifecycleProbeError("LOGIN_METHOD_NOT_LOCKED")
    from reqpilotx_server.adapters.agents.codex_app_server.probe import configured_auth_store

    if config.get("cli_auth_credentials_store") != configured_auth_store():
        raise LifecycleProbeError("CREDENTIAL_STORE_NOT_LOCKED")
    if config.get("check_for_update_on_startup") is not False:
        raise LifecycleProbeError("STARTUP_UPDATE_CHECK_ENABLED")
    history = config.get("history")
    analytics = config.get("analytics")
    feedback = config.get("feedback")
    otel = config.get("otel")
    shell_environment = config.get("shell_environment_policy")
    if not isinstance(history, Mapping) or history.get("persistence") != "none":
        raise LifecycleProbeError("HISTORY_PERSISTENCE_ENABLED")
    if not isinstance(analytics, Mapping) or analytics.get("enabled") is not False:
        raise LifecycleProbeError("ANALYTICS_ENABLED")
    if not isinstance(feedback, Mapping) or feedback.get("enabled") is not False:
        raise LifecycleProbeError("FEEDBACK_ENABLED")
    if (
        not isinstance(otel, Mapping)
        or otel.get("exporter") != "none"
        or otel.get("trace_exporter") != "none"
        or otel.get("metrics_exporter") != "none"
        or otel.get("log_user_prompt") is not False
    ):
        raise LifecycleProbeError("OTEL_NOT_DISABLED")
    if (
        not isinstance(shell_environment, Mapping)
        or shell_environment.get("inherit") != "none"
        or shell_environment.get("ignore_default_excludes") is not False
    ):
        raise LifecycleProbeError("SHELL_ENVIRONMENT_NOT_LOCKED")


def _preflight_session(
    session: JsonlRpcSession,
    *,
    codex_home: Path,
    model: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    platform = _validate_initialize_result(
        session.initialize(),
        codex_home=codex_home,
    )
    account = safe_account_summary(
        session.request("account/read", {"refreshToken": False})
    )
    if account.get("account_type") != "chatgpt":
        raise LifecycleProbeError("AUTH_REQUIRED")
    model_result = session.request(
        "model/list",
        {"limit": 100, "includeHidden": False},
    )
    if model_result.get("nextCursor") is not None:
        raise LifecycleProbeError("MODEL_LIST_INCOMPLETE")
    models = safe_model_summary(model_result, preferred_model=model)
    if not models.get("preferred_model_available"):
        raise LifecycleProbeError("MODEL_UNAVAILABLE")
    validate_effective_config(session.request("config/read", {"includeLayers": False}))
    mcp_status = session.request(
        "mcpServerStatus/list",
        {"limit": 100, "detail": "toolsAndAuthOnly"},
    )
    if mcp_status.get("data") != [] or mcp_status.get("nextCursor") is not None:
        raise LifecycleProbeError("AMBIENT_MCP_AVAILABLE")
    return platform, account


def _start_or_resume_thread(
    session: JsonlRpcSession,
    ledger: EventLedger,
    *,
    model: str,
    sterile_cwd: Path,
    thread_id: str | None = None,
    developer_instructions: str = _SYNTHETIC_DEVELOPER_INSTRUCTIONS,
) -> str:
    if thread_id is None:
        method = "thread/start"
        params: dict[str, Any] = {
            "model": model,
            "cwd": str(sterile_cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": False,
            "developerInstructions": developer_instructions,
            "serviceName": _SERVICE_NAME,
        }
    else:
        method = "thread/resume"
        params = {
            "threadId": thread_id,
            "model": model,
            "cwd": str(sterile_cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "developerInstructions": developer_instructions,
        }
    result = session.request(method, params)
    resolved_thread_id = _validate_thread_start_result(
        result,
        model=model,
        sterile_cwd=sterile_cwd,
    )
    if thread_id is not None and resolved_thread_id != thread_id:
        raise LifecycleProbeError("RESUMED_THREAD_ID_MISMATCH")
    ledger.bind_thread(resolved_thread_id)
    return resolved_thread_id


def _start_marker_turn(
    session: JsonlRpcSession,
    ledger: EventLedger,
    *,
    thread_id: str,
    model: str,
    sterile_cwd: Path,
    marker: str,
) -> tuple[str, str]:
    result = session.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": f"Return exactly one JSON object whose marker is {marker}.",
                }
            ],
            "cwd": str(sterile_cwd),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "model": model,
            "effort": "medium",
            "summary": "none",
            "outputSchema": {
                "type": "object",
                "properties": {
                    "marker": {
                        "type": "string",
                        "enum": [marker],
                    }
                },
                "required": ["marker"],
                "additionalProperties": False,
            },
        },
    )
    turn_id = _validate_turn_start_result(result)
    ledger.bind_turn(turn_id)
    terminal_status = session.wait_for_turn(turn_id)
    if terminal_status != "completed":
        raise LifecycleProbeError("TURN_NOT_COMPLETED")
    return turn_id, _validate_synthetic_response(ledger.final_agent_message, marker)


def _start_interrupt_turn(
    session: JsonlRpcSession,
    ledger: EventLedger,
    *,
    thread_id: str,
    model: str,
    sterile_cwd: Path,
) -> str:
    result = session.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": (
                        f"Synthetic marker {INTERRUPT_MARKER}. Return a JSON object "
                        "whose payload is exactly 1000000 uppercase X characters."
                    ),
                }
            ],
            "cwd": str(sterile_cwd),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "model": model,
            "effort": "medium",
            "summary": "none",
        },
    )
    turn_id = _validate_turn_start_result(result)
    ledger.bind_turn(turn_id)
    if turn_id in ledger.terminal_turns:
        raise LifecycleProbeError("INTERRUPT_RACE")
    return turn_id


def _start_crash_turn(
    session: JsonlRpcSession,
    ledger: EventLedger,
    *,
    thread_id: str,
    model: str,
    sterile_cwd: Path,
) -> str:
    result = session.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": (
                        f"Synthetic marker {CRASH_MARKER}. Return a JSON object "
                        "whose payload is exactly 1000000 uppercase Y characters."
                    ),
                }
            ],
            "cwd": str(sterile_cwd),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "model": model,
            "effort": "medium",
            "summary": "none",
        },
    )
    turn_id = _validate_turn_start_result(result)
    ledger.bind_turn(turn_id)
    if turn_id in ledger.terminal_turns:
        raise LifecycleProbeError("CRASH_RACE_LOST")
    return turn_id


def run_normal_lifecycle_canary(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if model != DEFAULT_PREFERRED_MODEL:
        raise LifecycleProbeError("MODEL_NOT_PINNED")
    if timeout_seconds <= 0:
        raise LifecycleProbeError("TIMEOUT_INVALID")
    repository_root = repository_root.resolve(strict=True)
    codex_home = validate_private_codex_home(
        codex_home,
        repository_root=repository_root,
    )
    lock = load_protocol_lock()
    state_path: Path | None = None
    receipt: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="reqpilotx-codex-lifecycle-") as state:
        state_path = Path(state).resolve(strict=True)
        os.chmod(state_path, 0o700)
        tmp_dir = _mkdir_private(state_path / "tmp")
        sterile_cwd = _mkdir_private(state_path / "workspace")
        sqlite_home = _mkdir_private(state_path / "sqlite")
        log_dir = _mkdir_private(state_path / "logs")
        protocol = verify_protocol_lock(
            codex_bin,
            lock=lock,
            scratch_dir=state_path,
        )
        environment = build_lifecycle_environment(
            codex_home=codex_home,
            credential_home=_validate_credential_home(Path.home()),
            tmp_dir=tmp_dir,
        )
        command = build_lifecycle_command(
            codex_bin,
            sqlite_home=sqlite_home,
            log_dir=log_dir,
        )
        ledger = EventLedger()
        session = JsonlRpcSession(
            command=command,
            environment=environment,
            cwd=sterile_cwd,
            timeout_seconds=timeout_seconds,
            ledger=ledger,
        )
        thread_id: str | None = None
        turn_id: str | None = None
        try:
            platform, account = _preflight_session(
                session,
                codex_home=codex_home,
                model=model,
            )
            thread_result = session.request(
                "thread/start",
                {
                    "model": model,
                    "cwd": str(sterile_cwd),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": False,
                    "developerInstructions": (
                        "Synthetic protocol canary. Do not inspect files, call tools, "
                        "or use external data. Return only the requested JSON object."
                    ),
                    "serviceName": _SERVICE_NAME,
                },
            )
            thread_id = _validate_thread_start_result(
                thread_result,
                model=model,
                sterile_cwd=sterile_cwd,
            )
            ledger.bind_thread(thread_id)
            turn_result = session.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "Return exactly one JSON object whose marker is "
                                f"{SYNTHETIC_MARKER}."
                            ),
                        }
                    ],
                    "cwd": str(sterile_cwd),
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "model": model,
                    "effort": "medium",
                    "summary": "none",
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "marker": {
                                "type": "string",
                                "enum": [SYNTHETIC_MARKER],
                            }
                        },
                        "required": ["marker"],
                        "additionalProperties": False,
                    },
                },
            )
            turn_id = _validate_turn_start_result(turn_result)
            ledger.bind_turn(turn_id)
            terminal_status = session.wait_for_turn(turn_id)
            if terminal_status != "completed":
                raise LifecycleProbeError("TURN_NOT_COMPLETED")
            response_digest = _validate_synthetic_response(ledger.final_agent_message)
            session.request("thread/delete", {"threadId": thread_id})
            session.wait_for_thread_deleted(thread_id)
            ledger.validate_normal_sequence(thread_id=thread_id, turn_id=turn_id)
            if any(sterile_cwd.iterdir()):
                raise LifecycleProbeError("STERILE_CWD_MUTATED")
            session.close()
            post_restart_events = _verify_thread_absent_after_restart(
                command=command,
                environment=environment,
                cwd=sterile_cwd,
                timeout_seconds=timeout_seconds,
                thread_id=thread_id,
                codex_home=codex_home,
            )
            session_inventory = _assert_persistent_markers_absent(codex_home)
            receipt = {
                "schema": LIFECYCLE_PROBE_SCHEMA,
                "status": "PASS",
                "model": model,
                "account_type": "chatgpt",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn_status": terminal_status,
                "response_sha256": response_digest,
                "protocol": protocol,
                "platform": platform,
                "events": ledger.content_free_summary(),
                "post_restart_events": post_restart_events,
                "post_restart_thread_absent": True,
                "codex_home_session_marker_absent": True,
                "codex_home_session_inventory": session_inventory,
                "thread_delete_event_observed": True,
                "run_temp_cleanup": False,
            }
        except BaseException as exc:
            if thread_id is not None and thread_id not in ledger.deleted_thread_ids:
                try:
                    _cleanup_failed_run(
                        session,
                        ledger=ledger,
                        thread_id=thread_id,
                        turn_id=turn_id,
                    )
                except LifecycleProbeError as cleanup_exc:
                    raise LifecycleProbeError("CLEANUP_FAILED") from cleanup_exc
            if isinstance(exc, LifecycleProbeError):
                raise
            raise LifecycleProbeError("RUN_ABORTED") from exc
        finally:
            session.close()
    if state_path is None or state_path.exists():
        raise LifecycleProbeError("RUN_STATE_CLEANUP_FAILED")
    receipt["run_temp_cleanup"] = True
    return receipt


def run_interrupt_resume_lifecycle_canary(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if model != DEFAULT_PREFERRED_MODEL:
        raise LifecycleProbeError("MODEL_NOT_PINNED")
    if timeout_seconds <= 0:
        raise LifecycleProbeError("TIMEOUT_INVALID")
    receipt: dict[str, Any]
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository_root,
    ) as runtime:
        interrupt_ledger = EventLedger()
        interrupt_session = _open_runtime_session(
            runtime,
            ledger=interrupt_ledger,
            timeout_seconds=timeout_seconds,
        )
        resume_session: JsonlRpcSession | None = None
        thread_id: str | None = None
        thread_deleted = False
        try:
            platform, account = _preflight_session(
                interrupt_session,
                codex_home=runtime.codex_home,
                model=model,
            )
            thread_id = _start_or_resume_thread(
                interrupt_session,
                interrupt_ledger,
                model=model,
                sterile_cwd=runtime.sterile_cwd,
            )
            interrupt_turn_id = _start_interrupt_turn(
                interrupt_session,
                interrupt_ledger,
                thread_id=thread_id,
                model=model,
                sterile_cwd=runtime.sterile_cwd,
            )
            interrupt_session.wait_for_turn_started(interrupt_turn_id)
            interrupt_result = interrupt_session.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": interrupt_turn_id},
            )
            if dict(interrupt_result) != {}:
                raise LifecycleProbeError("INTERRUPT_RESPONSE_INVALID")
            terminal = interrupt_session.wait_for_turn(interrupt_turn_id)
            if terminal != "interrupted":
                raise LifecycleProbeError("INTERRUPT_RACE")
            interrupt_ledger.validate_interrupted_sequence(
                thread_id=thread_id,
                turn_id=interrupt_turn_id,
            )
            interrupt_session.stop_gracefully()

            resume_ledger = EventLedger()
            resume_session = _open_runtime_session(
                runtime,
                ledger=resume_ledger,
                timeout_seconds=timeout_seconds,
            )
            _preflight_session(
                resume_session,
                codex_home=runtime.codex_home,
                model=model,
            )
            resumed_thread_id = _start_or_resume_thread(
                resume_session,
                resume_ledger,
                model=model,
                sterile_cwd=runtime.sterile_cwd,
                thread_id=thread_id,
            )
            resume_turn_id, response_digest = _start_marker_turn(
                resume_session,
                resume_ledger,
                thread_id=resumed_thread_id,
                model=model,
                sterile_cwd=runtime.sterile_cwd,
                marker=RESUME_MARKER,
            )
            if resume_turn_id == interrupt_turn_id:
                raise LifecycleProbeError("TURN_ID_REUSED")
            resume_session.request("thread/delete", {"threadId": thread_id})
            resume_session.wait_for_thread_deleted(thread_id)
            thread_deleted = True
            resume_ledger.validate_normal_sequence(
                thread_id=thread_id,
                turn_id=resume_turn_id,
                require_thread_started=False,
            )
            if any(runtime.sterile_cwd.iterdir()):
                raise LifecycleProbeError("STERILE_CWD_MUTATED")
            resume_session.stop_gracefully()
            post_restart_events = _verify_thread_absent_after_restart(
                command=runtime.command,
                environment=runtime.environment,
                cwd=runtime.sterile_cwd,
                timeout_seconds=timeout_seconds,
                thread_id=thread_id,
                codex_home=runtime.codex_home,
            )
            session_inventory = _assert_persistent_markers_absent(runtime.codex_home)
            receipt = {
                "schema": LIFECYCLE_PROBE_SCHEMA,
                "scenario": "interrupt_resume",
                "status": "PASS",
                "model": model,
                "account_type": account["account_type"],
                "platform": platform,
                "protocol": dict(runtime.protocol),
                "thread_id": thread_id,
                "interrupt": {
                    "turn_id": interrupt_turn_id,
                    "turn_status": terminal,
                    "events": interrupt_ledger.content_free_summary(),
                },
                "resume": {
                    "turn_id": resume_turn_id,
                    "turn_status": "completed",
                    "response_sha256": response_digest,
                    "events": resume_ledger.content_free_summary(),
                },
                "first_process_graceful_stop": True,
                "second_process_graceful_stop": True,
                "process_restart": True,
                "thread_delete_event_observed": True,
                "post_restart_thread_absent": True,
                "post_restart_events": post_restart_events,
                "codex_home_session_marker_absent": True,
                "codex_home_session_inventory": session_inventory,
                "run_temp_cleanup": False,
            }
        except BaseException as exc:
            interrupt_session.close(force=True)
            if resume_session is not None:
                resume_session.close(force=True)
            if thread_id is not None and not thread_deleted:
                try:
                    _delete_thread_with_fresh_session(
                        runtime,
                        thread_id=thread_id,
                        timeout_seconds=min(timeout_seconds, 20.0),
                    )
                except LifecycleProbeError as cleanup_exc:
                    raise LifecycleProbeError("CLEANUP_FAILED") from cleanup_exc
            if isinstance(exc, LifecycleProbeError):
                raise
            raise LifecycleProbeError("RUN_ABORTED") from exc
        finally:
            interrupt_session.close()
            if resume_session is not None:
                resume_session.close()
    receipt["run_temp_cleanup"] = True
    return receipt


def run_crash_restart_lifecycle_canary(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if model != DEFAULT_PREFERRED_MODEL:
        raise LifecycleProbeError("MODEL_NOT_PINNED")
    if timeout_seconds <= 0:
        raise LifecycleProbeError("TIMEOUT_INVALID")
    receipt: dict[str, Any]
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository_root,
    ) as runtime:
        crash_ledger = EventLedger()
        crash_session = _open_runtime_session(
            runtime,
            ledger=crash_ledger,
            timeout_seconds=timeout_seconds,
        )
        recovery_session: JsonlRpcSession | None = None
        thread_id: str | None = None
        thread_deleted = False
        try:
            platform, account = _preflight_session(
                crash_session,
                codex_home=runtime.codex_home,
                model=model,
            )
            thread_id = _start_or_resume_thread(
                crash_session,
                crash_ledger,
                model=model,
                sterile_cwd=runtime.sterile_cwd,
            )
            crash_turn_id = _start_crash_turn(
                crash_session,
                crash_ledger,
                thread_id=thread_id,
                model=model,
                sterile_cwd=runtime.sterile_cwd,
            )
            crash_session.wait_for_turn_started(crash_turn_id)
            crash_session.kill_and_confirm_unknown(crash_turn_id)

            recovery_ledger = EventLedger()
            recovery_session = _open_runtime_session(
                runtime,
                ledger=recovery_ledger,
                timeout_seconds=timeout_seconds,
            )
            _preflight_session(
                recovery_session,
                codex_home=runtime.codex_home,
                model=model,
            )
            read_result = recovery_session.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            status_before_resume = _turn_status_from_thread_result(
                read_result,
                thread_id=thread_id,
                turn_id=crash_turn_id,
                sterile_cwd=runtime.sterile_cwd,
            )
            resume_result = recovery_session.request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "model": model,
                    "cwd": str(runtime.sterile_cwd),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "developerInstructions": _SYNTHETIC_DEVELOPER_INSTRUCTIONS,
                },
            )
            resumed_thread_id = _validate_thread_start_result(
                resume_result,
                model=model,
                sterile_cwd=runtime.sterile_cwd,
            )
            if resumed_thread_id != thread_id:
                raise LifecycleProbeError("RESUMED_THREAD_ID_MISMATCH")
            recovery_ledger.bind_thread(thread_id)
            status_after_resume = _turn_status_from_thread_result(
                resume_result,
                thread_id=thread_id,
                turn_id=crash_turn_id,
                sterile_cwd=runtime.sterile_cwd,
            )
            if status_after_resume == "inProgress":
                raise LifecycleProbeError("CRASH_RECONCILIATION_STALE_IN_PROGRESS")
            if (
                status_before_resume in {"completed", "failed", "interrupted"}
                and status_after_resume != status_before_resume
            ):
                raise LifecycleProbeError("CRASH_TERMINAL_STATUS_DRIFT")
            recovery_session.request("thread/delete", {"threadId": thread_id})
            recovery_session.wait_for_thread_deleted(thread_id)
            thread_deleted = True
            if any(runtime.sterile_cwd.iterdir()):
                raise LifecycleProbeError("STERILE_CWD_MUTATED")
            recovery_session.stop_gracefully()
            post_restart_events = _verify_thread_absent_after_restart(
                command=runtime.command,
                environment=runtime.environment,
                cwd=runtime.sterile_cwd,
                timeout_seconds=timeout_seconds,
                thread_id=thread_id,
                codex_home=runtime.codex_home,
            )
            session_inventory = _assert_persistent_markers_absent(runtime.codex_home)
            receipt = {
                "schema": LIFECYCLE_PROBE_SCHEMA,
                "scenario": "crash_restart",
                "status": "PASS",
                "model": model,
                "account_type": account["account_type"],
                "platform": platform,
                "protocol": dict(runtime.protocol),
                "thread_id": thread_id,
                "turn_id": crash_turn_id,
                "transport_state": "UNKNOWN_AFTER_CRASH",
                "reconciled_status_before_resume": status_before_resume,
                "reconciled_status_after_resume": status_after_resume,
                "turn_start_count": 1,
                "recovery_turn_start_count": 0,
                "crash_events": crash_ledger.content_free_summary(),
                "recovery_events": recovery_ledger.content_free_summary(),
                "process_restart": True,
                "thread_delete_event_observed": True,
                "post_restart_thread_absent": True,
                "post_restart_events": post_restart_events,
                "codex_home_session_marker_absent": True,
                "codex_home_session_inventory": session_inventory,
                "run_temp_cleanup": False,
            }
        except BaseException as exc:
            crash_session.close(force=True)
            if recovery_session is not None:
                recovery_session.close(force=True)
            if thread_id is not None and not thread_deleted:
                try:
                    _delete_thread_with_fresh_session(
                        runtime,
                        thread_id=thread_id,
                        timeout_seconds=min(timeout_seconds, 20.0),
                    )
                except LifecycleProbeError as cleanup_exc:
                    raise LifecycleProbeError("CLEANUP_FAILED") from cleanup_exc
            if isinstance(exc, LifecycleProbeError):
                raise
            raise LifecycleProbeError("RUN_ABORTED") from exc
        finally:
            crash_session.close()
            if recovery_session is not None:
                recovery_session.close()
    receipt["run_temp_cleanup"] = True
    return receipt


def _open_runtime_session(
    runtime: CanaryRuntimeState,
    *,
    ledger: EventLedger,
    timeout_seconds: float,
) -> JsonlRpcSession:
    return JsonlRpcSession(
        command=runtime.command,
        environment=runtime.environment,
        cwd=runtime.sterile_cwd,
        timeout_seconds=timeout_seconds,
        ledger=ledger,
    )


def _delete_thread_with_fresh_session(
    runtime: CanaryRuntimeState,
    *,
    thread_id: str,
    timeout_seconds: float,
) -> None:
    ledger = EventLedger()
    session = _open_runtime_session(
        runtime,
        ledger=ledger,
        timeout_seconds=timeout_seconds,
    )
    try:
        _validate_initialize_result(
            session.initialize(),
            codex_home=runtime.codex_home,
        )
        session.request("thread/delete", {"threadId": thread_id})
    finally:
        session.close()
    _verify_thread_absent_after_restart(
        command=runtime.command,
        environment=runtime.environment,
        cwd=runtime.sterile_cwd,
        timeout_seconds=timeout_seconds,
        thread_id=thread_id,
        codex_home=runtime.codex_home,
    )


def _validate_thread_start_result(
    result: Mapping[str, Any],
    *,
    model: str,
    sterile_cwd: Path,
) -> str:
    sandbox = result.get("sandbox")
    thread = result.get("thread")
    if (
        result.get("model") != model
        or result.get("cwd") != str(sterile_cwd)
        or result.get("approvalPolicy") != "never"
        or not isinstance(sandbox, Mapping)
        or sandbox.get("type") != "readOnly"
        or sandbox.get("networkAccess") is not False
        or not isinstance(thread, Mapping)
        or not _nonempty_string(thread.get("id"))
        or thread.get("cwd") != str(sterile_cwd)
        or thread.get("ephemeral") is not False
    ):
        raise LifecycleProbeError("THREAD_START_RESULT_INVALID")
    _validate_instruction_sources(result, sterile_cwd=sterile_cwd)
    return str(thread["id"])


def _turn_status_from_thread_result(
    result: Mapping[str, Any],
    *,
    thread_id: str,
    turn_id: str,
    sterile_cwd: Path,
) -> str:
    thread = result.get("thread")
    if (
        not isinstance(thread, Mapping)
        or thread.get("id") != thread_id
        or thread.get("cwd") != str(sterile_cwd)
        or thread.get("ephemeral") is not False
        or not isinstance(thread.get("turns"), list)
    ):
        raise LifecycleProbeError("CRASH_THREAD_SNAPSHOT_INVALID")
    matching_turns = [
        turn
        for turn in thread["turns"]
        if isinstance(turn, Mapping) and turn.get("id") == turn_id
    ]
    if len(matching_turns) != 1:
        raise LifecycleProbeError("CRASH_TURN_SNAPSHOT_INVALID")
    status = matching_turns[0].get("status")
    if status not in {"completed", "failed", "inProgress", "interrupted"}:
        raise LifecycleProbeError("CRASH_TURN_STATUS_INVALID")
    return str(status)


def _validate_turn_start_result(result: Mapping[str, Any]) -> str:
    turn = result.get("turn")
    if (
        not isinstance(turn, Mapping)
        or not _nonempty_string(turn.get("id"))
        or turn.get("status") != "inProgress"
        or turn.get("items") != []
        or turn.get("error") is not None
    ):
        raise LifecycleProbeError("TURN_START_RESULT_INVALID")
    return str(turn["id"])


def _validate_instruction_sources(
    result: Mapping[str, Any], *, sterile_cwd: Path
) -> None:
    if "instructionSources" not in result:
        raise LifecycleProbeError("INSTRUCTION_SOURCES_MISSING")
    sources = result.get("instructionSources")
    if not isinstance(sources, list):
        raise LifecycleProbeError("INSTRUCTION_SOURCES_INVALID")
    for raw in sources:
        if not isinstance(raw, str):
            raise LifecycleProbeError("INSTRUCTION_SOURCES_INVALID")
        source = Path(raw)
        try:
            source.resolve(strict=True).relative_to(sterile_cwd)
        except (OSError, ValueError) as exc:
            raise LifecycleProbeError("AMBIENT_INSTRUCTION_SOURCE") from exc


def _verify_thread_absent_after_restart(
    *,
    command: Sequence[str],
    environment: Mapping[str, str],
    cwd: Path,
    timeout_seconds: float,
    thread_id: str,
    codex_home: Path,
) -> dict[str, Any]:
    ledger = EventLedger()
    session = JsonlRpcSession(
        command=command,
        environment=environment,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        ledger=ledger,
    )
    try:
        _validate_initialize_result(session.initialize(), codex_home=codex_home)
        for archived in (False, True):
            result = session.request(
                "thread/list",
                {"archived": archived, "limit": 100},
            )
            data = result.get("data")
            if not isinstance(data, list) or result.get("nextCursor") is not None:
                raise LifecycleProbeError("POST_RESTART_THREAD_LIST_INVALID")
            if any(
                isinstance(thread, Mapping) and thread.get("id") == thread_id
                for thread in data
            ):
                raise LifecycleProbeError("POST_RESTART_THREAD_STILL_PRESENT")
    finally:
        session.close()
    return ledger.content_free_summary()


def _assert_persistent_markers_absent(
    codex_home: Path,
    *,
    extra_markers: Sequence[str] = (),
) -> dict[str, int]:
    inventory = {
        "archived_sessions_file_count": 0,
        "sessions_file_count": 0,
    }
    for directory_name in ("archived_sessions", "sessions"):
        root = codex_home / directory_name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise LifecycleProbeError("PERSISTENT_SESSION_PATH_INVALID")
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                raise LifecycleProbeError("PERSISTENT_SESSION_PATH_INVALID")
            if not candidate.is_file():
                continue
            inventory[f"{directory_name}_file_count"] += 1
            for marker in (*_SYNTHETIC_MARKERS, *extra_markers):
                if _file_contains(candidate, marker.encode("utf-8")):
                    raise LifecycleProbeError("PERSISTENT_MARKER_RETAINED")
    return inventory


def _file_contains(path: Path, marker: bytes) -> bool:
    overlap = b""
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(65_536):
                combined = overlap + chunk
                if marker in combined:
                    return True
                overlap = combined[-max(len(marker) - 1, 0) :]
    except OSError as exc:
        raise LifecycleProbeError("PERSISTENT_SESSION_UNREADABLE") from exc
    return False


def _cleanup_failed_run(
    session: JsonlRpcSession,
    *,
    ledger: EventLedger,
    thread_id: str,
    turn_id: str | None,
) -> None:
    if turn_id is not None and turn_id not in ledger.terminal_turns:
        session.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )
        terminal = session.wait_for_turn(turn_id)
        if terminal != "interrupted":
            raise LifecycleProbeError("INTERRUPT_TERMINAL_INVALID")
    session.request("thread/delete", {"threadId": thread_id})
    session.wait_for_thread_deleted(thread_id)


def _validate_synthetic_response(
    value: str | None,
    marker: str = SYNTHETIC_MARKER,
) -> str:
    if not isinstance(value, str):
        raise LifecycleProbeError("STRUCTURED_RESPONSE_MISSING")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LifecycleProbeError("STRUCTURED_RESPONSE_INVALID") from exc
    if payload != {"marker": marker}:
        raise LifecycleProbeError("STRUCTURED_RESPONSE_INVALID")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_private_directory(
    path: Path,
    *,
    reason_prefix: str,
    forbidden_roots: Sequence[Path] = (),
) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise LifecycleProbeError(f"{reason_prefix}_PATH_INVALID")
    try:
        resolved = path.resolve(strict=True)
        details = resolved.stat()
    except OSError as exc:
        raise LifecycleProbeError(f"{reason_prefix}_PATH_INVALID") from exc
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise LifecycleProbeError(f"{reason_prefix}_OWNERSHIP_INVALID")
    if resolved != path:
        raise LifecycleProbeError(f"{reason_prefix}_PATH_INVALID")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise LifecycleProbeError(f"{reason_prefix}_PERMISSIONS_INVALID")
    for root in forbidden_roots:
        try:
            resolved.relative_to(root.resolve(strict=True))
        except ValueError:
            continue
        raise LifecycleProbeError(f"{reason_prefix}_LOCATION_FORBIDDEN")
    return resolved


def _validate_credential_home(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise LifecycleProbeError("CREDENTIAL_HOME_INVALID")
    try:
        resolved = path.resolve(strict=True)
        details = resolved.stat()
    except OSError as exc:
        raise LifecycleProbeError("CREDENTIAL_HOME_INVALID") from exc
    if (
        resolved != path
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
    ):
        raise LifecycleProbeError("CREDENTIAL_HOME_INVALID")
    return resolved


def _mkdir_private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path.resolve(strict=True)


def _signal_process_group(
    process: subprocess.Popen[bytes], sig: signal.Signals
) -> None:
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    if process_group != process.pid:
        raise LifecycleProbeError("PROCESS_GROUP_IDENTITY_INVALID")
    try:
        os.killpg(process_group, sig)
    except ProcessLookupError:
        return


def _codex_error_reason(params: Mapping[str, Any]) -> str:
    error = params.get("error")
    if not isinstance(error, Mapping):
        return "APP_SERVER_ERROR_NOTIFICATION"
    info = error.get("codexErrorInfo")
    kind = ""
    http_status: int | None = None
    if isinstance(info, str):
        kind = info
    elif isinstance(info, Mapping):
        kind = str(info.get("type") or info.get("kind") or "")
        if not kind and len(info) == 1:
            kind = str(next(iter(info)))
        status_candidate = info.get("httpStatusCode")
        if status_candidate is None:
            for value in info.values():
                if isinstance(value, Mapping) and "httpStatusCode" in value:
                    status_candidate = value.get("httpStatusCode")
                    break
        if type(status_candidate) is int:
            http_status = status_candidate
    normalized = re.sub(r"[^a-z0-9]+", "", kind.casefold())
    if normalized == "usagelimitexceeded" or http_status == 429:
        return "USAGE_LIMITED"
    if normalized == "unauthorized" or http_status == 401:
        return "REAUTH_REQUIRED"
    if normalized in {
        "httpconnectionfailed",
        "responsestreamconnectionfailed",
        "responsestreamdisconnected",
        "responsetoomanyfailedattempts",
        "internalservererror",
    } or (http_status is not None and http_status >= 500):
        return "APP_SERVER_TEMPORARY_UNAVAILABLE"
    if normalized in {"contextwindowexceeded", "badrequest", "sandboxerror"}:
        return "APP_SERVER_REQUEST_REJECTED"
    return "APP_SERVER_ERROR_NOTIFICATION"


def _notification_thread_id(params: Mapping[str, Any]) -> str | None:
    value = params.get("threadId")
    if _nonempty_string(value):
        return str(value)
    thread = params.get("thread")
    if isinstance(thread, Mapping) and _nonempty_string(thread.get("id")):
        return str(thread["id"])
    return None


def _notification_turn_id(params: Mapping[str, Any]) -> str | None:
    value = params.get("turnId")
    if _nonempty_string(value):
        return str(value)
    turn = params.get("turn")
    if isinstance(turn, Mapping) and _nonempty_string(turn.get("id")):
        return str(turn["id"])
    return None


def _safe_rpc_error_hint(error: object) -> str:
    if not isinstance(error, Mapping):
        return ""
    message = str(error.get("message") or "").casefold()
    hints = (
        ("not loaded", "_HINT_NOT_LOADED"),
        ("not found", "_HINT_NOT_FOUND"),
        ("does not exist", "_HINT_NOT_FOUND"),
        ("still active", "_HINT_ACTIVE"),
        ("is active", "_HINT_ACTIVE"),
        ("invalid params", "_HINT_INVALID_PARAMS"),
        ("invalid request", "_HINT_INVALID_REQUEST"),
        ("unsupported", "_HINT_UNSUPPORTED"),
    )
    return next((hint for token, hint in hints if token in message), "")


def _method_code(method: str) -> str:
    return method.upper().replace("/", "_").replace("-", "_")


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LifecycleProbeError("LOCKED_ARTIFACT_UNREADABLE") from exc
    return digest.hexdigest()


def _toml_string(path: Path) -> str:
    return json.dumps(str(path.resolve(strict=True)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a fixed, synthetic Codex App Server lifecycle canary."
    )
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path(os.environ.get("REQPILOT_CODEX_BIN", DEFAULT_CODEX_BIN)),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=(
            Path(value)
            if (value := os.environ.get("REQPILOT_CODEX_HOME", "").strip())
            else None
        ),
    )
    parser.add_argument("--model", default=DEFAULT_PREFERRED_MODEL)
    parser.add_argument(
        "--mode",
        choices=("normal", "interrupt-resume", "crash-restart"),
        default="normal",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.codex_home is None:
        payload = {
            "schema": LIFECYCLE_PROBE_SCHEMA,
            "status": "FAIL",
            "reason_code": "DEDICATED_CODEX_HOME_REQUIRED",
        }
        print(json.dumps(payload, sort_keys=True))
        return 2
    try:
        runners = {
            "normal": run_normal_lifecycle_canary,
            "interrupt-resume": run_interrupt_resume_lifecycle_canary,
            "crash-restart": run_crash_restart_lifecycle_canary,
        }
        runner = runners[args.mode]
        result = runner(
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            repository_root=Path(__file__).resolve().parents[3],
            model=str(args.model),
            timeout_seconds=float(args.timeout_seconds),
        )
    except LifecycleProbeError as exc:
        print(
            json.dumps(
                {
                    "schema": LIFECYCLE_PROBE_SCHEMA,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
