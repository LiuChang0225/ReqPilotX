from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from app.application.extraction.build_llm_shadow_preview import (
    LlmShadowPreviewProgressCancelled,
    _source_ir_adapter_with_visual_capture_progress,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextPageInput,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.domain.extraction.visual_table import (
    VisualTableCell,
    VisualTableCropInput,
    VisualTableSnapshot,
    stable_visual_table_cell_id,
    stable_visual_table_snapshot_id,
)
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from app.services.extraction_visual_routes import AuthorizedPdfVisualRuntime
from app.services import visual_text_snapshot_cache as snapshot_cache_module
from app.services.visual_text_snapshot_cache import (
    CachedVisualTableProvider,
    CachedVisualTextProvider,
    VisualTableSnapshotCacheRequiredMiss,
    VisualTextSnapshotCacheRequiredMiss,
    VisualTextSnapshotCache,
    VisualTextSnapshotCacheBinding,
)


PROJECT_ID = "project-a"
DOCUMENT_ID = "document-a"
PROVIDER_ID = "visual-provider"
PROVIDER_VERSION = "visual-provider/v1"


class VisualTextSnapshotCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary.name) / "storage"
        self.cache = VisualTextSnapshotCache(self.storage_root)
        self.page_input = _page_input()
        self.snapshot = _snapshot(self.page_input)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_visual_table_cache_is_exact_crop_model_and_plan_bound(self) -> None:
        crop_input = _table_crop_input()
        snapshot = _table_snapshot(crop_input)
        first_delegate = _RecordingTableProvider(snapshot)
        first = CachedVisualTableProvider(
            first_delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            processing_plan_sha256="a" * 64,
        )

        self.assertIs(first.parse_table(crop_input), snapshot)
        self.assertEqual(first_delegate.calls, 1)

        hit_delegate = _RecordingTableProvider(snapshot)
        hit = CachedVisualTableProvider(
            hit_delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            processing_plan_sha256="a" * 64,
        )
        self.assertEqual(hit.parse_table(crop_input), snapshot)
        self.assertEqual(hit_delegate.calls, 0)

        changed_plan_delegate = _RecordingTableProvider(snapshot)
        changed_plan = CachedVisualTableProvider(
            changed_plan_delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            processing_plan_sha256="b" * 64,
        )
        changed_plan.parse_table(crop_input)
        self.assertEqual(changed_plan_delegate.calls, 1)

    def test_visual_table_required_cache_miss_never_calls_remote(self) -> None:
        crop_input = _table_crop_input()
        delegate = _RecordingTableProvider(_table_snapshot(crop_input))
        provider = CachedVisualTableProvider(
            delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            processing_plan_sha256="a" * 64,
            cache_required_page_numbers=frozenset({crop_input.page_number}),
        )

        with self.assertRaises(VisualTableSnapshotCacheRequiredMiss):
            provider.parse_table(crop_input)

        self.assertEqual(delegate.calls, 0)

    def test_cache_hit_avoids_delegate_and_file_is_private(self) -> None:
        first_delegate = _RecordingProvider(self.snapshot)
        first = CachedVisualTextProvider(
            first_delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
        )

        self.assertIs(first.recognize_page(self.page_input), self.snapshot)
        self.assertEqual(first_delegate.calls, 1)

        hit_delegate = _RecordingProvider(self.snapshot)
        hit = CachedVisualTextProvider(
            hit_delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
        )
        restored = hit.recognize_page(self.page_input)

        self.assertEqual(restored, self.snapshot)
        self.assertEqual(hit_delegate.calls, 0)
        path = self.cache.entry_path(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=self.page_input,
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
        )
        self.assertTrue(path.resolve().is_relative_to(self.storage_root.resolve()))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            envelope["payload"]["binding"],
            {
                "snapshot_schema_version": "visual-text/v1",
                "source_sha256": self.page_input.source_sha256,
                "page_number": 1,
                "rendered_page_sha256": self.page_input.page_image_sha256,
                "page_bbox": list(self.page_input.page_bbox),
                "page_image_media_type": "image/png",
                "page_image_width_px": 20,
                "page_image_height_px": 30,
                "provider_id": PROVIDER_ID,
                "provider_version": PROVIDER_VERSION,
            },
        )

    def test_repair_cache_required_page_never_falls_through_to_delegate(self) -> None:
        delegate = _RecordingProvider(self.snapshot)
        provider = CachedVisualTextProvider(
            delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            cache_required_page_numbers=frozenset({1}),
            remote_allowed_page_numbers=frozenset({2}),
        )

        with self.assertRaises(VisualTextSnapshotCacheRequiredMiss):
            provider.recognize_page(self.page_input)

        self.assertEqual(delegate.calls, 0)

    def test_repair_remote_allowed_page_may_call_delegate_once(self) -> None:
        delegate = _RecordingProvider(self.snapshot)
        provider = CachedVisualTextProvider(
            delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            remote_allowed_page_numbers=frozenset({1}),
        )

        self.assertEqual(provider.recognize_page(self.page_input), self.snapshot)
        self.assertEqual(delegate.calls, 1)

    def test_sixty_pages_use_independent_bounded_entry_files(self) -> None:
        page_inputs: list[VisualTextPageInput] = []
        with mock.patch.object(
            self.cache,
            "load",
            side_effect=AssertionError(
                "saving one page must not read an accumulated document cache"
            ),
        ):
            for page_number in range(1, 61):
                page_input = _page_input(
                    page_number=page_number,
                    page_image_bytes=f"rendered-page-{page_number}".encode(),
                )
                page_inputs.append(page_input)
                self.assertTrue(
                    self.cache.save_snapshot(
                        project_id=PROJECT_ID,
                        document_id=DOCUMENT_ID,
                        page_input=page_input,
                        provider_id=PROVIDER_ID,
                        provider_version=PROVIDER_VERSION,
                        snapshot=_snapshot(page_input),
                    )
                )

        document_directory = self.cache.entry_path(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=page_inputs[0],
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
        ).parent
        entry_files = tuple(document_directory.glob("*.json"))
        self.assertEqual(len(entry_files), 60)
        self.assertTrue(
            all(
                item.stat().st_size <= snapshot_cache_module._MAX_CACHE_FILE_BYTES
                for item in entry_files
            )
        )

        first_path = self.cache.entry_path(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=page_inputs[0],
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
        )
        first_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(
            self.cache.load_snapshot(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                page_input=page_inputs[0],
                provider_id=PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
            )
        )
        self.assertIsNotNone(
            self.cache.load_snapshot(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                page_input=page_inputs[-1],
                provider_id=PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
            )
        )

    def test_every_exact_identity_mismatch_is_a_safe_miss(self) -> None:
        self.assertTrue(
            self.cache.save_snapshot(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                page_input=self.page_input,
                provider_id=PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
                snapshot=self.snapshot,
            )
        )
        other_source = _page_input(source_bytes=b"other source")
        other_page = self.page_input.model_copy(update={"page_number": 2})
        other_render = _page_input(page_image_bytes=b"other rendered png")

        mismatches = (
            (other_source, PROVIDER_ID, PROVIDER_VERSION),
            (other_page, PROVIDER_ID, PROVIDER_VERSION),
            (other_render, PROVIDER_ID, PROVIDER_VERSION),
            (self.page_input, "other-provider", PROVIDER_VERSION),
            (self.page_input, PROVIDER_ID, "visual-provider/v2"),
        )
        for page_input, provider_id, provider_version in mismatches:
            with self.subTest(
                page=page_input.page_number,
                source=page_input.source_sha256,
                rendered=page_input.page_image_sha256,
                provider_id=provider_id,
                provider_version=provider_version,
            ):
                self.assertIsNone(
                    self.cache.load_snapshot(
                        project_id=PROJECT_ID,
                        document_id=DOCUMENT_ID,
                        page_input=page_input,
                        provider_id=provider_id,
                        provider_version=provider_version,
                    )
                )

    def test_corrupt_or_schema_mismatched_entry_is_a_safe_miss(self) -> None:
        self.cache.save_snapshot(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=self.page_input,
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
            snapshot=self.snapshot,
        )
        path = self.cache.entry_path(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=self.page_input,
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
        )
        path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self._load())

        self.cache.save_snapshot(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=self.page_input,
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
            snapshot=self.snapshot,
        )
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["snapshot"]["schema_version"] = "visual-text/v0"
        _rewrite_integrity_checked_envelope(path, envelope)
        self.assertIsNone(self._load())

    def test_limits_fail_closed_without_reading_or_growing_the_document(self) -> None:
        with mock.patch.object(
            snapshot_cache_module,
            "_MAX_DOCUMENT_CACHE_ENTRIES",
            2,
        ):
            for page_number in (1, 2):
                page_input = _page_input(
                    page_number=page_number,
                    page_image_bytes=f"page-{page_number}".encode(),
                )
                self.assertTrue(
                    self.cache.save_snapshot(
                        project_id=PROJECT_ID,
                        document_id=DOCUMENT_ID,
                        page_input=page_input,
                        provider_id=PROVIDER_ID,
                        provider_version=PROVIDER_VERSION,
                        snapshot=_snapshot(page_input),
                    )
                )
            third = _page_input(page_number=3, page_image_bytes=b"page-3")
            self.assertFalse(
                self.cache.save_snapshot(
                    project_id=PROJECT_ID,
                    document_id=DOCUMENT_ID,
                    page_input=third,
                    provider_id=PROVIDER_ID,
                    provider_version=PROVIDER_VERSION,
                    snapshot=_snapshot(third),
                )
            )

        with mock.patch.object(
            snapshot_cache_module,
            "_MAX_SNAPSHOT_SERIALIZED_BYTES",
            1,
        ):
            oversized_snapshot_input = _page_input(
                page_number=4,
                page_image_bytes=b"page-4",
            )
            self.assertFalse(
                self.cache.save_snapshot(
                    project_id=PROJECT_ID,
                    document_id=DOCUMENT_ID,
                    page_input=oversized_snapshot_input,
                    provider_id=PROVIDER_ID,
                    provider_version=PROVIDER_VERSION,
                    snapshot=_snapshot(oversized_snapshot_input),
                )
            )

        with mock.patch.object(
            snapshot_cache_module,
            "_MAX_CACHE_FILE_BYTES",
            1_024,
        ):
            bounded_cache = VisualTextSnapshotCache(
                Path(self.temporary.name) / "bounded-storage"
            )
        oversized_path = bounded_cache.entry_path(
            project_id=PROJECT_ID,
            document_id="bounded-document",
            page_input=self.page_input,
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
        )
        oversized_path.parent.mkdir(parents=True)
        oversized_path.write_bytes(b"x" * 1_025)
        with mock.patch(
            "app.services.llm_extraction_result_cache.json.loads",
            side_effect=AssertionError("oversized cache files must not be parsed"),
        ):
            self.assertIsNone(
                bounded_cache.load_snapshot(
                    project_id=PROJECT_ID,
                    document_id="bounded-document",
                    page_input=self.page_input,
                    provider_id=PROVIDER_ID,
                    provider_version=PROVIDER_VERSION,
                )
            )

    def test_provider_failure_and_empty_result_are_not_cached(self) -> None:
        rejected = _RejectingProvider()
        provider = CachedVisualTextProvider(
            rejected,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
        )
        for _ in range(2):
            with self.assertRaises(VisualTextProviderRejected):
                provider.recognize_page(self.page_input)
        self.assertEqual(rejected.calls, 2)
        self.assertIsNone(self._load())

        empty_snapshot = _snapshot(self.page_input, include_observation=False)
        empty_delegate = _RecordingProvider(empty_snapshot)
        empty_provider = CachedVisualTextProvider(
            empty_delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
        )
        empty_provider.recognize_page(self.page_input)
        empty_provider.recognize_page(self.page_input)
        self.assertEqual(empty_delegate.calls, 2)
        self.assertIsNone(self._load())

    def test_page_specific_provider_version_controls_cache_identity(self) -> None:
        versioned = _VersionedProvider(
            self.snapshot,
            versions={1: PROVIDER_VERSION},
        )
        CachedVisualTextProvider(
            versioned,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
        ).recognize_page(self.page_input)

        hit_delegate = _VersionedProvider(
            self.snapshot,
            versions={1: PROVIDER_VERSION},
        )
        CachedVisualTextProvider(
            hit_delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
        ).recognize_page(self.page_input)
        self.assertEqual(hit_delegate.calls, 0)

        changed_snapshot = _snapshot(
            self.page_input,
            provider_version="visual-provider/page-v2",
        )
        changed_delegate = _VersionedProvider(
            changed_snapshot,
            versions={1: "visual-provider/page-v2"},
        )
        self.assertEqual(
            CachedVisualTextProvider(
                changed_delegate,
                cache=self.cache,
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            ).recognize_page(self.page_input),
            changed_snapshot,
        )
        self.assertEqual(changed_delegate.calls, 1)

    def test_purge_removes_snapshots_and_revokes_late_worker_write(self) -> None:
        self.assertTrue(
            self.cache.save_snapshot(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                page_input=self.page_input,
                provider_id=PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
                snapshot=self.snapshot,
            )
        )
        self.assertTrue(
            self.cache.purge_document(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            )
        )
        self.assertIsNone(self._load())
        self.assertFalse(
            self.cache.save_snapshot(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                page_input=self.page_input,
                provider_id=PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
                snapshot=self.snapshot,
            )
        )
        self.assertFalse(
            self.cache.entry_path(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                page_input=self.page_input,
                provider_id=PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
            ).exists()
        )

    def test_cache_write_failure_does_not_change_live_provider_result(self) -> None:
        delegate = _RecordingProvider(self.snapshot)
        provider = CachedVisualTextProvider(
            delegate,
            cache=self.cache,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
        )
        with (
            mock.patch.object(
                self.cache,
                "save_snapshot",
                side_effect=OSError("cache unavailable"),
            ),
            mock.patch(
                "app.services.visual_text_snapshot_cache.logger.warning"
            ) as warning,
        ):
            self.assertIs(provider.recognize_page(self.page_input), self.snapshot)
        warning.assert_called_once()

    def test_cache_hit_counts_progress_and_preserves_cancellation(self) -> None:
        self.cache.save_snapshot(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=self.page_input,
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
            snapshot=self.snapshot,
        )
        delegate = _RecordingProvider(self.snapshot)
        authorized = _authorized_runtime(
            delegate,
            source_sha256=self.page_input.source_sha256,
        )
        observed: list[tuple[int, int, int]] = []
        adapter = _source_ir_adapter_with_visual_capture_progress(
            authorized,
            lambda completed, upper_bound, page: observed.append(
                (completed, upper_bound, page)
            ),
            visual_text_snapshot_cache=VisualTextSnapshotCacheBinding(
                cache=self.cache,
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            ),
        )
        provider = adapter._visual_text_provider
        assert provider is not None

        self.assertEqual(provider.recognize_page(self.page_input), self.snapshot)
        self.assertEqual(delegate.calls, 0)
        self.assertEqual(observed, [(1, 2, 1)])

        cancelled = _source_ir_adapter_with_visual_capture_progress(
            authorized,
            lambda *_args: (_ for _ in ()).throw(
                LlmShadowPreviewProgressCancelled("job replaced")
            ),
            visual_text_snapshot_cache=VisualTextSnapshotCacheBinding(
                cache=self.cache,
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            ),
        )
        cancelled_provider = cancelled._visual_text_provider
        assert cancelled_provider is not None
        with self.assertRaisesRegex(
            LlmShadowPreviewProgressCancelled,
            "job replaced",
        ):
            cancelled_provider.recognize_page(self.page_input)
        self.assertEqual(delegate.calls, 0)

    def test_miss_cancelled_after_delegate_is_not_persisted(self) -> None:
        delegate = _RecordingProvider(self.snapshot)
        authorized = _authorized_runtime(
            delegate,
            source_sha256=self.page_input.source_sha256,
        )
        adapter = _source_ir_adapter_with_visual_capture_progress(
            authorized,
            lambda *_args: (_ for _ in ()).throw(
                LlmShadowPreviewProgressCancelled("document deleted")
            ),
            visual_text_snapshot_cache=VisualTextSnapshotCacheBinding(
                cache=self.cache,
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            ),
        )
        provider = adapter._visual_text_provider
        assert provider is not None

        with self.assertRaisesRegex(
            LlmShadowPreviewProgressCancelled,
            "document deleted",
        ):
            provider.recognize_page(self.page_input)
        self.assertEqual(delegate.calls, 1)
        self.assertIsNone(self._load())

    def _load(self) -> VisualTextSnapshot | None:
        return self.cache.load_snapshot(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            page_input=self.page_input,
            provider_id=PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
        )


class _RecordingProvider:
    provider_id = PROVIDER_ID
    provider_version = PROVIDER_VERSION

    def __init__(self, snapshot: VisualTextSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def recognize_page(self, _page_input: VisualTextPageInput) -> VisualTextSnapshot:
        self.calls += 1
        return self.snapshot


class _VersionedProvider(_RecordingProvider):
    def __init__(
        self,
        snapshot: VisualTextSnapshot,
        *,
        versions: dict[int, str],
    ) -> None:
        super().__init__(snapshot)
        self.versions = versions

    def provider_version_for_page(self, page_number: int) -> str:
        return self.versions[page_number]


class _RejectingProvider:
    provider_id = PROVIDER_ID
    provider_version = PROVIDER_VERSION

    def __init__(self) -> None:
        self.calls = 0

    def recognize_page(self, _page_input: VisualTextPageInput) -> VisualTextSnapshot:
        self.calls += 1
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
            "synthetic provider failure",
        )


class _RecordingTableProvider:
    provider_id = PROVIDER_ID
    provider_version = PROVIDER_VERSION

    def __init__(self, snapshot: VisualTableSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def parse_table(
        self,
        _crop_input: VisualTableCropInput,
    ) -> VisualTableSnapshot:
        self.calls += 1
        return self.snapshot


def _table_crop_input() -> VisualTableCropInput:
    source_bytes = b"%PDF-table-source"
    crop_bytes = b"table-crop-png"
    return VisualTableCropInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=2,
        page_bbox=(0.0, 0.0, 600.0, 800.0),
        table_source_node_id="node-" + "1" * 32,
        table_id="table-" + "2" * 32,
        table_index=0,
        table_bbox=(40.0, 80.0, 560.0, 320.0),
        crop_image_bytes=crop_bytes,
        crop_image_sha256=hashlib.sha256(crop_bytes).hexdigest(),
        crop_image_width_px=1040,
        crop_image_height_px=480,
    )


def _table_snapshot(crop_input: VisualTableCropInput) -> VisualTableSnapshot:
    snapshot_id = stable_visual_table_snapshot_id(
        source_sha256=crop_input.source_sha256,
        crop_image_sha256=crop_input.crop_image_sha256,
        provider_id=PROVIDER_ID,
        provider_version=PROVIDER_VERSION,
        page_number=crop_input.page_number,
        table_source_node_id=crop_input.table_source_node_id,
        table_id=crop_input.table_id,
        table_index=crop_input.table_index,
        table_bbox=crop_input.table_bbox,
        crop_image_width_px=crop_input.crop_image_width_px,
        crop_image_height_px=crop_input.crop_image_height_px,
    )
    cell = VisualTableCell(
        visual_table_cell_id=stable_visual_table_cell_id(
            visual_table_snapshot_id=snapshot_id,
            row=0,
            column=0,
            row_span=1,
            column_span=1,
        ),
        row=0,
        column=0,
        raw_text="grounded table text",
    )
    return VisualTableSnapshot(
        visual_table_snapshot_id=snapshot_id,
        source_sha256=crop_input.source_sha256,
        provider_id=PROVIDER_ID,
        provider_version=PROVIDER_VERSION,
        page_number=crop_input.page_number,
        page_bbox=crop_input.page_bbox,
        table_source_node_id=crop_input.table_source_node_id,
        table_id=crop_input.table_id,
        table_index=crop_input.table_index,
        table_bbox=crop_input.table_bbox,
        crop_image_sha256=crop_input.crop_image_sha256,
        crop_image_width_px=crop_input.crop_image_width_px,
        crop_image_height_px=crop_input.crop_image_height_px,
        row_count=1,
        column_count=1,
        cells=(cell,),
    )


def _page_input(
    *,
    source_bytes: bytes = b"%PDF-source",
    page_image_bytes: bytes = b"rendered-png",
    page_number: int = 1,
) -> VisualTextPageInput:
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=page_number,
        page_bbox=(0.0, 0.0, 20.0, 30.0),
        page_image_bytes=page_image_bytes,
        page_image_sha256=hashlib.sha256(page_image_bytes).hexdigest(),
        page_image_width_px=20,
        page_image_height_px=30,
    )


def _snapshot(
    page_input: VisualTextPageInput,
    *,
    provider_version: str = PROVIDER_VERSION,
    include_observation: bool = True,
) -> VisualTextSnapshot:
    observations = ()
    if include_observation:
        bbox = (1.0, 1.0, 10.0, 4.0)
        observations = (
            VisualTextObservation(
                observation_id=stable_visual_text_observation_id(
                    source_sha256=page_input.source_sha256,
                    page_image_sha256=page_input.page_image_sha256,
                    provider_id=PROVIDER_ID,
                    provider_version=provider_version,
                    page_number=page_input.page_number,
                    observation_index=0,
                    bbox=bbox,
                ),
                observation_index=0,
                source_sha256=page_input.source_sha256,
                page_number=page_input.page_number,
                bbox=bbox,
                raw_text="grounded text",
                confidence=0.9,
                provider_id=PROVIDER_ID,
                provider_version=provider_version,
                page_image_sha256=page_input.page_image_sha256,
            ),
        )
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=page_input.source_sha256,
            page_image_sha256=page_input.page_image_sha256,
            provider_id=PROVIDER_ID,
            provider_version=provider_version,
            page_number=page_input.page_number,
            page_bbox=page_input.page_bbox,
            page_image_width_px=page_input.page_image_width_px,
            page_image_height_px=page_input.page_image_height_px,
        ),
        source_sha256=page_input.source_sha256,
        provider_id=PROVIDER_ID,
        provider_version=provider_version,
        page_number=page_input.page_number,
        page_bbox=page_input.page_bbox,
        page_image_sha256=page_input.page_image_sha256,
        page_image_width_px=page_input.page_image_width_px,
        page_image_height_px=page_input.page_image_height_px,
        observations=observations,
    )


def _authorized_runtime(
    provider: _RecordingProvider,
    *,
    source_sha256: str,
) -> AuthorizedPdfVisualRuntime:
    selected_pages = frozenset({1, 2})
    original_adapter = PdfSourceIRAdapter(
        visual_text_provider=provider,
        visual_text_page_numbers=selected_pages,
    )
    runtime = SimpleNamespace(
        pdf_adapter=original_adapter,
        visual_text_provider=provider,
        visual_text_page_numbers=selected_pages,
        visual_table_provider=None,
        visual_table_page_numbers=frozenset(),
        route=SimpleNamespace(
            source_sha256=source_sha256,
            page_numbers=selected_pages,
        ),
    )
    return AuthorizedPdfVisualRuntime(
        provider_name="bailian",
        route=SimpleNamespace(
            source_sha256=source_sha256,
            route_id=f"pvr-{'3' * 32}",
            route_sha256="3" * 64,
        ),
        runtime=runtime,
    )


def _rewrite_integrity_checked_envelope(
    path: Path,
    envelope: dict[str, object],
) -> None:
    payload = envelope["payload"]
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["payload_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    path.write_text(json.dumps(envelope), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
