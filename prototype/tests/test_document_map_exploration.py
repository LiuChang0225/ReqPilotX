from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCandidateV1,
    DocumentMapSurveyFindingV1,
    DocumentMapSurveyRangeCandidateV1,
    DocumentMapSurveyRangeKind,
    DocumentMapSurveyStatus,
    DocumentMapVisualCoverageMode,
    build_document_map_visual_coverage_receipt,
    compile_document_map_survey,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


class DocumentMapExplorationTests(unittest.TestCase):
    def test_compiles_agent_owned_page_ranges_with_full_visual_receipt(self) -> None:
        candidate = _candidate()
        receipt = build_document_map_visual_coverage_receipt(
            source_sha256=candidate.source_sha256,
            page_count=candidate.page_count,
            mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
            event_summary=_event_summary((1, 2, 3)),
        )

        compilation = compile_document_map_survey(
            candidate,
            visual_coverage=receipt,
            expected_run_id=candidate.run_id,
            expected_source_sha256=candidate.source_sha256,
            expected_kit_identity=candidate.kit_identity,
        )

        self.assertEqual(compilation.status, DocumentMapSurveyStatus.COMPLETE)
        self.assertEqual(
            tuple(item.page_numbers for item in compilation.scopes),
            ((1,), (2, 3)),
        )
        self.assertEqual(compilation.unresolved_scope_ids, ())
        self.assertEqual(
            compilation.visual_coverage_receipt_sha256,
            receipt.receipt_sha256,
        )
        replay = compile_document_map_survey(
            candidate,
            visual_coverage=receipt,
            expected_run_id=candidate.run_id,
            expected_source_sha256=candidate.source_sha256,
            expected_kit_identity=candidate.kit_identity,
        )
        self.assertEqual(replay.compilation_sha256, compilation.compilation_sha256)
        self.assertEqual(
            tuple(item.scope_id for item in replay.scopes),
            tuple(item.scope_id for item in compilation.scopes),
        )

    def test_rejects_host_or_agent_page_partition_gaps(self) -> None:
        payload = _candidate().model_dump(mode="json")
        payload["ranges"][1]["page_numbers"] = [3]
        payload["ranges"][1]["evidence_refs"] = ["page:3"]

        with self.assertRaisesRegex(ValueError, "partition every page exactly once"):
            DocumentMapSurveyCandidateV1.model_validate(payload)

    def test_reference_mode_rejects_unopened_high_resolution_page(self) -> None:
        candidate = _candidate()

        with self.assertRaisesRegex(
            ValueError,
            "every high-resolution page",
        ):
            build_document_map_visual_coverage_receipt(
                source_sha256=candidate.source_sha256,
                page_count=candidate.page_count,
                mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
                event_summary=_event_summary((1, 3)),
            )

    def test_any_survey_mode_requires_contact_sheet_opened(self) -> None:
        candidate = _candidate()
        summary = _event_summary((1, 2, 3))
        summary["visual_inspection"]["contact_sheet_opened"] = False  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "opened contact sheet"):
            build_document_map_visual_coverage_receipt(
                source_sha256=candidate.source_sha256,
                page_count=candidate.page_count,
                mode=DocumentMapVisualCoverageMode.PRODUCT_ADAPTIVE,
                event_summary=summary,
            )

    def test_adaptive_mode_still_visually_samples_every_primary_range(self) -> None:
        candidate = _candidate()
        receipt = build_document_map_visual_coverage_receipt(
            source_sha256=candidate.source_sha256,
            page_count=candidate.page_count,
            mode=DocumentMapVisualCoverageMode.PRODUCT_ADAPTIVE,
            event_summary=_event_summary((1,)),
        )

        with self.assertRaisesRegex(
            ValueError,
            "high-resolution observation in every range",
        ):
            compile_document_map_survey(
                candidate,
                visual_coverage=receipt,
                expected_run_id=candidate.run_id,
                expected_source_sha256=candidate.source_sha256,
                expected_kit_identity=candidate.kit_identity,
            )

    def test_survey_page_refs_cannot_claim_unrelated_pages(self) -> None:
        payload = _candidate().model_dump(mode="json")
        payload["ranges"][0]["evidence_refs"] = ["page:2"]

        with self.assertRaisesRegex(ValueError, "leaves its page range"):
            DocumentMapSurveyCandidateV1.model_validate(payload)

    def test_host_normalizes_nonsemantic_codes_and_uses_numeric_page_order(self) -> None:
        finding = DocumentMapSurveyFindingV1(
            finding_code="NUMBERING_RESTART",
            page_numbers=(9, 10),
            evidence_refs=("page:9", "page:10"),
            public_summary="编号跨页继续。",
        )

        self.assertEqual(finding.finding_code, "numbering_restart")
        self.assertEqual(finding.evidence_refs, ("page:9", "page:10"))

    def test_unresolved_page_range_is_partial_but_remains_schedulable(self) -> None:
        payload = _candidate().model_dump(mode="json")
        payload["status"] = "PARTIAL"
        payload["ranges"][1]["kind"] = "UNRESOLVED"
        candidate = DocumentMapSurveyCandidateV1.model_validate(payload)
        receipt = build_document_map_visual_coverage_receipt(
            source_sha256=candidate.source_sha256,
            page_count=candidate.page_count,
            mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
            event_summary=_event_summary((1, 2, 3)),
        )

        compilation = compile_document_map_survey(
            candidate,
            visual_coverage=receipt,
            expected_run_id=candidate.run_id,
            expected_source_sha256=candidate.source_sha256,
            expected_kit_identity=candidate.kit_identity,
        )

        self.assertEqual(compilation.status, DocumentMapSurveyStatus.PARTIAL)
        self.assertEqual(len(compilation.unresolved_scope_ids), 1)
        self.assertEqual(len(compilation.scopes), 2)


def _candidate() -> DocumentMapSurveyCandidateV1:
    return DocumentMapSurveyCandidateV1(
        run_id="agent-run-" + "1" * 32,
        source_sha256="2" * 64,
        kit_identity=AgentKitIdentityV1(
            kit_id="reqpilotx-document-extraction",
            kit_version="0.15.0",
            manifest_sha256="4" * 64,
            bundle_sha256="3" * 64,
            policy_id="reqpilotx-extraction-policy",
            policy_version="1.0.0",
            tool_registry_id="reqpilotx-document-evidence-tools",
            tool_registry_version="1.3.0",
        ),
        page_count=3,
        status=DocumentMapSurveyStatus.COMPLETE,
        document_summary="一份含封面和正文的技术文档。",
        ranges=(
            DocumentMapSurveyRangeCandidateV1(
                proposal_key="front-matter",
                kind=DocumentMapSurveyRangeKind.FRONT_MATTER,
                label="封面",
                page_numbers=(1,),
                evidence_refs=("page:1",),
                public_summary="封面和文档控制信息。",
            ),
            DocumentMapSurveyRangeCandidateV1(
                proposal_key="main-document",
                kind=DocumentMapSurveyRangeKind.MAIN_DOCUMENT,
                label="正文",
                page_numbers=(2, 3),
                evidence_refs=("page:2", "page:3"),
                public_summary="连续的技术正文。",
            ),
        ),
        findings=(
            DocumentMapSurveyFindingV1(
                finding_code="numbering_restart",
                page_numbers=(2,),
                evidence_refs=("page:2",),
                public_summary="正文在第 2 页开始新的章节编号。",
            ),
        ),
    )


def _event_summary(pages: tuple[int, ...]) -> dict[str, object]:
    return {
        "schema_version": "reqpilotx-canonical-stage-event-ledger/v1",
        "visual_inspection": {
            "contact_sheet_opened": True,
            "high_resolution_page_numbers": list(pages),
            "other_image_view_count": 0,
        },
    }


if __name__ == "__main__":
    unittest.main()
