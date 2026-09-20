"""Neutral ownership port for SourceIR asset publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.extraction.source_ir import SourceAssetRef


class SourceAssetPublicationError(RuntimeError):
    pass


class SourceAssetPublicationConflict(SourceAssetPublicationError):
    pass


@dataclass(frozen=True, slots=True)
class SourceAssetPublicationIdentity:
    publication_id: str
    project_id: str
    sor_document_id: str
    source_sha256: str
    created_at_utc: datetime


@runtime_checkable
class SourceAssetPublicationRepositoryPort(Protocol):
    def begin(self, identity: SourceAssetPublicationIdentity) -> None: ...

    def stage_asset(
        self,
        identity: SourceAssetPublicationIdentity,
        *,
        asset: SourceAssetRef,
        size_bytes: int,
        touched_at_utc: datetime,
    ) -> None: ...

    def validate_manifest(
        self,
        identity: SourceAssetPublicationIdentity,
        assets: tuple[SourceAssetRef, ...],
    ) -> None: ...

    def abort(self, publication_id: str) -> tuple[str, ...]: ...

    def reap_stale(self, *, stale_before_utc: datetime) -> tuple[str, ...]: ...

    def list_unreferenced(self, *, limit: int = 256) -> tuple[str, ...]: ...

    def forget_if_unreferenced(self, storage_key: str) -> bool: ...


__all__ = [
    "SourceAssetPublicationConflict",
    "SourceAssetPublicationError",
    "SourceAssetPublicationIdentity",
    "SourceAssetPublicationRepositoryPort",
]
