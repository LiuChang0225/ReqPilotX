from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.extraction.semantic_provider import SemanticProviderRunRecord


class SemanticProviderRunRepositoryError(RuntimeError):
    pass


class SemanticProviderRunConflictError(SemanticProviderRunRepositoryError):
    pass


@runtime_checkable
class SemanticProviderRunRepositoryPort(Protocol):
    def append(self, run: SemanticProviderRunRecord) -> bool: ...

    def get(self, provider_run_id: str) -> SemanticProviderRunRecord | None: ...


__all__ = [
    "SemanticProviderRunConflictError",
    "SemanticProviderRunRepositoryError",
    "SemanticProviderRunRepositoryPort",
]
