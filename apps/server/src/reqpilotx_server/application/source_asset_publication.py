"""Fenced publication and reference-safe cleanup for neutral SourceIR assets."""

from __future__ import annotations

import io
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from reqpilotx_server import db
from reqpilotx_server.adapters.persistence.sql_source_asset_publication import (
    SqlSourceAssetPublicationRepository,
)
from reqpilotx_server.domain.extraction.source_ir import SourceAssetRef
from reqpilotx_server.ports.object_store import ObjectStore
from reqpilotx_server.ports.source_asset_publication import (
    SourceAssetPublicationConflict,
    SourceAssetPublicationIdentity,
    SourceAssetPublicationRepositoryPort,
)


_SQLITE_ASSET_LIFECYCLE_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


class SourceAssetPublicationAttempt:
    """One document-version fence held from parser callback through DB commit."""

    def __init__(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        source_sha256: str,
        object_store: ObjectStore,
        repository: SourceAssetPublicationRepositoryPort | None = None,
        created_at_utc: datetime,
    ) -> None:
        self.identity = SourceAssetPublicationIdentity(
            publication_id=f"source-capture-publication-{uuid.uuid4().hex}",
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=source_sha256,
            created_at_utc=_aware_utc(created_at_utc),
        )
        self._object_store = object_store
        self._repository = repository or SqlSourceAssetPublicationRepository()
        self._sealed = False
        self._repository.begin(self.identity)

    def publish(self, asset: SourceAssetRef, content: bytes) -> None:
        if self._sealed:
            raise RuntimeError("source asset publication is already sealed")
        payload = bytes(content)
        if not payload:
            raise SourceAssetPublicationConflict("source assets must not be empty")
        import hashlib

        if hashlib.sha256(payload).hexdigest() != asset.content_sha256:
            raise SourceAssetPublicationConflict(
                "source asset bytes do not match SourceAssetRef"
            )
        with _SQLITE_ASSET_LIFECYCLE_LOCK:
            existing = self._object_store.stat(asset.storage_key)
            if existing is not None and (
                existing.sha256 != asset.content_sha256
                or existing.size != len(payload)
            ):
                raise SourceAssetPublicationConflict(
                    "source asset key is already bound to different bytes"
                )
            self._repository.stage_asset(
                self.identity,
                asset=asset,
                size_bytes=len(payload),
                touched_at_utc=datetime.now(timezone.utc),
            )
            if existing is None:
                self._object_store.put(
                    key=asset.storage_key,
                    source=io.BytesIO(payload),
                    expected_sha256=asset.content_sha256,
                    media_type=asset.media_type,
                )
            stored = self._object_store.stat(asset.storage_key)
            if stored is None or (
                stored.sha256 != asset.content_sha256 or stored.size != len(payload)
            ):
                raise SourceAssetPublicationConflict(
                    "source asset object publication was not durable"
                )

    def verify(self, assets: tuple[SourceAssetRef, ...]) -> None:
        self._repository.validate_manifest(self.identity, assets)
        for asset in assets:
            stored = self._object_store.stat(asset.storage_key)
            if stored is None or stored.sha256 != asset.content_sha256:
                raise SourceAssetPublicationConflict(
                    "SourceIR references an unavailable source asset object"
                )

    def seal(self) -> None:
        self._sealed = True

    def abort(self) -> None:
        if self._sealed:
            return
        released = self._repository.abort(self.identity.publication_id)
        _release_candidates(
            released,
            object_store=self._object_store,
            repository=self._repository,
        )
        self._sealed = True


def reap_stale_source_asset_publications(
    *,
    object_store: ObjectStore,
    repository: SourceAssetPublicationRepositoryPort | None = None,
    now_utc: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> int:
    store = repository or SqlSourceAssetPublicationRepository()
    released = store.reap_stale(
        stale_before_utc=_aware_utc(now_utc or datetime.now(timezone.utc)) - max_age
    )
    return _release_candidates(released, object_store=object_store, repository=store)


def release_source_asset_candidates(
    storage_keys: tuple[str, ...],
    *,
    object_store: ObjectStore,
    repository: SourceAssetPublicationRepositoryPort | None = None,
) -> int:
    return _release_candidates(
        storage_keys,
        object_store=object_store,
        repository=repository or SqlSourceAssetPublicationRepository(),
    )


def _release_candidates(
    storage_keys: tuple[str, ...],
    *,
    object_store: ObjectStore,
    repository: SourceAssetPublicationRepositoryPort,
) -> int:
    deleted = 0
    for key in tuple(dict.fromkeys(storage_keys)):
        if db.is_postgresql_runtime():
            try:
                with db.claim_unreferenced_source_asset_object_deletion(key) as claimed:
                    if not claimed:
                        continue
                    object_store.delete(key)
                    deleted += 1
            except Exception:
                # Reference release has already committed. Preserve metadata
                # and let the bounded recovery scan retry without making
                # customer document deletion falsely fail.
                logger.warning(
                    "Source-capture asset garbage collection was deferred. "
                    "storage_key=%s",
                    key,
                    exc_info=True,
                )
            continue
        try:
            with _SQLITE_ASSET_LIFECYCLE_LOCK:
                if key not in repository.list_unreferenced(limit=10_000):
                    continue
                object_store.delete(key)
                if repository.forget_if_unreferenced(key):
                    deleted += 1
        except Exception:
            logger.warning(
                "Source-capture asset garbage collection was deferred. "
                "storage_key=%s",
                key,
                exc_info=True,
            )
    return deleted


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source asset timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "SourceAssetPublicationAttempt",
    "reap_stale_source_asset_publications",
    "release_source_asset_candidates",
]
