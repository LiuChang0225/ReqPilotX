from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int
    sha256: str
    media_type: str
    etag: str = ""


@runtime_checkable
class ObjectStore(Protocol):
    def put(
        self,
        *,
        key: str,
        source: BinaryIO,
        expected_sha256: str,
        media_type: str,
    ) -> StoredObject: ...

    def open(self, key: str) -> BinaryIO: ...

    def stat(self, key: str) -> StoredObject | None: ...

    def delete(self, key: str) -> bool: ...


__all__ = ["ObjectStore", "StoredObject"]
