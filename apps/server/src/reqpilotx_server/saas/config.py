from __future__ import annotations

import os
from dataclasses import dataclass, field
from collections.abc import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class SaaSSettings:
    enabled: bool = False
    origin: str = ""
    invitation: str = field(default="", repr=False)
    session_seconds: int = 28800

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        env = os.environ if environment is None else environment
        raw = str(env.get("REQPILOT_SAAS_ENABLED", "0"))
        if raw not in {"0", "1"}:
            raise ValueError("REQPILOT_SAAS_ENABLED must be 0 or 1")
        settings = cls(
            enabled=raw == "1",
            origin=str(env.get("REQPILOT_SAAS_ORIGIN", "")).strip(),
            invitation=str(env.get("REQPILOT_SAAS_INVITE_CODE", "")),
            session_seconds=int(env.get("REQPILOT_SAAS_SESSION_SECONDS", "28800")),
        )
        settings.validate()
        return settings

    def validate(self, *, enabled_required: bool = False) -> None:
        if not self.enabled:
            if enabled_required:
                raise ValueError("SaaS authentication must be enabled")
            return
        parsed = urlsplit(self.origin)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or not parsed.hostname
                or self.origin.endswith("/")):
            raise ValueError("REQPILOT_SAAS_ORIGIN must be one exact origin without a trailing slash")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("SaaS requires HTTPS except for a private loopback pilot")
        if len(self.invitation) < 32 or len(self.invitation) > 512:
            raise ValueError("REQPILOT_SAAS_INVITE_CODE must contain 32..512 characters")
        if not 900 <= self.session_seconds <= 86400:
            raise ValueError("SaaS sessions must expire within 900..86400 seconds")

    @property
    def secure_cookie(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def cookie_name(self) -> str:
        return "__Host-reqpilotx_session" if self.secure_cookie else "reqpilotx_session"
