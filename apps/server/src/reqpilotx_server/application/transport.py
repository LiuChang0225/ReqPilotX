from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from os import PathLike
from typing import Any, Protocol


class RequestContext(Protocol):
    headers: Mapping[str, str]


class ResponseSink(Protocol):
    headers: MutableMapping[str, str]
    status_code: int


class BackgroundTaskScheduler(Protocol):
    def add_task(
        self,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...


class UploadInput(Protocol):
    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


class CaseInsensitiveHeaders(dict[str, str]):
    """Small transport-neutral header map with HTTP name semantics."""

    def __init__(self, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(
            (str(name).lower(), str(value))
            for name, value in (headers or {}).items()
        )

    def __getitem__(self, name: str) -> str:
        return super().__getitem__(str(name).lower())

    def get(self, name: str, default: Any = None) -> Any:
        return super().get(str(name).lower(), default)

    def __contains__(self, name: object) -> bool:
        return super().__contains__(str(name).lower())


@dataclass(frozen=True, slots=True)
class FileArtifact:
    path: str | PathLike[str]
    filename: str | None = None
    media_type: str | None = None
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _normalized_headers(self.headers))


@dataclass(frozen=True, slots=True)
class BinaryArtifact:
    content: bytes | str
    status_code: int = 200
    media_type: str | None = None
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _normalized_headers(self.headers))


@dataclass(frozen=True, slots=True)
class JsonArtifact:
    content: Any
    status_code: int = 200
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _normalized_headers(self.headers))


@dataclass(frozen=True, slots=True)
class FormValue:
    default: Any


class _UploadRequired:
    __slots__ = ()


UPLOAD_REQUIRED = _UploadRequired()


def form_value(default: Any) -> FormValue:
    return FormValue(default)


def upload_required() -> _UploadRequired:
    return UPLOAD_REQUIRED


def _normalized_headers(
    headers: Mapping[str, str] | None,
) -> CaseInsensitiveHeaders:
    return CaseInsensitiveHeaders(headers)


__all__ = [
    "BackgroundTaskScheduler",
    "BinaryArtifact",
    "CaseInsensitiveHeaders",
    "FileArtifact",
    "FormValue",
    "JsonArtifact",
    "RequestContext",
    "ResponseSink",
    "UPLOAD_REQUIRED",
    "UploadInput",
    "form_value",
    "upload_required",
]
