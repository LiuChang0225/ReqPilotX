"""Run one durable worker and exit if its desktop owner disappears."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path


def _watch_parent() -> None:
    identity = os.environ.get("REQPILOT_DESKTOP_PARENT_INSTANCE_ID", "")
    path = Path(os.environ.get("REQPILOT_DESKTOP_HEARTBEAT_PATH", ""))
    if not identity or not path.is_absolute():
        os._exit(70)
    while True:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(payload["updated_at"])
            valid = payload.get("instance_id") == identity and 0 <= age <= 8
            stopping = payload.get("stopping") is True
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            os._exit(70)
        if stopping:
            signal.raise_signal(signal.SIGTERM)
            return
        time.sleep(1)


def main() -> int:
    threading.Thread(target=_watch_parent, daemon=True).start()
    from reqpilotx_server.worker.__main__ import main as worker_main

    return worker_main()


if __name__ == "__main__":
    raise SystemExit(main())
