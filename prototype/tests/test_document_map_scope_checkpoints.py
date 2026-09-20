from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import tempfile
import unittest

from app.adapters.persistence.native_document_map_scope_checkpoints import (
    NativeDocumentMapScopeCheckpointReceiptV1,
    NativeDocumentMapScopeCheckpointStore,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.document_map_scope import (
    compile_document_map_heading_scope,
)
from prototype.tests.test_document_map_scope import (
    _accepted_candidate,
    _chapter_requests,
    _fixtures,
)


class DocumentMapScopeCheckpointTests(unittest.TestCase):
    def test_round_trips_one_private_scope_without_claiming_complete_map(self) -> None:
        request, compilation = _scope_compilation()
        with tempfile.TemporaryDirectory() as raw:
            store = NativeDocumentMapScopeCheckpointStore(
                object_store=LocalObjectStore(Path(raw)),
                project_id="project-one",
                document_id="document-one",
            )
            receipt = store.save(
                request=request,
                compilation=compilation,
                kit_bundle_sha256="c" * 64,
            )
            restored = store.load(receipt)

        self.assertTrue(receipt.content_free)
        self.assertEqual(restored, compilation)
        self.assertFalse(restored.document_map_complete)
        self.assertTrue(restored.private_checkpoint_only)
        self.assertNotIn(compilation.public_summary, receipt.model_dump_json())

    def test_rejects_scope_receipt_replayed_for_another_document(self) -> None:
        request, compilation = _scope_compilation()
        with tempfile.TemporaryDirectory() as raw:
            object_store = LocalObjectStore(Path(raw))
            source_store = NativeDocumentMapScopeCheckpointStore(
                object_store=object_store,
                project_id="project-one",
                document_id="document-one",
            )
            foreign_store = NativeDocumentMapScopeCheckpointStore(
                object_store=object_store,
                project_id="project-one",
                document_id="document-two",
            )
            receipt = source_store.save(
                request=request,
                compilation=compilation,
                kit_bundle_sha256="c" * 64,
            )

            with self.assertRaisesRegex(ValueError, "object key drifted"):
                foreign_store.load(receipt)

    def test_receipt_tampering_is_rejected_before_object_read(self) -> None:
        request, compilation = _scope_compilation()
        with tempfile.TemporaryDirectory() as raw:
            store = NativeDocumentMapScopeCheckpointStore(
                object_store=LocalObjectStore(Path(raw)),
                project_id="project-one",
                document_id="document-one",
            )
            payload = store.save(
                request=request,
                compilation=compilation,
                kit_bundle_sha256="c" * 64,
            ).model_dump(mode="json")
            payload["candidate_sha256"] = "f" * 64

            with self.assertRaisesRegex(ValueError, "receipt SHA drifted"):
                NativeDocumentMapScopeCheckpointReceiptV1.model_validate(payload)


def _scope_compilation():  # type: ignore[no-untyped-def]
    source, survey, visual, skeleton = _fixtures()
    request, _ = _chapter_requests(
        source=source,
        survey=survey,
        visual=visual,
        skeleton=skeleton,
    )
    compilation = compile_document_map_heading_scope(
        request=request,
        candidate=_accepted_candidate(request),
    )
    return request, compilation


if __name__ == "__main__":
    unittest.main()
