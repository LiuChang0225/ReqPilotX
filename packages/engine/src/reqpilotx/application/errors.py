from __future__ import annotations

from typing import Any


class ApplicationError(Exception):
    """Transport-neutral application failure.

    ``status_code`` is an API presentation hint, not a dependency on FastAPI.
    Non-HTTP callers can inspect ``code`` and ``detail`` directly.
    """

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        headers: dict[str, str] | None = None,
        code: str = "",
    ) -> None:
        super().__init__(_message_from_detail(detail))
        self.status_code = int(status_code)
        self.detail = detail
        self.headers = dict(headers or {})
        self.code = str(code or "")


def _message_from_detail(detail: Any) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail)
    return str(detail)


__all__ = ["ApplicationError"]
