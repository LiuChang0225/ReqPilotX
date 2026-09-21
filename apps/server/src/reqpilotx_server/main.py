"""ReqPilotX ASGI composition entrypoint."""

from __future__ import annotations

from reqpilotx_server.api.app_factory import create_app


app = create_app()
