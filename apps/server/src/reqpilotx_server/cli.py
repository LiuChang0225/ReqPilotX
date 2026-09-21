"""Installed server process entrypoints."""

from __future__ import annotations

import os


def api_main() -> int:
    import uvicorn

    uvicorn.run(
        "reqpilotx_server.main:app",
        host=str(os.getenv("REQPILOT_HOST") or "127.0.0.1"),
        port=int(os.getenv("REQPILOT_PORT") or "8000"),
        log_level=str(os.getenv("REQPILOT_LOG_LEVEL") or "info").lower(),
    )
    return 0
