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

from app.adapters.persistence.native_document_map_survey_checkpoints import (
    NativeDocumentMapSurveyCheckpointReceiptV1,
    NativeDocumentMapSurveyCheckpointStore,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCandidateV1,
    DocumentMapSurveyRangeCandidateV1,
    DocumentMapSurveyRangeKind,
    DocumentMapSurveyStatus,
    DocumentMapVisualCoverageMode,
    build_document_map_visual_coverage_receipt,
    compile_document_map_survey,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


class DocumentMapSurveyCheckpointTests(unittest.TestCase):
    def test_round_trips_private_compilation_with_content_free_receipt(self) -> None:
        compilation = _compilation()
        with tempfile.TemporaryDirectory() as raw:
            store = NativeDocumentMapSurveyCheckpointStore(
                object_store=LocalObjectStore(Path(raw)),
                project_id="project-one",
                document_id="document-one",
            )

            receipt = store.save(compilation)
            restored = store.load(receipt)

        self.assertTrue(receipt.content_free)
        self.assertEqual(restored, compilation)
        self.assertEqual(
            receipt.scope_ids,
            tuple(item.scope_id for item in compilation.scopes),
        )
        self.assertNotIn(compilation.document_summary, receipt.model_dump_json())

    def test_rejects_receipt_replayed_for_another_document(self) -> None:
        compilation = _compilation()
        with tempfile.TemporaryDirectory() as raw:
            object_store = LocalObjectStore(Path(raw))
            source_store = NativeDocumentMapSurveyCheckpointStore(
                object_store=object_store,
                project_id="project-one",
                document_id="document-one",
            )
            foreign_store = NativeDocumentMapSurveyCheckpointStore(
                object_store=object_store,
                project_id="project-one",
                document_id="document-two",
            )
            receipt = source_store.save(compilation)

            with self.assertRaisesRegex(ValueError, "object key drifted"):
                foreign_store.load(receipt)

    def test_receipt_tampering_fails_before_object_read(self) -> None:
        compilation = _compilation()
        with tempfile.TemporaryDirectory() as raw:
            store = NativeDocumentMapSurveyCheckpointStore(
                object_store=LocalObjectStore(Path(raw)),
                project_id="project-one",
                document_id="document-one",
            )
            payload = store.save(compilation).model_dump(mode="json")
            payload["candidate_sha256"] = "f" * 64

            with self.assertRaisesRegex(ValueError, "receipt SHA drifted"):
                NativeDocumentMapSurveyCheckpointReceiptV1.model_validate(payload)


def _compilation():  # type: ignore[no-untyped-def]
    kit = AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.15.0",
        manifest_sha256="4" * 64,
        bundle_sha256="3" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.3.0",
    )
    candidate = DocumentMapSurveyCandidateV1(
        run_id="agent-run-" + "1" * 32,
        source_sha256="2" * 64,
        kit_identity=kit,
        page_count=2,
        status=DocumentMapSurveyStatus.COMPLETE,
        document_summary="一份测试文档。",
        ranges=(
            DocumentMapSurveyRangeCandidateV1(
                proposal_key="front",
                kind=DocumentMapSurveyRangeKind.FRONT_MATTER,
                label="封面",
                page_numbers=(1,),
                evidence_refs=("page:1",),
                public_summary="封面范围。",
            ),
            DocumentMapSurveyRangeCandidateV1(
                proposal_key="body",
                kind=DocumentMapSurveyRangeKind.MAIN_DOCUMENT,
                label="正文",
                page_numbers=(2,),
                evidence_refs=("page:2",),
                public_summary="正文范围。",
            ),
        ),
    )
    visual = build_document_map_visual_coverage_receipt(
        source_sha256=candidate.source_sha256,
        page_count=2,
        mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
        event_summary={
            "schema_version": "reqpilotx-canonical-stage-event-ledger/v1",
            "visual_inspection": {
                "contact_sheet_opened": True,
                "high_resolution_page_numbers": [1, 2],
                "other_image_view_count": 0,
            },
        },
    )
    return compile_document_map_survey(
        candidate,
        visual_coverage=visual,
        expected_run_id=candidate.run_id,
        expected_source_sha256=candidate.source_sha256,
        expected_kit_identity=kit,
    )


if __name__ == "__main__":
    unittest.main()
