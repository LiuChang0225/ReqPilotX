from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.extraction.evaluate_llm_repeatability import (
    LlmRequirementRepeatabilityMismatchCode,
    LlmRequirementRepeatabilityStatus,
    evaluate_llm_requirement_batch_repeatability,
    run_llm_requirement_batch_repeatability,
)
from app.application.extraction.llm_provision_batch import (
    extract_requirement_provisions_with_provider,
)
from app.domain.extraction.semantics import SemanticClass
from tests.test_llm_provision_batch import (
    _ProvisionProvider,
    _three_provision_input,
)


SEED = 1234


class LlmRequirementRepeatabilityTests(unittest.TestCase):
    def test_three_identical_runs_pass_with_text_free_evidence(self) -> None:
        source, tree, locator = _three_provision_input()

        execution = run_llm_requirement_batch_repeatability(
            source_ir=source,
            structure_tree=tree,
            provider_factory=_seeded_provider,
            scope_locator=locator,
            sampling_seed=SEED,
            repetition_count=3,
        )

        report = execution.report
        self.assertIs(report.status, LlmRequirementRepeatabilityStatus.PASS)
        self.assertTrue(report.all_runs_complete)
        self.assertTrue(report.provider_request_exact_match)
        self.assertTrue(report.classification_request_exact_match)
        self.assertTrue(report.assembly_request_exact_match)
        self.assertTrue(report.semantic_exact_match)
        self.assertTrue(report.assembly_exact_match)
        self.assertTrue(report.requirement_view_exact_match)
        self.assertTrue(report.decision_skeleton_exact_match)
        self.assertEqual(
            report.unstable_semantic_decision_semantic_unit_ids,
            (),
        )
        self.assertEqual(
            report.unstable_assembly_decision_semantic_unit_ids,
            (),
        )
        self.assertEqual(
            report.unstable_requirement_view_semantic_unit_ids,
            (),
        )
        self.assertEqual(report.unstable_decision_semantic_unit_ids, ())
        self.assertEqual(report.mismatch_codes, ())
        self.assertEqual(len(execution.runs), 3)
        self.assertFalse(report.human_review_required)
        self.assertFalse(report.decision_review_required)
        self.assertFalse(report.non_decision_variance_only)
        self.assertFalse(report.is_human_gold)
        self.assertFalse(report.is_serving_activation_gate)
        serialized = report.model_dump_json()
        self.assertNotIn("The first product", serialized)
        self.assertNotIn("The second product", serialized)

    def test_assembly_and_view_drift_fail_without_falsely_claiming_semantic_drift(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        runs = (
            _batch(source, tree, locator),
            _batch(source, tree, locator, exclude_all=True),
            _batch(source, tree, locator),
        )

        report = evaluate_llm_requirement_batch_repeatability(
            source_ir=source,
            structure_tree=tree,
            runs=runs,
            sampling_seed=SEED,
        )

        self.assertIs(report.status, LlmRequirementRepeatabilityStatus.FAIL)
        self.assertTrue(report.semantic_exact_match)
        self.assertFalse(report.assembly_exact_match)
        self.assertFalse(report.requirement_view_exact_match)
        self.assertFalse(report.decision_skeleton_exact_match)
        self.assertTrue(report.human_review_required)
        self.assertTrue(report.decision_review_required)
        self.assertFalse(report.non_decision_variance_only)
        self.assertEqual(
            report.unstable_semantic_decision_semantic_unit_ids,
            (),
        )
        self.assertTrue(report.unstable_assembly_decision_semantic_unit_ids)
        self.assertTrue(report.unstable_requirement_view_semantic_unit_ids)
        self.assertTrue(report.unstable_decision_semantic_unit_ids)
        self.assertIn(
            LlmRequirementRepeatabilityMismatchCode.ASSEMBLY_RESULT_MISMATCH,
            report.mismatch_codes,
        )
        self.assertIn(
            LlmRequirementRepeatabilityMismatchCode.REQUIREMENT_VIEW_MISMATCH,
            report.mismatch_codes,
        )

    def test_request_identity_drift_is_separate_from_functional_results(self) -> None:
        source, tree, locator = _three_provision_input()
        shifted = _ProvisionProvider()
        shifted.sampling_seed = SEED
        shifted.classification_call_count = 10
        shifted.assembly_call_count = 10
        runs = (
            _batch(source, tree, locator),
            extract_requirement_provisions_with_provider(
                source,
                tree,
                shifted,
                locator,
            ),
            _batch(source, tree, locator),
        )

        report = evaluate_llm_requirement_batch_repeatability(
            source_ir=source,
            structure_tree=tree,
            runs=runs,
            sampling_seed=SEED,
        )

        self.assertFalse(report.provider_request_exact_match)
        self.assertFalse(report.classification_request_exact_match)
        self.assertFalse(report.assembly_request_exact_match)
        self.assertTrue(report.semantic_exact_match)
        self.assertTrue(report.assembly_exact_match)
        self.assertTrue(report.requirement_view_exact_match)
        self.assertTrue(report.decision_skeleton_exact_match)
        self.assertTrue(report.decision_review_required)
        self.assertFalse(report.non_decision_variance_only)
        self.assertEqual(
            report.mismatch_codes,
            (LlmRequirementRepeatabilityMismatchCode.PROVIDER_REQUEST_MISMATCH,),
        )

    def test_partial_provider_run_fails_the_gate(self) -> None:
        source, tree, locator = _three_provision_input()
        runs = (
            _batch(source, tree, locator),
            _batch(source, tree, locator, timeout_labels={"4.12.2"}),
            _batch(source, tree, locator),
        )

        report = evaluate_llm_requirement_batch_repeatability(
            source_ir=source,
            structure_tree=tree,
            runs=runs,
            sampling_seed=SEED,
        )

        self.assertFalse(report.all_runs_complete)
        self.assertTrue(report.human_review_required)
        self.assertTrue(report.decision_review_required)
        self.assertIn(
            LlmRequirementRepeatabilityMismatchCode.RUN_NOT_COMPLETE,
            report.mismatch_codes,
        )

    def test_context_only_view_drift_keeps_decision_skeleton_stable(self) -> None:
        source, tree, locator = _three_provision_input()
        runs = (
            _batch(source, tree, locator),
            _batch_with_provider(source, tree, locator, _ContextExcludedProvider()),
            _batch(source, tree, locator),
        )

        report = evaluate_llm_requirement_batch_repeatability(
            source_ir=source,
            structure_tree=tree,
            runs=runs,
            sampling_seed=SEED,
        )

        self.assertIs(report.status, LlmRequirementRepeatabilityStatus.FAIL)
        self.assertTrue(report.semantic_exact_match)
        self.assertFalse(report.assembly_exact_match)
        self.assertFalse(report.requirement_view_exact_match)
        self.assertTrue(report.decision_skeleton_exact_match)
        self.assertEqual(report.unstable_decision_semantic_unit_ids, ())
        self.assertTrue(report.human_review_required)
        self.assertFalse(report.decision_review_required)
        self.assertTrue(report.non_decision_variance_only)

    def test_non_decision_semantic_drift_can_change_assembly_request_only(self) -> None:
        source, tree, locator = _three_provision_input()
        runs = (
            _batch(source, tree, locator),
            _batch_with_provider(
                source,
                tree,
                locator,
                _ContextSemanticVarianceProvider(),
            ),
            _batch(source, tree, locator),
        )

        report = evaluate_llm_requirement_batch_repeatability(
            source_ir=source,
            structure_tree=tree,
            runs=runs,
            sampling_seed=SEED,
        )

        self.assertFalse(report.provider_request_exact_match)
        self.assertTrue(report.classification_request_exact_match)
        self.assertFalse(report.assembly_request_exact_match)
        self.assertFalse(report.semantic_exact_match)
        self.assertTrue(report.decision_skeleton_exact_match)
        self.assertTrue(report.human_review_required)
        self.assertFalse(report.decision_review_required)
        self.assertTrue(report.non_decision_variance_only)

    def test_volatile_run_ids_do_not_change_semantic_or_assembly_input_identity(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _RecordingProvisionProvider()
        provider.sampling_seed = SEED

        first = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )
        second = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        first_extraction = first.provisions[0].extraction
        second_extraction = second.provisions[0].extraction
        assert first_extraction is not None and second_extraction is not None
        self.assertNotEqual(
            first_extraction.provider_runs[0].provider_run_id,
            second_extraction.provider_runs[0].provider_run_id,
        )
        self.assertEqual(
            first_extraction.semantics.semantic_snapshot_id,
            second_extraction.semantics.semantic_snapshot_id,
        )
        self.assertEqual(
            provider.assembly_requests[0].model_dump(mode="json"),
            provider.assembly_requests[3].model_dump(mode="json"),
        )

    def test_run_count_seed_and_provider_seed_fail_closed(self) -> None:
        source, tree, locator = _three_provision_input()
        with self.assertRaisesRegex(ValueError, "three and ten"):
            run_llm_requirement_batch_repeatability(
                source_ir=source,
                structure_tree=tree,
                provider_factory=_seeded_provider,
                scope_locator=locator,
                sampling_seed=SEED,
                repetition_count=2,
            )
        with self.assertRaisesRegex(ValueError, "sampling_seed"):
            run_llm_requirement_batch_repeatability(
                source_ir=source,
                structure_tree=tree,
                provider_factory=_seeded_provider,
                scope_locator=locator,
                sampling_seed=-1,
                repetition_count=3,
            )
        with self.assertRaisesRegex(ValueError, "provider sampling seed"):
            run_llm_requirement_batch_repeatability(
                source_ir=source,
                structure_tree=tree,
                provider_factory=lambda: _ProvisionProvider(),
                scope_locator=locator,
                sampling_seed=SEED,
                repetition_count=3,
            )


class _RecordingProvisionProvider(_ProvisionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.assembly_requests = []

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        self.assembly_requests.append(request)
        return super().assemble_requirements(request)


class _ContextExcludedProvider(_ProvisionProvider):
    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        result = super().assemble_requirements(request)
        primary = next(
            item for item in request.nodes if item.semantic_class.value == "REQUIREMENT"
        )
        decisions = tuple(
            item
            if item.target_structure_node_id == primary.target_structure_node_id
            else item.model_copy(
                update={
                    "include_in_view": False,
                    "role": None,
                    "parent_structure_node_id": None,
                    "reason_codes": ("synthetic_context_excluded",),
                }
            )
            for item in result.decisions
        )
        return result.model_copy(update={"decisions": decisions})


class _ContextSemanticVarianceProvider(_ProvisionProvider):
    def classify_nodes(self, request):  # type: ignore[no-untyped-def]
        result = super().classify_nodes(request)
        context_target = next(
            item.target_structure_node_id
            for item in result.decisions
            if item.semantic_class is not SemanticClass.REQUIREMENT
        )
        decisions = tuple(
            item.model_copy(
                update={
                    "semantic_class": SemanticClass.BACKGROUND,
                    "requirement_subtype": None,
                    "reason_codes": ("synthetic_context_semantic_variance",),
                }
            )
            if item.target_structure_node_id == context_target
            else item
            for item in result.decisions
        )
        return result.model_copy(update={"decisions": decisions})

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        result = super().assemble_requirements(request)
        return result.model_copy(
            update={"run": result.run.model_copy(update={"request_sha256": "1" * 64})}
        )


def _seeded_provider(**kwargs):  # type: ignore[no-untyped-def]
    provider = _ProvisionProvider(**kwargs)
    provider.sampling_seed = SEED
    return provider


def _batch(source, tree, locator, **kwargs):  # type: ignore[no-untyped-def]
    return extract_requirement_provisions_with_provider(
        source,
        tree,
        _seeded_provider(**kwargs),
        locator,
    )


def _batch_with_provider(source, tree, locator, provider):  # type: ignore[no-untyped-def]
    provider.sampling_seed = SEED
    return extract_requirement_provisions_with_provider(source, tree, provider, locator)


if __name__ == "__main__":
    unittest.main()
