"""Persistence port for immutable SourceIR snapshots."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot


class SourceIRRepositoryError(RuntimeError):
    """Base error raised by SourceIR snapshot repositories."""


class SourceIRConflictError(SourceIRRepositoryError):
    """The requested SourceIR identity already contains a different payload."""


@runtime_checkable
class SourceIRRepositoryPort(Protocol):
    """Append-only repository for validated SourceIR snapshots.

    ``append`` returns ``True`` when it inserts the snapshot and ``False`` for
    an idempotent replay of the exact canonical payload. Reusing a
    ``source_ir_id`` for different content must raise ``SourceIRConflictError``.
    """

    def append(self, snapshot: SourceIRSnapshot) -> bool:
        """Atomically append one immutable snapshot."""

    def get(self, source_ir_id: str) -> SourceIRSnapshot | None:
        """Return the snapshot with exactly this identity, if present."""

    def list_by_document_id(self, document_id: str) -> tuple[SourceIRSnapshot, ...]:
        """Return all snapshots for one document in deterministic order."""


__all__ = [
    "SourceIRConflictError",
    "SourceIRRepositoryError",
    "SourceIRRepositoryPort",
]
