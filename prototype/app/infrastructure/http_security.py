from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


LOCAL_CORS_ORIGIN_REGEX = (
    r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?$"
)
_LOCAL_HOST_TOKENS = ("127.0.0.1", "localhost", "[::1]")
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_TEST_MODE_ENV_VAR = "REQPILOT_TEST_MODE"


def is_allowed_host(host_header: str, allowed_hosts: Iterable[str]) -> bool:
    try:
        parsed = urlsplit(f"//{str(host_header or '').strip()}")
        incoming_port = parsed.port
    except ValueError:
        return False
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    incoming_host = parsed.hostname.casefold()
    for configured in allowed_hosts:
        try:
            candidate = urlsplit(f"//{str(configured or '').strip()}")
            configured_port = candidate.port
        except ValueError:
            continue
        if (
            candidate.hostname
            and candidate.hostname.casefold() == incoming_host
            and (configured_port is None or configured_port == incoming_port)
        ):
            return True
    return False


def is_allowed_origin(
    origin: str,
    allowed_origins: Iterable[str],
    *,
    allow_loopback_ports: bool = False,
) -> bool:
    try:
        parsed = urlsplit(str(origin or "").strip())
        incoming_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    for configured in allowed_origins:
        try:
            candidate = urlsplit(str(configured or "").strip())
            configured_port = candidate.port
        except ValueError:
            continue
        same_origin = (
            candidate.scheme == parsed.scheme
            and candidate.hostname
            and candidate.hostname.casefold() == parsed.hostname.casefold()
            and configured_port == incoming_port
        )
        if same_origin:
            return True
        if (
            allow_loopback_ports
            and parsed.hostname in _LOCAL_HOSTNAMES
            and candidate.hostname == parsed.hostname
            and candidate.scheme == parsed.scheme
            and configured_port is None
        ):
            return True
    return False


def is_allowed_local_origin(origin: str) -> bool:
    return is_allowed_origin(
        origin,
        (
            "http://127.0.0.1",
            "https://127.0.0.1",
            "http://localhost",
            "https://localhost",
            "http://[::1]",
            "https://[::1]",
        ),
        allow_loopback_ports=True,
    )


def is_allowed_local_host(
    host_header: str,
    *,
    test_mode: bool | None = None,
) -> bool:
    enabled = (
        os.getenv(_TEST_MODE_ENV_VAR) == "1" if test_mode is None else bool(test_mode)
    )
    allowed = (*_LOCAL_HOST_TOKENS, "testserver") if enabled else _LOCAL_HOST_TOKENS
    return is_allowed_host(host_header, allowed)


class RequestBoundaryMiddleware:
    """Reject untrusted Host and browser Origin values before routing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str],
        allow_loopback_origin_ports: bool = False,
    ) -> None:
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)
        self.allowed_origins = tuple(allowed_origins)
        self.allow_loopback_origin_ports = bool(allow_loopback_origin_ports)
        if not self.allowed_hosts:
            raise ValueError("Request boundary requires at least one allowed host.")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        if not is_allowed_host(headers.get("host", ""), self.allowed_hosts):
            await JSONResponse(
                status_code=400,
                content={"detail": "Invalid request host."},
            )(scope, receive, send)
            return
        origin = headers.get("origin", "").strip()
        if origin and not is_allowed_origin(
            origin,
            self.allowed_origins,
            allow_loopback_ports=self.allow_loopback_origin_ports,
        ):
            await JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin request blocked."},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class LocalRequestBoundaryMiddleware(RequestBoundaryMiddleware):
    """Compatibility constructor for isolated desktop/test consumers."""

    def __init__(self, app: ASGIApp) -> None:
        test_mode = os.getenv(_TEST_MODE_ENV_VAR) == "1"
        hosts = (
            (*_LOCAL_HOST_TOKENS, "testserver")
            if test_mode
            else _LOCAL_HOST_TOKENS
        )
        super().__init__(
            app,
            allowed_hosts=hosts,
            allowed_origins=(
                "http://127.0.0.1",
                "https://127.0.0.1",
                "http://localhost",
                "https://localhost",
                "http://[::1]",
                "https://[::1]",
            ),
            allow_loopback_origin_ports=True,
        )


__all__ = [
    "LOCAL_CORS_ORIGIN_REGEX",
    "LocalRequestBoundaryMiddleware",
    "RequestBoundaryMiddleware",
    "is_allowed_host",
    "is_allowed_local_host",
    "is_allowed_local_origin",
    "is_allowed_origin",
]
