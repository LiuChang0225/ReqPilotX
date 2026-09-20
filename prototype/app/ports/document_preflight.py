from __future__ import annotations

from os import PathLike
from typing import Protocol, runtime_checkable

from app.domain.extraction.preflight import DocumentPreflightReport


@runtime_checkable
class DocumentPreflightPort(Protocol):
    """Read-only boundary for validating an original document before parsing."""

    def inspect(self, source_path: str | PathLike[str]) -> DocumentPreflightReport:
        ...
