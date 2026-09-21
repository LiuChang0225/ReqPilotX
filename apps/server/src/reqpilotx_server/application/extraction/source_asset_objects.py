from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from reqpilotx_server import db
from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, RUNTIME_STATE
from reqpilotx_server.domain.extraction.source_ir import SourceAssetRef
from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend
from reqpilotx_server.ports.object_store import ObjectStore


SourceAssetPublisher = Callable[[SourceAssetRef, bytes], None]
SourceAssetLoader = Callable[[str, str, int], bytes | None]
SourceAssetMetadataPreparer = Callable[..., None]
SourceAssetMetadataConfirmer = Callable[..., bool]
SOURCE_ASSET_PUBLICATION_RUNTIME_VERSION = "source-asset-publication/v1"
EXTRACTION_RESULT_ARTIFACT_PUBLICATION_RUNTIME_VERSION = (
    "extraction-result-artifact-publication/v1"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceAssetPublicationIdentity:
    project_id: str
    document_id: str
    source_sha256: str
    job_id: str
    attempt_generation: int
    worker_id: str


_CURRENT_SOURCE_ASSET_PUBLICATION: ContextVar[
    SourceAssetPublicationIdentity | None
] = ContextVar("reqpilotx_source_asset_publication", default=None)


def build_source_asset_publisher(
    object_store: ObjectStore,
    *,
    register_metadata: Callable[..., None] | None = None,
    prepare_metadata: SourceAssetMetadataPreparer | None = None,
    confirm_metadata: SourceAssetMetadataConfirmer | None = None,
) -> SourceAssetPublisher:
    """Build a fail-closed publisher for immutable SourceIR asset bytes."""

    def publish(asset: SourceAssetRef, content_bytes: bytes) -> None:
        expected_sha256 = asset.content_sha256
        if (
            not content_bytes
            or hashlib.sha256(content_bytes).hexdigest() != expected_sha256
        ):
            raise RuntimeError("source asset payload identity mismatch")

        existing = object_store.stat(asset.storage_key)
        if prepare_metadata is not None:
            # Production ownership is registered before bytes are created.  A
            # metadata/ref transaction failure therefore cannot orphan a new
            # object, while a later object failure remains discoverable via
            # the durable staging reference.
            prepare_metadata(
                storage_key=asset.storage_key,
                sha256=expected_sha256,
                size_bytes=len(content_bytes),
                media_type=asset.media_type,
                object_kind="source-ir-asset",
            )
            # Publication/deletion is fenced in PostgreSQL, but the initial
            # stat may precede that lock. Re-read after registration so a
            # concurrent GC cannot leave a stale "already exists" decision.
            existing = object_store.stat(asset.storage_key)
        if existing is None:
            object_store.put(
                key=asset.storage_key,
                source=io.BytesIO(content_bytes),
                expected_sha256=expected_sha256,
                media_type=asset.media_type,
            )

        # A put receipt alone is not durable evidence.  Re-read object metadata
        # so local and remote stores share the same post-publication contract.
        stored = object_store.stat(asset.storage_key)
        if (
            stored is None
            or stored.sha256 != expected_sha256
            or int(stored.size) != len(content_bytes)
        ):
            raise RuntimeError(
                "source asset storage key is bound to different bytes"
            )
        if confirm_metadata is not None and not confirm_metadata(
            storage_key=asset.storage_key,
            sha256=expected_sha256,
            size_bytes=len(content_bytes),
            media_type=asset.media_type,
        ):
            _delete_unreferenced_source_asset_objects(
                object_store,
                (asset.storage_key,),
            )
            raise RuntimeError("source asset publication lost its job lease fence")
        if register_metadata is not None:
            register_metadata(
                storage_key=asset.storage_key,
                sha256=expected_sha256,
                size_bytes=len(content_bytes),
                media_type=asset.media_type,
                object_kind="source-ir-asset",
            )

    return publish


class SourceAssetPublicationAttempt:
    """One job-generation staging manifest with explicit seal/abort."""

    def __init__(
        self,
        identity: SourceAssetPublicationIdentity,
        object_store: ObjectStore,
    ) -> None:
        self.identity = identity
        self.object_store = object_store
        self._sealed = False

    def seal(self) -> None:
        """Validate publication; queue.succeed atomically promotes the refs."""

        if self._sealed:
            return
        db.seal_source_asset_publication_attempt(
            project_id=self.identity.project_id,
            document_id=self.identity.document_id,
            source_sha256=self.identity.source_sha256,
            job_id=self.identity.job_id,
            attempt_generation=self.identity.attempt_generation,
            worker_id=self.identity.worker_id,
        )
        self._sealed = True

    def abort(self) -> None:
        if self._sealed:
            return
        candidates = db.abort_source_asset_publication_attempt(
            job_id=self.identity.job_id,
            attempt_generation=self.identity.attempt_generation,
        )
        _delete_unreferenced_source_asset_objects(
            self.object_store,
            candidates,
        )


@contextmanager
def source_asset_publication_attempt(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
) -> Iterator[SourceAssetPublicationAttempt]:
    """Bind asset publication to one durable extraction delivery."""

    container = RUNTIME_STATE.container
    if (
        container is None
        or RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL
    ):
        raise RuntimeError(
            "Durable source asset publication requires PostgreSQL object storage."
        )
    recover_stale_source_asset_publications()
    identity = SourceAssetPublicationIdentity(
        project_id=str(project_id or "").strip(),
        document_id=str(document_id or "").strip(),
        source_sha256=str(source_sha256 or "").strip().lower(),
        job_id=str(job_id or "").strip(),
        attempt_generation=int(attempt_generation),
        worker_id=str(worker_id or "").strip(),
    )
    attempt = SourceAssetPublicationAttempt(identity, container.object_store)
    token = _CURRENT_SOURCE_ASSET_PUBLICATION.set(identity)
    try:
        yield attempt
    finally:
        _CURRENT_SOURCE_ASSET_PUBLICATION.reset(token)
        if not attempt._sealed:
            try:
                attempt.abort()
            except Exception:
                # A durable staging row is intentionally recoverable by the
                # worker maintenance scan; do not mask the extraction error
                # that caused this attempt to abort.
                logger.warning(
                    "Source asset publication abort was deferred. job_id=%s "
                    "generation=%s",
                    identity.job_id,
                    identity.attempt_generation,
                    exc_info=True,
                )


def recover_stale_source_asset_publications() -> int:
    """Reap dead job generations and retry bounded zero-ref object GC."""

    container = RUNTIME_STATE.container
    if (
        container is None
        or RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL
    ):
        return 0
    candidates = set(db.reap_stale_source_asset_staging_references())
    candidates.update(db.reap_stale_document_extraction_page_references())
    candidates.update(db.list_unreferenced_managed_source_asset_objects())
    deleted = _delete_unreferenced_source_asset_objects(
        container.object_store,
        tuple(sorted(candidates)),
    )
    artifact_candidates = set(
        db.reap_stale_extraction_result_artifact_references()
    )
    artifact_candidates.update(
        db.reap_stale_extraction_result_artifact_cas_operations()
    )
    artifact_candidates.update(
        db.list_unreferenced_extraction_result_artifacts()
    )
    deleted += delete_unreferenced_extraction_result_artifacts(
        tuple(sorted(artifact_candidates))
    )
    return deleted


def delete_unreferenced_extraction_result_artifacts(
    storage_keys: tuple[str, ...],
) -> int:
    container = RUNTIME_STATE.container
    if container is None:
        return 0
    deleted = 0
    for storage_key in dict.fromkeys(storage_keys):
        try:
            with db.claim_unreferenced_extraction_result_artifact_deletion(
                storage_key
            ) as allowed:
                if allowed:
                    container.object_store.delete(storage_key)
                    deleted += 1
        except Exception:
            logger.warning(
                "Extraction result artifact garbage collection was deferred. "
                "storage_key=%s",
                storage_key,
                exc_info=True,
            )
    return deleted


def release_source_asset_objects_after_document_delete(
    storage_keys: tuple[str, ...],
) -> int:
    """Delete document-released assets only after a global locked recheck."""

    container = RUNTIME_STATE.container
    if (
        container is None
        or RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL
    ):
        return 0
    return _delete_unreferenced_source_asset_objects(
        container.object_store,
        storage_keys,
    )


def _delete_unreferenced_source_asset_objects(
    object_store: ObjectStore,
    storage_keys: tuple[str, ...],
) -> int:
    deleted = 0
    for storage_key in dict.fromkeys(storage_keys):
        try:
            with db.claim_unreferenced_source_asset_object_deletion(
                storage_key
            ) as allowed:
                if not allowed:
                    continue
                object_store.delete(storage_key)
                deleted += 1
        except Exception:
            # Reference release has already committed. Keep metadata and let
            # the bounded recovery scan retry deletion without affecting the
            # successful extraction publication.
            logger.warning(
                "Source asset garbage collection was deferred. storage_key=%s",
                storage_key,
                exc_info=True,
            )
    return deleted


def build_source_asset_loader(object_store: ObjectStore) -> SourceAssetLoader:
    """Build a bounded loader that returns only hash-verified object bytes."""

    def load(
        storage_key: str,
        expected_sha256: str,
        max_bytes: int,
    ) -> bytes | None:
        normalized_sha256 = str(expected_sha256 or "").strip().lower()
        if (
            len(normalized_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in normalized_sha256
            )
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 1
        ):
            return None
        try:
            stored = object_store.stat(storage_key)
            if (
                stored is None
                or stored.sha256 != normalized_sha256
                or int(stored.size) < 1
                or int(stored.size) > max_bytes
            ):
                return None
            with object_store.open(storage_key) as stream:
                content_bytes = stream.read(int(stored.size) + 1)
        except Exception:
            return None
        if (
            len(content_bytes) != int(stored.size)
            or hashlib.sha256(content_bytes).hexdigest() != normalized_sha256
        ):
            return None
        return content_bytes

    return load


def runtime_source_asset_publisher() -> SourceAssetPublisher | None:
    """Return the runtime asset sink, or no sink outside an initialized reqpilotx_server.

    SourceIR adapters deliberately keep portable logical keys.  This
    application boundary is the only place where those keys become durable
    object-store claims, and it verifies the complete content identity before
    and after publication.
    """

    container = RUNTIME_STATE.container
    if container is None:
        return None
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        identity = _CURRENT_SOURCE_ASSET_PUBLICATION.get()
        if identity is None:
            # Ad-hoc previews may create an in-memory SourceIR, but production
            # object publication is legal only inside a fenced extraction
            # attempt. This avoids ownerless objects from read paths.
            return None

        def prepare_metadata(**values: object) -> None:
            db.register_source_asset_staging_reference(
                project_id=identity.project_id,
                document_id=identity.document_id,
                source_sha256=identity.source_sha256,
                job_id=identity.job_id,
                attempt_generation=identity.attempt_generation,
                worker_id=identity.worker_id,
                storage_key=str(values["storage_key"]),
                sha256=str(values["sha256"]),
                size_bytes=int(values["size_bytes"]),
                media_type=str(values["media_type"]),
            )

        def confirm_metadata(**values: object) -> bool:
            return db.confirm_source_asset_staging_reference(
                project_id=identity.project_id,
                document_id=identity.document_id,
                source_sha256=identity.source_sha256,
                job_id=identity.job_id,
                attempt_generation=identity.attempt_generation,
                storage_key=str(values["storage_key"]),
                sha256=str(values["sha256"]),
                size_bytes=int(values["size_bytes"]),
                media_type=str(values["media_type"]),
            )

        return build_source_asset_publisher(
            container.object_store,
            prepare_metadata=prepare_metadata,
            confirm_metadata=confirm_metadata,
        )
    return build_source_asset_publisher(
        container.object_store,
    )


def runtime_source_asset_loader() -> SourceAssetLoader | None:
    """Return the application-owned verified loader for export services."""

    container = RUNTIME_STATE.container
    if container is None:
        return None
    return build_source_asset_loader(container.object_store)


__all__ = [
    "SourceAssetLoader",
    "SourceAssetPublicationAttempt",
    "SourceAssetPublisher",
    "SOURCE_ASSET_PUBLICATION_RUNTIME_VERSION",
    "EXTRACTION_RESULT_ARTIFACT_PUBLICATION_RUNTIME_VERSION",
    "build_source_asset_loader",
    "build_source_asset_publisher",
    "recover_stale_source_asset_publications",
    "delete_unreferenced_extraction_result_artifacts",
    "release_source_asset_objects_after_document_delete",
    "runtime_source_asset_loader",
    "runtime_source_asset_publisher",
    "source_asset_publication_attempt",
]
