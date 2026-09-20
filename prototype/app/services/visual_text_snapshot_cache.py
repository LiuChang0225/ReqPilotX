"""Source-bound persistent cache for trusted per-page visual text snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Literal, TypeAlias

from pydantic import ValidationError

from app.domain.extraction.visual_table import (
    VISUAL_TABLE_SCHEMA_VERSION,
    VisualTableCropInput,
    VisualTableSnapshot,
)
from app.domain.source_visual_text import (
    VISUAL_TEXT_SCHEMA_VERSION,
    VisualTextPageInput,
    VisualTextSnapshot,
)
from app.ports.visual_table_provider import VisualTableProviderPort
from app.ports.visual_text_provider import VisualTextProviderPort
from app.services.llm_extraction_result_cache import (
    LlmExtractionResultCachePathError,
    _SourceBoundJsonFileCache,
)

VISUAL_TEXT_SNAPSHOT_CACHE_SCHEMA_VERSION = "visual-text-snapshot-cache/v1"
_VISUAL_TEXT_SNAPSHOT_CACHE_DIRECTORY_NAME = "visual_text_page_snapshots"
_MAX_SNAPSHOT_SERIALIZED_BYTES = 15 * 1024 * 1024
_MAX_CACHE_FILE_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENT_CACHE_ENTRIES = 256
_MAX_DOCUMENT_CACHE_BYTES = 512 * 1024 * 1024
_LOCK_REGISTRY_GUARD = Lock()
_DOCUMENT_LOCKS: dict[tuple[str, str, str], RLock] = {}
_ENTRY_LOCKS: dict[tuple[str, str, str, str], RLock] = {}
_REVOKED_DOCUMENT_BINDINGS: set[tuple[str, str, str]] = set()
logger = logging.getLogger(__name__)


class VisualTextSnapshotCacheRequiredMiss(RuntimeError):
    """A repair page was not allowed to fall through to a remote provider."""


class VisualTableSnapshotCacheRequiredMiss(RuntimeError):
    """A table crop was not allowed to fall through to a remote provider."""


@dataclass(frozen=True, slots=True)
class VisualTextProviderTelemetryV1:
    """Content-free facts for one page/provider recognition boundary."""

    page_number: int
    provider_id: str
    provider_version: str
    request_count: int
    latency_ms: int
    cache_hit: bool
    outcome: Literal["READY", "FAILED", "CACHE_HIT"]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "visual-text-provider-telemetry/v1",
            "page_number": self.page_number,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "request_count": self.request_count,
            "latency_ms": self.latency_ms,
            "cache_hit": self.cache_hit,
            "outcome": self.outcome,
        }


VisualTextProviderTelemetryCallback: TypeAlias = Callable[
    [VisualTextProviderTelemetryV1],
    None,
]


class VisualTextSnapshotCache(_SourceBoundJsonFileCache):
    """Persist only validated snapshots under an exact page/provider identity."""

    def __init__(self, storage_root: str | Path) -> None:
        super().__init__(
            storage_root,
            cache_directory_name=_VISUAL_TEXT_SNAPSHOT_CACHE_DIRECTORY_NAME,
            cache_schema_version=VISUAL_TEXT_SNAPSHOT_CACHE_SCHEMA_VERSION,
            max_file_bytes=_MAX_CACHE_FILE_BYTES,
        )

    def entry_path(
        self,
        *,
        project_id: str,
        document_id: str,
        page_input: VisualTextPageInput,
        provider_id: str,
        provider_version: str,
    ) -> Path:
        binding = _cache_entry_binding(
            page_input=page_input,
            provider_id=provider_id,
            provider_version=provider_version,
        )
        return self.cache_path(
            project_id=project_id,
            document_id=document_id,
            source_sha256=_cache_entry_key(binding),
        )

    def load_snapshot(
        self,
        *,
        project_id: str,
        document_id: str,
        page_input: VisualTextPageInput,
        provider_id: str,
        provider_version: str,
    ) -> VisualTextSnapshot | None:
        binding = _cache_entry_binding(
            page_input=page_input,
            provider_id=provider_id,
            provider_version=provider_version,
        )
        entry_key = _cache_entry_key(binding)
        document_binding = self._document_binding(
            project_id=project_id,
            document_id=document_id,
        )
        document_lock = _document_lock(document_binding)
        entry_lock = _entry_lock(document_binding, entry_key)
        with document_lock, entry_lock:
            if document_binding in _REVOKED_DOCUMENT_BINDINGS:
                return None
            payload = self.load(
                project_id=project_id,
                document_id=document_id,
                source_sha256=entry_key,
            )
            if not _valid_cache_payload(payload, expected_binding=binding):
                return None
            try:
                snapshot = VisualTextSnapshot.model_validate(payload["snapshot"])
            except (TypeError, ValueError, ValidationError):
                return None
            if not visual_text_snapshot_matches_input(
                snapshot,
                page_input=page_input,
                provider_id=provider_id,
                provider_version=provider_version,
            ):
                return None
            return snapshot

    def save_snapshot(
        self,
        *,
        project_id: str,
        document_id: str,
        page_input: VisualTextPageInput,
        provider_id: str,
        provider_version: str,
        snapshot: object,
    ) -> bool:
        if not visual_text_snapshot_matches_input(
            snapshot,
            page_input=page_input,
            provider_id=provider_id,
            provider_version=provider_version,
        ):
            return False
        assert isinstance(snapshot, VisualTextSnapshot)
        snapshot_payload = snapshot.model_dump(mode="json")
        if (
            len(_canonical_json_bytes(snapshot_payload))
            > _MAX_SNAPSHOT_SERIALIZED_BYTES
        ):
            return False
        binding = _cache_entry_binding(
            page_input=page_input,
            provider_id=provider_id,
            provider_version=provider_version,
        )
        entry_key = _cache_entry_key(binding)
        document_binding = self._document_binding(
            project_id=project_id,
            document_id=document_id,
        )
        payload = {
            "binding": binding,
            "snapshot": snapshot_payload,
        }
        target = self.cache_path(
            project_id=project_id,
            document_id=document_id,
            source_sha256=entry_key,
        )
        projected_file_bytes = _projected_cache_file_bytes(
            project_id=project_id,
            document_id=document_id,
            entry_key=entry_key,
            payload=payload,
        )
        if projected_file_bytes > _MAX_CACHE_FILE_BYTES:
            return False
        document_lock = _document_lock(document_binding)
        entry_lock = _entry_lock(document_binding, entry_key)
        with document_lock, entry_lock:
            if document_binding in _REVOKED_DOCUMENT_BINDINGS:
                return False
            entry_count, total_bytes, replaced_bytes = _document_cache_usage(
                target,
            )
            projected_entry_count = entry_count + (
                0 if replaced_bytes is not None else 1
            )
            projected_document_bytes = (
                total_bytes - (replaced_bytes or 0) + projected_file_bytes
            )
            if (
                projected_entry_count > _MAX_DOCUMENT_CACHE_ENTRIES
                or projected_document_bytes > _MAX_DOCUMENT_CACHE_BYTES
            ):
                return False
            self.save(
                project_id=project_id,
                document_id=document_id,
                source_sha256=entry_key,
                payload=payload,
            )
        return True

    def load_visual_table_snapshot(
        self,
        *,
        project_id: str,
        document_id: str,
        crop_input: VisualTableCropInput,
        provider_id: str,
        provider_version: str,
        processing_plan_sha256: str,
    ) -> VisualTableSnapshot | None:
        """Load one exact crop/model/plan-bound table snapshot."""

        binding = _visual_table_cache_entry_binding(
            crop_input=crop_input,
            provider_id=provider_id,
            provider_version=provider_version,
            processing_plan_sha256=processing_plan_sha256,
        )
        entry_key = _cache_entry_key(binding)
        document_binding = self._document_binding(
            project_id=project_id,
            document_id=document_id,
        )
        with _document_lock(document_binding), _entry_lock(
            document_binding,
            entry_key,
        ):
            if document_binding in _REVOKED_DOCUMENT_BINDINGS:
                return None
            payload = self.load(
                project_id=project_id,
                document_id=document_id,
                source_sha256=entry_key,
            )
            if not _valid_cache_payload(payload, expected_binding=binding):
                return None
            try:
                snapshot = VisualTableSnapshot.model_validate(payload["snapshot"])
            except (TypeError, ValueError, ValidationError):
                return None
            if not visual_table_snapshot_matches_input(
                snapshot,
                crop_input=crop_input,
                provider_id=provider_id,
                provider_version=provider_version,
            ):
                return None
            return snapshot

    def save_visual_table_snapshot(
        self,
        *,
        project_id: str,
        document_id: str,
        crop_input: VisualTableCropInput,
        provider_id: str,
        provider_version: str,
        processing_plan_sha256: str,
        snapshot: object,
    ) -> bool:
        """Persist only a fully validated exact-crop table snapshot."""

        if not visual_table_snapshot_matches_input(
            snapshot,
            crop_input=crop_input,
            provider_id=provider_id,
            provider_version=provider_version,
        ):
            return False
        assert isinstance(snapshot, VisualTableSnapshot)
        snapshot_payload = snapshot.model_dump(mode="json")
        if (
            len(_canonical_json_bytes(snapshot_payload))
            > _MAX_SNAPSHOT_SERIALIZED_BYTES
        ):
            return False
        binding = _visual_table_cache_entry_binding(
            crop_input=crop_input,
            provider_id=provider_id,
            provider_version=provider_version,
            processing_plan_sha256=processing_plan_sha256,
        )
        entry_key = _cache_entry_key(binding)
        document_binding = self._document_binding(
            project_id=project_id,
            document_id=document_id,
        )
        payload = {"binding": binding, "snapshot": snapshot_payload}
        target = self.cache_path(
            project_id=project_id,
            document_id=document_id,
            source_sha256=entry_key,
        )
        projected_file_bytes = _projected_cache_file_bytes(
            project_id=project_id,
            document_id=document_id,
            entry_key=entry_key,
            payload=payload,
        )
        if projected_file_bytes > _MAX_CACHE_FILE_BYTES:
            return False
        with _document_lock(document_binding), _entry_lock(
            document_binding,
            entry_key,
        ):
            if document_binding in _REVOKED_DOCUMENT_BINDINGS:
                return False
            entry_count, total_bytes, replaced_bytes = _document_cache_usage(target)
            projected_entry_count = entry_count + (
                0 if replaced_bytes is not None else 1
            )
            projected_document_bytes = (
                total_bytes - (replaced_bytes or 0) + projected_file_bytes
            )
            if (
                projected_entry_count > _MAX_DOCUMENT_CACHE_ENTRIES
                or projected_document_bytes > _MAX_DOCUMENT_CACHE_BYTES
            ):
                return False
            self.save(
                project_id=project_id,
                document_id=document_id,
                source_sha256=entry_key,
                payload=payload,
            )
        return True

    def purge_document(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> bool:
        document_binding = self._document_binding(
            project_id=project_id,
            document_id=document_id,
        )
        with _document_lock(document_binding):
            # A process-local worker may return from a remote call after the
            # deletion path completes. Revocation keeps that late worker from
            # recreating the deleted document's cache.
            _REVOKED_DOCUMENT_BINDINGS.add(document_binding)
            return super().purge_document(
                project_id=project_id,
                document_id=document_id,
            )

    def _document_binding(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> tuple[str, str, str]:
        return (str(self.storage_root), project_id, document_id)


class CachedVisualTextProvider:
    """Read-through provider wrapper that preserves delegate rejection semantics."""

    def __init__(
        self,
        delegate: VisualTextProviderPort,
        *,
        cache: VisualTextSnapshotCache,
        project_id: str,
        document_id: str,
        cache_hit_callback: Callable[[int], None] | None = None,
        telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
        cache_required_page_numbers: frozenset[int] = frozenset(),
        remote_allowed_page_numbers: frozenset[int] | None = None,
    ) -> None:
        self.provider_id = str(delegate.provider_id)
        self.provider_version = str(delegate.provider_version)
        self._delegate = delegate
        self._cache = cache
        self._project_id = project_id
        self._document_id = document_id
        self._cache_hit_callback = cache_hit_callback
        self._telemetry_callback = telemetry_callback
        self._cache_required_page_numbers = cache_required_page_numbers
        self._remote_allowed_page_numbers = remote_allowed_page_numbers

    def provider_version_for_page(self, page_number: int) -> str:
        version_for_page = getattr(self._delegate, "provider_version_for_page", None)
        if callable(version_for_page):
            return str(version_for_page(page_number))
        return self.provider_version

    def recognize_page(
        self,
        page_input: VisualTextPageInput,
    ) -> VisualTextSnapshot:
        started_at = time.monotonic()
        provider_version = self.provider_version_for_page(page_input.page_number)
        snapshot = self._cache.load_snapshot(
            project_id=self._project_id,
            document_id=self._document_id,
            page_input=page_input,
            provider_id=self.provider_id,
            provider_version=provider_version,
        )
        if snapshot is not None:
            notify_visual_text_provider_telemetry(
                self._telemetry_callback,
                VisualTextProviderTelemetryV1(
                    page_number=page_input.page_number,
                    provider_id=self.provider_id,
                    provider_version=provider_version,
                    request_count=0,
                    latency_ms=max(
                        0,
                        round((time.monotonic() - started_at) * 1_000),
                    ),
                    cache_hit=True,
                    outcome="CACHE_HIT",
                ),
            )
            if self._cache_hit_callback is not None:
                self._cache_hit_callback(page_input.page_number)
            return snapshot

        if page_input.page_number in self._cache_required_page_numbers or (
            self._remote_allowed_page_numbers is not None
            and page_input.page_number not in self._remote_allowed_page_numbers
        ):
            raise VisualTextSnapshotCacheRequiredMiss(
                "The frozen visual repair cache entry is unavailable."
            )

        # Delegate failures and cancellation cross this boundary unchanged.
        snapshot = self._delegate.recognize_page(page_input)
        try:
            self._cache.save_snapshot(
                project_id=self._project_id,
                document_id=self._document_id,
                page_input=page_input,
                provider_id=self.provider_id,
                provider_version=provider_version,
                snapshot=snapshot,
            )
        except Exception:
            # Persistence is an optimization. A trusted live snapshot must
            # retain the adapter's existing acceptance/rejection semantics if
            # the cache is unavailable.
            logger.warning(
                "Visual text snapshot cache write failed. "
                "project_id=%s document_id=%s page_number=%s",
                self._project_id,
                self._document_id,
                page_input.page_number,
                exc_info=True,
            )
        return snapshot


class CachedVisualTableProvider:
    """Read through only exact source/page/crop/model/plan cache entries."""

    def __init__(
        self,
        delegate: VisualTableProviderPort,
        *,
        cache: VisualTextSnapshotCache,
        project_id: str,
        document_id: str,
        processing_plan_sha256: str,
        cache_required_page_numbers: frozenset[int] = frozenset(),
        remote_allowed_page_numbers: frozenset[int] | None = None,
    ) -> None:
        self.provider_id = str(delegate.provider_id)
        self.provider_version = str(delegate.provider_version)
        self._delegate = delegate
        self._cache = cache
        self._project_id = project_id
        self._document_id = document_id
        self._processing_plan_sha256 = processing_plan_sha256
        self._cache_required_page_numbers = cache_required_page_numbers
        self._remote_allowed_page_numbers = remote_allowed_page_numbers

    def parse_table(self, crop_input: VisualTableCropInput) -> VisualTableSnapshot:
        snapshot = self._cache.load_visual_table_snapshot(
            project_id=self._project_id,
            document_id=self._document_id,
            crop_input=crop_input,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            processing_plan_sha256=self._processing_plan_sha256,
        )
        if snapshot is not None:
            return snapshot
        if crop_input.page_number in self._cache_required_page_numbers or (
            self._remote_allowed_page_numbers is not None
            and crop_input.page_number not in self._remote_allowed_page_numbers
        ):
            raise VisualTableSnapshotCacheRequiredMiss(
                "The frozen visual table cache entry is unavailable."
            )
        snapshot = self._delegate.parse_table(crop_input)
        try:
            self._cache.save_visual_table_snapshot(
                project_id=self._project_id,
                document_id=self._document_id,
                crop_input=crop_input,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                processing_plan_sha256=self._processing_plan_sha256,
                snapshot=snapshot,
            )
        except Exception:
            logger.warning(
                "Visual table snapshot cache write failed. "
                "project_id=%s document_id=%s page_number=%s table_index=%s",
                self._project_id,
                self._document_id,
                crop_input.page_number,
                crop_input.table_index,
                exc_info=True,
            )
        return snapshot


@dataclass(frozen=True, slots=True)
class VisualTextSnapshotCacheBinding:
    """Bind one cache namespace to a project/document without source text."""

    cache: VisualTextSnapshotCache
    project_id: str
    document_id: str
    cache_required_page_numbers: frozenset[int] = frozenset()
    remote_allowed_page_numbers: frozenset[int] | None = None

    def wrap(
        self,
        delegate: VisualTextProviderPort,
        *,
        cache_hit_callback: Callable[[int], None] | None = None,
        telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
    ) -> CachedVisualTextProvider:
        return CachedVisualTextProvider(
            delegate,
            cache=self.cache,
            project_id=self.project_id,
            document_id=self.document_id,
            cache_hit_callback=cache_hit_callback,
            telemetry_callback=telemetry_callback,
            cache_required_page_numbers=self.cache_required_page_numbers,
            remote_allowed_page_numbers=self.remote_allowed_page_numbers,
        )

    def wrap_visual_table(
        self,
        delegate: VisualTableProviderPort,
        *,
        processing_plan_sha256: str,
    ) -> CachedVisualTableProvider:
        return CachedVisualTableProvider(
            delegate,
            cache=self.cache,
            project_id=self.project_id,
            document_id=self.document_id,
            processing_plan_sha256=processing_plan_sha256,
            cache_required_page_numbers=self.cache_required_page_numbers,
            remote_allowed_page_numbers=self.remote_allowed_page_numbers,
        )


def notify_visual_text_provider_telemetry(
    callback: VisualTextProviderTelemetryCallback | None,
    event: VisualTextProviderTelemetryV1,
) -> None:
    """Keep best-effort observability outside provider acceptance semantics."""

    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        logger.warning(
            "Visual text provider telemetry callback failed. "
            "provider_id=%s page_number=%s",
            event.provider_id,
            event.page_number,
        )


def visual_text_snapshot_matches_input(
    snapshot: object,
    *,
    page_input: VisualTextPageInput,
    provider_id: str,
    provider_version: str,
) -> bool:
    """Revalidate snapshot schema, stable IDs, page facts, and provider identity."""

    if not isinstance(snapshot, VisualTextSnapshot):
        return False
    try:
        validated = VisualTextSnapshot.model_validate(
            snapshot.model_dump(mode="json"),
        )
    except (TypeError, ValueError, ValidationError):
        return False
    return (
        validated.schema_version == VISUAL_TEXT_SCHEMA_VERSION
        and bool(validated.observations)
        and validated.source_sha256 == page_input.source_sha256
        and validated.page_number == page_input.page_number
        and validated.page_bbox == page_input.page_bbox
        and validated.page_image_sha256 == page_input.page_image_sha256
        and validated.page_image_media_type == page_input.page_image_media_type
        and validated.page_image_width_px == page_input.page_image_width_px
        and validated.page_image_height_px == page_input.page_image_height_px
        and validated.provider_id == provider_id
        and validated.provider_version == provider_version
    )


def visual_table_snapshot_matches_input(
    snapshot: object,
    *,
    crop_input: VisualTableCropInput,
    provider_id: str,
    provider_version: str,
) -> bool:
    """Revalidate exact crop topology, stable IDs, and provider identity."""

    if not isinstance(snapshot, VisualTableSnapshot):
        return False
    try:
        validated = VisualTableSnapshot.model_validate(
            snapshot.model_dump(mode="json"),
        )
    except (TypeError, ValueError, ValidationError):
        return False
    return (
        validated.schema_version == VISUAL_TABLE_SCHEMA_VERSION
        and validated.source_sha256 == crop_input.source_sha256
        and validated.page_number == crop_input.page_number
        and validated.page_bbox == crop_input.page_bbox
        and validated.table_source_node_id == crop_input.table_source_node_id
        and validated.table_id == crop_input.table_id
        and validated.table_index == crop_input.table_index
        and validated.table_bbox == crop_input.table_bbox
        and validated.crop_image_sha256 == crop_input.crop_image_sha256
        and validated.crop_image_media_type == crop_input.crop_image_media_type
        and validated.crop_image_width_px == crop_input.crop_image_width_px
        and validated.crop_image_height_px == crop_input.crop_image_height_px
        and validated.render_scale == crop_input.render_scale
        and validated.annotations_included == crop_input.annotations_included
        and validated.provider_id == provider_id
        and validated.provider_version == provider_version
    )


def _cache_entry_binding(
    *,
    page_input: VisualTextPageInput,
    provider_id: str,
    provider_version: str,
) -> dict[str, Any]:
    return {
        "snapshot_schema_version": VISUAL_TEXT_SCHEMA_VERSION,
        "source_sha256": page_input.source_sha256,
        "page_number": page_input.page_number,
        "rendered_page_sha256": page_input.page_image_sha256,
        "page_bbox": list(page_input.page_bbox),
        "page_image_media_type": page_input.page_image_media_type,
        "page_image_width_px": page_input.page_image_width_px,
        "page_image_height_px": page_input.page_image_height_px,
        "provider_id": provider_id,
        "provider_version": provider_version,
    }


def _visual_table_cache_entry_binding(
    *,
    crop_input: VisualTableCropInput,
    provider_id: str,
    provider_version: str,
    processing_plan_sha256: str,
) -> dict[str, Any]:
    return {
        "snapshot_schema_version": VISUAL_TABLE_SCHEMA_VERSION,
        "processing_plan_sha256": processing_plan_sha256,
        "source_sha256": crop_input.source_sha256,
        "page_number": crop_input.page_number,
        "page_bbox": list(crop_input.page_bbox),
        "table_source_node_id": crop_input.table_source_node_id,
        "table_id": crop_input.table_id,
        "table_index": crop_input.table_index,
        "table_bbox": list(crop_input.table_bbox),
        "crop_image_sha256": crop_input.crop_image_sha256,
        "crop_image_media_type": crop_input.crop_image_media_type,
        "crop_image_width_px": crop_input.crop_image_width_px,
        "crop_image_height_px": crop_input.crop_image_height_px,
        "render_scale": crop_input.render_scale,
        "annotations_included": crop_input.annotations_included,
        "provider_id": provider_id,
        "provider_version": provider_version,
    }


def _cache_entry_key(binding: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        dict(binding),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _valid_cache_payload(
    payload: object,
    *,
    expected_binding: Mapping[str, Any],
) -> bool:
    return bool(
        isinstance(payload, dict)
        and set(payload) == {"binding", "snapshot"}
        and payload.get("binding") == dict(expected_binding)
        and isinstance(payload.get("snapshot"), dict)
    )


def _document_lock(binding: tuple[str, str, str]) -> RLock:
    with _LOCK_REGISTRY_GUARD:
        return _DOCUMENT_LOCKS.setdefault(binding, RLock())


def _entry_lock(
    document_binding: tuple[str, str, str],
    entry_key: str,
) -> RLock:
    binding = (*document_binding, entry_key)
    with _LOCK_REGISTRY_GUARD:
        return _ENTRY_LOCKS.setdefault(binding, RLock())


def _document_cache_usage(target: Path) -> tuple[int, int, int | None]:
    directory = target.parent
    if not directory.exists():
        return 0, 0, None
    if directory.is_symlink() or not directory.is_dir():
        raise LlmExtractionResultCachePathError(
            "visual snapshot cache path is not a safe directory"
        )
    entry_count = 0
    total_bytes = 0
    replaced_bytes: int | None = None
    for child in directory.iterdir():
        child_status = child.lstat()
        if child.is_symlink() or not stat.S_ISREG(child_status.st_mode):
            raise LlmExtractionResultCachePathError(
                "visual snapshot cache contains an unsafe entry"
            )
        entry_count += 1
        total_bytes += child_status.st_size
        if child == target:
            replaced_bytes = child_status.st_size
    return entry_count, total_bytes, replaced_bytes


def _projected_cache_file_bytes(
    *,
    project_id: str,
    document_id: str,
    entry_key: str,
    payload: Mapping[str, Any],
) -> int:
    normalized_payload = json.loads(_canonical_json_bytes(dict(payload)))
    payload_bytes = _canonical_json_bytes(normalized_payload)
    envelope = {
        "schema_version": VISUAL_TEXT_SNAPSHOT_CACHE_SCHEMA_VERSION,
        "binding": {
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": entry_key,
        },
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payload": normalized_payload,
    }
    return len(_canonical_json_bytes(envelope)) + 1


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "VISUAL_TEXT_SNAPSHOT_CACHE_SCHEMA_VERSION",
    "CachedVisualTableProvider",
    "CachedVisualTextProvider",
    "VisualTextProviderTelemetryCallback",
    "VisualTextProviderTelemetryV1",
    "VisualTextSnapshotCache",
    "VisualTextSnapshotCacheBinding",
    "VisualTableSnapshotCacheRequiredMiss",
    "notify_visual_text_provider_telemetry",
    "visual_table_snapshot_matches_input",
    "visual_text_snapshot_matches_input",
]
