from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from pydantic import ValidationError

from app.application.extraction.codex_extraction_parity import (
    CodexExtractionLane,
    CodexExtractionMetricDeltaV1,
    CodexExtractionMetricV1,
    CodexExtractionObservationV1,
    CodexExtractionParityStatus,
    CodexExtractionProvenance,
    CodexExtractionStage,
    CodexExtractionStageObservationV1,
    CodexExtractionStageStatus,
    NativeCodexReferencePayloadV1,
    build_native_codex_reference_observation,
    compare_codex_extraction_observations,
    native_codex_reference_output_schema,
)


SOURCE_SHA256 = "1" * 64


def _stage(
    stage: CodexExtractionStage,
    *,
    status: CodexExtractionStageStatus = CodexExtractionStageStatus.OBSERVED,
    metrics: tuple[tuple[str, int], ...] = (),
    capabilities: tuple[str, ...] = (),
    decision_sha256: str | None = None,
    grounding_sha256: str | None = None,
    decision_fingerprint_kind: str | None = None,
    grounding_fingerprint_kind: str | None = None,
    diagnostics: tuple[str, ...] = (),
) -> CodexExtractionStageObservationV1:
    return CodexExtractionStageObservationV1(
        stage=stage,
        status=status,
        metrics=tuple(
            CodexExtractionMetricV1(name=name, value=value) for name, value in metrics
        ),
        capabilities_used=capabilities,
        decision_sha256=decision_sha256,
        grounding_sha256=grounding_sha256,
        decision_fingerprint_kind=(
            decision_fingerprint_kind
            or ("normalized-test-v1" if decision_sha256 else None)
        ),
        grounding_fingerprint_kind=(
            grounding_fingerprint_kind
            or ("normalized-test-v1" if grounding_sha256 else None)
        ),
        diagnostic_codes=diagnostics,
    )


def _observation(
    *,
    lane: CodexExtractionLane,
    stages: tuple[CodexExtractionStageObservationV1, ...] | None = None,
    source_sha256: str = SOURCE_SHA256,
) -> CodexExtractionObservationV1:
    provenance = {
        CodexExtractionLane.NATIVE_CODEX_REFERENCE: (
            CodexExtractionProvenance.NATIVE_CODEX_REFERENCE
        ),
        CodexExtractionLane.CODEX_CANDIDATE_DRAFT: (
            CodexExtractionProvenance.AI_REVIEWED_CANDIDATE_DRAFT
        ),
        CodexExtractionLane.REQPILOTX: (
            CodexExtractionProvenance.REQPILOTX_RUNTIME_OBSERVATION
        ),
    }[lane]
    return CodexExtractionObservationV1(
        observation_id=(
            "codex-extraction-observation-"
            + {
                CodexExtractionLane.NATIVE_CODEX_REFERENCE: "a" * 32,
                CodexExtractionLane.CODEX_CANDIDATE_DRAFT: "b" * 32,
                CodexExtractionLane.REQPILOTX: "c" * 32,
            }[lane]
        ),
        lane=lane,
        provenance=provenance,
        source_sha256=source_sha256,
        page_count=3,
        model_id="gpt-5.6-sol",
        skill_ids=("reqpilotx-pdf-understanding",),
        stages=stages or tuple(_stage(stage) for stage in CodexExtractionStage),
    )


def _native_stage(
    stage: CodexExtractionStage,
) -> CodexExtractionStageObservationV1:
    metrics = {
        CodexExtractionStage.PAGE_UNDERSTANDING: (
            ("page_count", 3),
            ("pages_observed", 3),
            ("pages_unresolved", 0),
        ),
        CodexExtractionStage.STRUCTURE: (
            ("native_reference_figures", 0),
            ("native_reference_logical_tables", 1),
            ("native_reference_numbered_sections", 2),
        ),
        CodexExtractionStage.REQUIREMENT_RECOGNITION: (
            ("native_reference_requirement_parents", 2),
            ("native_reference_semantic_unresolved", 0),
        ),
        CodexExtractionStage.REQUIREMENT_DECOMPOSITION: (
            ("native_reference_requirement_children", 1),
            ("native_reference_requirement_parents", 2),
            ("native_reference_unresolved_spans", 0),
        ),
    }[stage]
    capability = {
        CodexExtractionStage.PAGE_UNDERSTANDING: "page_render",
        CodexExtractionStage.STRUCTURE: "structure_reconstruction",
        CodexExtractionStage.REQUIREMENT_RECOGNITION: "semantic_reasoning",
        CodexExtractionStage.REQUIREMENT_DECOMPOSITION: ("parent_child_decomposition"),
    }[stage]
    semantic_stage = stage in {
        CodexExtractionStage.REQUIREMENT_RECOGNITION,
        CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
    }
    return _stage(
        stage,
        status=(
            CodexExtractionStageStatus.NOT_RUN
            if semantic_stage
            else CodexExtractionStageStatus.OBSERVED
        ),
        metrics=(
            tuple((name, 0) for name, _value in metrics) if semantic_stage else metrics
        ),
        capabilities=() if semantic_stage else (capability,),
        diagnostics=("source_bound_stage_deferred",) if semantic_stage else (),
    )


class CodexExtractionParityTests(unittest.TestCase):
    def test_native_reference_payload_builds_stable_observation(self) -> None:
        payload = NativeCodexReferencePayloadV1(
            source_sha256=SOURCE_SHA256,
            page_count=3,
            model_id="gpt-5.6-sol",
            skill_ids=("pdf",),
            stages=tuple(_native_stage(stage) for stage in CodexExtractionStage),
        )

        first = build_native_codex_reference_observation(payload)
        second = build_native_codex_reference_observation(payload)

        self.assertEqual(first, second)
        self.assertEqual(first.lane, CodexExtractionLane.NATIVE_CODEX_REFERENCE)
        self.assertEqual(
            first.provenance,
            CodexExtractionProvenance.NATIVE_CODEX_REFERENCE,
        )

    def test_native_reference_output_schema_requires_every_property(self) -> None:
        schema = native_codex_reference_output_schema()

        def assert_required(value: object) -> None:
            if isinstance(value, list):
                for item in value:
                    assert_required(item)
                return
            if not isinstance(value, dict):
                return
            properties = value.get("properties")
            if isinstance(properties, dict):
                self.assertEqual(value["required"], list(properties))
                self.assertFalse(value["additionalProperties"])
            for item in value.values():
                assert_required(item)

        assert_required(schema)

    def test_observation_requires_complete_ordered_stages(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "extraction stages must be complete and ordered",
        ):
            _observation(
                lane=CodexExtractionLane.CODEX_CANDIDATE_DRAFT,
                stages=(
                    _stage(CodexExtractionStage.STRUCTURE),
                    _stage(CodexExtractionStage.PAGE_UNDERSTANDING),
                ),
            )

    def test_observation_rejects_lane_provenance_mismatch(self) -> None:
        payload = _observation(
            lane=CodexExtractionLane.CODEX_CANDIDATE_DRAFT
        ).model_dump(mode="json")
        payload["provenance"] = "NATIVE_CODEX_REFERENCE"
        with self.assertRaisesRegex(
            ValidationError,
            "observation provenance does not match its lane",
        ):
            CodexExtractionObservationV1.model_validate(payload)

    def test_metric_delta_rejects_inconsistent_value(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "metric delta does not match its values",
        ):
            CodexExtractionMetricDeltaV1(
                name="requirement_parents",
                reference_value=10,
                reqpilotx_value=7,
                delta=3,
            )

    def test_compare_reports_earliest_stage_and_content_free_deltas(self) -> None:
        reference = _observation(
            lane=CodexExtractionLane.CODEX_CANDIDATE_DRAFT,
            stages=(
                _stage(
                    CodexExtractionStage.PAGE_UNDERSTANDING,
                    metrics=(("pages_observed", 3),),
                    capabilities=("page_render", "page_text"),
                    grounding_sha256="d" * 64,
                ),
                _stage(
                    CodexExtractionStage.STRUCTURE,
                    metrics=(("structure_units", 12),),
                ),
                _stage(CodexExtractionStage.REQUIREMENT_RECOGNITION),
                _stage(CodexExtractionStage.REQUIREMENT_DECOMPOSITION),
            ),
        )
        reqpilotx = _observation(
            lane=CodexExtractionLane.REQPILOTX,
            stages=(
                _stage(
                    CodexExtractionStage.PAGE_UNDERSTANDING,
                    status=CodexExtractionStageStatus.PARTIAL,
                    metrics=(("pages_observed", 2),),
                    capabilities=("page_text",),
                    grounding_sha256="e" * 64,
                    diagnostics=("visual_page_missing",),
                ),
                _stage(
                    CodexExtractionStage.STRUCTURE,
                    status=CodexExtractionStageStatus.BLOCKED,
                    metrics=(("structure_units", 1),),
                ),
                _stage(
                    CodexExtractionStage.REQUIREMENT_RECOGNITION,
                    status=CodexExtractionStageStatus.NOT_RUN,
                ),
                _stage(
                    CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
                    status=CodexExtractionStageStatus.NOT_RUN,
                ),
            ),
        )

        report = compare_codex_extraction_observations(reference, reqpilotx)

        self.assertEqual(report.status, CodexExtractionParityStatus.DIFFERENT)
        self.assertEqual(
            report.first_gap_stage,
            CodexExtractionStage.PAGE_UNDERSTANDING,
        )
        page = report.stages[0]
        self.assertEqual(page.reference_only_capabilities, ("page_render",))
        self.assertEqual(page.metric_deltas[0].delta, -1)
        self.assertIn("grounding_fingerprint_mismatch", page.gap_codes)
        self.assertIn(
            "reference_is_rule_assisted_candidate_draft",
            report.limitations,
        )
        serialized = report.model_dump(mode="json")
        self.assertNotIn("source_text", serialized)

    def test_compare_can_report_structural_parity_without_claiming_gold(self) -> None:
        reference = _observation(lane=CodexExtractionLane.NATIVE_CODEX_REFERENCE)
        reqpilotx = _observation(lane=CodexExtractionLane.REQPILOTX)

        report = compare_codex_extraction_observations(reference, reqpilotx)

        self.assertEqual(report.status, CodexExtractionParityStatus.PARITY)
        self.assertIsNone(report.first_gap_stage)
        self.assertFalse(report.is_human_gold)
        self.assertFalse(report.is_serving_activation_gate)

    def test_compare_does_not_compare_different_fingerprint_kinds(self) -> None:
        reference_stages = list(
            _observation(lane=CodexExtractionLane.NATIVE_CODEX_REFERENCE).stages
        )
        reqpilotx_stages = list(_observation(lane=CodexExtractionLane.REQPILOTX).stages)
        reference_stages[1] = _stage(
            CodexExtractionStage.STRUCTURE,
            decision_sha256="a" * 64,
            decision_fingerprint_kind="native-outline-v1",
        )
        reqpilotx_stages[1] = _stage(
            CodexExtractionStage.STRUCTURE,
            decision_sha256="b" * 64,
            decision_fingerprint_kind="structure-tree-v2",
        )

        report = compare_codex_extraction_observations(
            _observation(
                lane=CodexExtractionLane.NATIVE_CODEX_REFERENCE,
                stages=tuple(reference_stages),
            ),
            _observation(
                lane=CodexExtractionLane.REQPILOTX,
                stages=tuple(reqpilotx_stages),
            ),
        )

        structure = report.stages[1]
        self.assertIsNone(structure.decision_match)
        self.assertNotIn("decision_fingerprint_mismatch", structure.gap_codes)

    def test_compare_rejects_cross_source_observations(self) -> None:
        reference = _observation(lane=CodexExtractionLane.NATIVE_CODEX_REFERENCE)
        reqpilotx = _observation(
            lane=CodexExtractionLane.REQPILOTX,
            source_sha256="2" * 64,
        )

        with self.assertRaisesRegex(ValueError, "same source SHA-256"):
            compare_codex_extraction_observations(reference, reqpilotx)


if __name__ == "__main__":
    unittest.main()
