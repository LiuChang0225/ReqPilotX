"""ReqPilotX ASGI composition entrypoint."""

from __future__ import annotations

from reqpilotx_server.api.app_factory import create_app
from reqpilotx_server.composition.trust import configure_host_truststore


configure_host_truststore()
app = create_app()
