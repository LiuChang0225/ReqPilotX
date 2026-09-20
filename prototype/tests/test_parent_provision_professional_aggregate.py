from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import unittest

from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    AgentNativeReadOnlyRuntimePolicyV1,
)
from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAgentNativeReadCapabilityV1,
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalAppServerStageActivation,
    CanonicalAppServerStageAttestationV1,
    CanonicalDocumentEvidenceAttestationV1,
    CanonicalStageFileDigestV1,
    CanonicalStagePredecessorBindingV1,
)
from app.adapters.agents.codex_app_server.d3_stage_input_workspace import (
    D3StageInputBundleAttestationV1,
    D3StageInputFileDigestV1,
    D3StageInputPredecessorBindingV1,
)
from app.adapters.agents.codex_app_server.lifecycle import CanaryRuntimeState
from app.adapters.agents.codex_app_server.parent_provision_professional_aggregate import (
    ParentProvisionAggregateProfessionalProofV1,
    ParentProvisionProfessionalAggregateError,
    ParentProvisionRecognitionFinalizationMode,
    build_parent_provision_recognition_finalized_execution_receipt,
    finalize_parent_provision_professional_aggregate,
)
from app.adapters.agents.d3_parent_provision_attestation import (
    compile_d3_parent_provision_policy_closure,
)
from app.adapters.agents.portable_kit import PortableAgentKit, load_portable_agent_kit
from app.application.agent_workflow.d3_parent_provisions import (
    ParentProvisionStatus,
    Requirementness,
    canonical_json_sha256,
    parent_provision_evidence_refs,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentStageArtifactStatus,
)
from app.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateExecutionReceiptV2,
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionBatchStatus,
    ParentProvisionRecognitionCleanupStatus,
    build_parent_provision_recognition_aggregate_execution_receipt,
    build_parent_provision_recognition_attempt_receipt,
    build_parent_provision_recognition_batch_receipt,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchAggregateV1,
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectScopeStatus,
    aggregate_parent_provision_object_batches,
    build_parent_provision_object_batch_plan,
    compile_parent_provision_object_candidate,
)

if __package__:
    from .test_parent_provision_object_batches import (
        _closed_candidate,
        _entry_for,
        _fixture,
    )
else:
    from test_parent_provision_object_batches import (
        _closed_candidate,
        _entry_for,
        _fixture,
    )


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = REPOSITORY_ROOT / "agent-kit"


@dataclass(frozen=True, slots=True)
class _Scenario:
    kit: PortableAgentKit
    activation: CanonicalAppServerStageActivation
    identity: object
    segments: tuple[object, ...]
    plan: ParentProvisionObjectBatchPlanV1
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2
    finalized_receipt: object
    aggregate: ParentProvisionObjectBatchAggregateV1


class ParentProvisionProfessionalAggregateTests(unittest.TestCase):
    def test_durable_attempts_can_finalize_in_a_fresh_session(self) -> None:
        scenario = _scenario()
        fresh_session = scenario.activation.session_attestation.model_copy(
            update={"session_id": "canonical-document-session-" + "f" * 32}
        )
        digest = canonical_json_sha256(
            fresh_session.model_dump(mode="json", exclude={"attestation_id"})
        )
        fresh_session = fresh_session.model_copy(
            update={"attestation_id": f"canonical-session-attestation-{digest[:32]}"}
        )
        fresh_activation = replace(
            scenario.activation,
            session_attestation=fresh_session,
        )

        result = _finalize(scenario, activation=fresh_activation)

        self.assertTrue(result.professional_policy_attestation.policy_attested)
        self.assertEqual(result.proposal.status, ParentProvisionStatus.COMPLETE)

    def test_complete_aggregate_proof_is_stable_and_closes_final_lineage(self) -> None:
        scenario = _scenario()

        first = _finalize(scenario)
        second = _finalize(scenario)

        self.assertEqual(first.professional_proof, second.professional_proof)
        self.assertEqual(
            first.professional_execution_receipt.execution_trace_sha256,
            first.professional_proof.proof_sha256,
        )
        self.assertEqual(
            first.professional_proof.finalized_execution_receipt_sha256,
            scenario.finalized_receipt.receipt_sha256,
        )
        self.assertTrue(first.professional_policy_attestation.policy_attested)
        self.assertEqual(first.proposal.status, ParentProvisionStatus.COMPLETE)
        self.assertEqual(
            first.stage_artifact.status, AgentStageArtifactStatus.VALIDATED
        )
        self.assertEqual(
            first.final_receipt.proposal_sha256,
            first.host_validation_receipt.artifact_sha256,
        )
        self.assertEqual(
            first.final_receipt.validation_receipt_sha256,
            first.host_validation_receipt.receipt_sha256,
        )
        self.assertEqual(
            first.proposal.policy_attestation.execution_receipt_sha256,
            canonical_json_sha256(
                first.professional_execution_receipt.model_dump(mode="json")
            ),
        )

    def test_semantic_partial_is_professionally_attested_not_execution_failed(
        self,
    ) -> None:
        scenario = _scenario(partial=True)

        result = _finalize(scenario)

        self.assertEqual(result.proposal.status, ParentProvisionStatus.PARTIAL)
        self.assertEqual(result.stage_artifact.status, AgentStageArtifactStatus.PARTIAL)
        self.assertEqual(result.stage_artifact.unresolved_scope_count, 1)
        self.assertEqual(
            scenario.finalized_receipt.status,
            ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
        )

    def test_unresolved_entry_without_unresolved_scope_remains_partial(self) -> None:
        scenario = _scenario(entry_partial=True)

        result = _finalize(scenario)

        self.assertEqual(result.proposal.status, ParentProvisionStatus.PARTIAL)
        self.assertEqual(scenario.finalized_receipt.semantic_unresolved_scope_count, 0)
        self.assertEqual(scenario.finalized_receipt.semantic_unresolved_entry_count, 1)
        self.assertFalse(scenario.finalized_receipt.predecessor_partial)

    def test_partial_predecessor_without_unresolved_content_remains_partial(
        self,
    ) -> None:
        scenario = _scenario(predecessor_partial=True)

        result = _finalize(scenario)

        self.assertEqual(result.proposal.status, ParentProvisionStatus.PARTIAL)
        self.assertEqual(scenario.finalized_receipt.semantic_unresolved_scope_count, 0)
        self.assertEqual(scenario.finalized_receipt.semantic_unresolved_entry_count, 0)
        self.assertTrue(scenario.finalized_receipt.predecessor_partial)

    def test_reconciled_aggregate_requires_and_binds_coordination_execution(
        self,
    ) -> None:
        scenario = _scenario(reconciled=True)

        result = _finalize(scenario)

        self.assertEqual(
            scenario.finalized_receipt.finalization_mode,
            ParentProvisionRecognitionFinalizationMode.RECONCILED,
        )
        self.assertEqual(
            result.professional_proof.object_batch_aggregate_sha256,
            scenario.aggregate.aggregate_sha256,
        )
        with self.assertRaisesRegex(
            ParentProvisionProfessionalAggregateError,
            "RECONCILIATION_EXECUTION_PROOF_MISSING",
        ):
            _build_finalized(
                initial=scenario.initial_receipt,
                aggregate=scenario.aggregate,
                reconciliation_plan_sha256=None,
                reconciliation_execution_receipts_sha256=None,
            )

    def test_execution_failure_cannot_be_reclassified_as_semantic_partial(self) -> None:
        scenario = _scenario()
        failed = _failed_initial_receipt(scenario)

        with self.assertRaisesRegex(
            ParentProvisionProfessionalAggregateError,
            "RECONCILIATION_INPUT_CONTAINS_EXECUTION_FAILURE",
        ):
            _build_finalized(
                initial=failed,
                aggregate=scenario.aggregate,
                reconciliation_plan_sha256="7" * 64,
                reconciliation_execution_receipts_sha256="8" * 64,
            )

    def test_missing_initial_batch_cannot_be_professionally_finalized(self) -> None:
        scenario = _scenario()
        incomplete = _incomplete_initial_receipt(scenario)

        with self.assertRaisesRegex(
            ParentProvisionProfessionalAggregateError,
            "BATCH_SET_INCOMPLETE",
        ):
            _build_finalized(initial=incomplete, aggregate=scenario.aggregate)

    def test_tampered_batch_trace_final_hash_session_and_policy_are_rejected(
        self,
    ) -> None:
        scenario = _scenario()
        first_batch = scenario.initial_receipt.batches[0]
        bad_batch = first_batch.model_copy(update={"receipt_sha256": "0" * 64})
        bad_initial_batch = scenario.initial_receipt.model_copy(
            update={
                "batches": (bad_batch, *scenario.initial_receipt.batches[1:]),
            }
        )
        bad_initial_trace = scenario.initial_receipt.model_copy(
            update={"aggregate_execution_trace_sha256": "0" * 64}
        )
        bad_final_hash = scenario.finalized_receipt.model_copy(
            update={"global_aggregate_sha256": "0" * 64}
        )
        bad_session = replace(
            scenario.activation,
            session_attestation=scenario.activation.session_attestation.model_copy(
                update={"runner_profile_sha256": "0" * 64}
            ),
        )
        assert scenario.activation.d3_policy_closure is not None
        bad_policy = replace(
            scenario.activation,
            d3_policy_closure=scenario.activation.d3_policy_closure.model_copy(
                update={"stage_implementation_status": "PLANNED"}
            ),
        )

        for overrides in (
            {"initial_aggregate_execution_receipt": bad_initial_batch},
            {"initial_aggregate_execution_receipt": bad_initial_trace},
            {"finalized_execution_receipt": bad_final_hash},
            {"activation": bad_session},
            {"activation": bad_policy},
        ):
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(ParentProvisionProfessionalAggregateError):
                    _finalize(scenario, **overrides)

    def test_professional_proof_detects_its_own_tampering(self) -> None:
        proof = _finalize(_scenario()).professional_proof
        payload = proof.model_dump(mode="json")
        payload["final_execution_trace_sha256"] = "0" * 64

        with self.assertRaisesRegex(ValueError, "proof SHA drifted"):
            ParentProvisionAggregateProfessionalProofV1.model_validate(payload)


def _scenario(
    *,
    partial: bool = False,
    entry_partial: bool = False,
    predecessor_partial: bool = False,
    reconciled: bool = False,
) -> _Scenario:
    kit = load_portable_agent_kit(AGENT_KIT_ROOT)
    base = _fixture()
    identity = base.identity.model_copy(
        update={
            "kit_identity": kit.identity,
            "status": (
                ParentProvisionStatus.PARTIAL
                if predecessor_partial
                else base.identity.status
            ),
        }
    )
    activation = _activation(kit, identity)
    stage_input = activation.d3_stage_input_attestation
    assert stage_input is not None
    plan = build_parent_provision_object_batch_plan(
        d2_identity=identity,
        logical_objects=base.objects,
        source_segments=base.segments,
        stage_input_bundle_sha256=stage_input.bundle_sha256,
    )
    final_results = _compile_results(
        plan=plan,
        identity=identity,
        segments=base.segments,
        kit=kit,
        partial=partial,
        entry_partial=entry_partial,
        reconciliation_batch_id=None,
    )
    aggregate = aggregate_parent_provision_object_batches(
        plan=plan,
        expected_plan_sha256=plan.plan_sha256,
        results=final_results,
        d2_identity=identity,
        source_segments=base.segments,
        kit_identity=kit.identity,
    )
    initial_results = final_results
    reconciliation_batch_id = None
    if reconciled:
        reconciliation_batch_id = next(
            item.batch_id
            for item in plan.targets
            if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
        )
        initial_results = _compile_results(
            plan=plan,
            identity=identity,
            segments=base.segments,
            kit=kit,
            partial=False,
            entry_partial=False,
            reconciliation_batch_id=reconciliation_batch_id,
        )
    batches = _batch_receipts(
        activation=activation,
        plan=plan,
        results=initial_results,
    )
    evidence_refs = parent_provision_evidence_refs(
        declared_logical_object_ids=(
            aggregate.compiled_decision.declared_logical_object_ids
        ),
        unresolved_logical_object_ids=(
            aggregate.compiled_decision.unresolved_logical_object_ids
        ),
        entries=aggregate.compiled_decision.entries,
    )
    closure = activation.d3_policy_closure
    assert closure is not None
    if reconciled:
        initial = build_parent_provision_recognition_aggregate_execution_receipt(
            **_aggregate_values(
                activation=activation,
                plan=plan,
                batches=batches,
                policy_closure_sha256=canonical_json_sha256(
                    closure.model_dump(mode="json")
                ),
            ),
            status=ParentProvisionRecognitionAggregateStatus.BLOCKED,
            semantic_unresolved_scope_count=0,
            object_batch_aggregate_sha256=None,
            decision_candidate_sha256=None,
            parent_provision_decision_sha256=None,
            evidence_refs_sha256=None,
            reason_codes=("ADJACENT_RECONCILIATION_REQUIRED",),
        )
    else:
        initial = build_parent_provision_recognition_aggregate_execution_receipt(
            **_aggregate_values(
                activation=activation,
                plan=plan,
                batches=batches,
                policy_closure_sha256=canonical_json_sha256(
                    closure.model_dump(mode="json")
                ),
            ),
            status=(
                ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC
                if aggregate.compiled_decision.status is ParentProvisionStatus.PARTIAL
                else ParentProvisionRecognitionAggregateStatus.COMPLETE
            ),
            semantic_unresolved_scope_count=sum(
                item.semantic_unresolved_scope_count for item in batches
            ),
            semantic_unresolved_entry_count=sum(
                item.semantic_unresolved_entry_count for item in batches
            ),
            predecessor_partial=predecessor_partial,
            object_batch_aggregate_sha256=aggregate.aggregate_sha256,
            decision_candidate_sha256=aggregate.global_candidate_sha256,
            parent_provision_decision_sha256=(
                aggregate.compiled_decision.decision_sha256
            ),
            evidence_refs_sha256=canonical_json_sha256(evidence_refs),
            reason_codes=(),
        )
    finalized = _build_finalized(
        initial=initial,
        aggregate=aggregate,
        replaced_batch_ids=(
            tuple(
                item.batch_id
                for item in plan.targets
                if item.execution_mode
                is ParentProvisionObjectExecutionMode.MODEL_TARGET
            )
            if reconciled
            else ()
        ),
        reconciliation_plan_sha256=("7" * 64 if reconciled else None),
        reconciliation_execution_receipts_sha256=("8" * 64 if reconciled else None),
    )
    return _Scenario(
        kit=kit,
        activation=activation,
        identity=identity,
        segments=base.segments,
        plan=plan,
        initial_receipt=initial,
        finalized_receipt=finalized,
        aggregate=aggregate,
    )


def _compile_results(
    *,
    plan: ParentProvisionObjectBatchPlanV1,
    identity,  # type: ignore[no-untyped-def]
    segments,  # type: ignore[no-untyped-def]
    kit: PortableAgentKit,
    partial: bool,
    entry_partial: bool,
    reconciliation_batch_id: str | None,
) -> tuple[ParentProvisionObjectBatchCompilationV1, ...]:
    results = []
    model_targets = tuple(
        item
        for item in plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    for index, target in enumerate(model_targets):
        if target.batch_id == reconciliation_batch_id:
            candidate = ParentProvisionObjectCandidateV1(
                scope_status=(
                    ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ),
                diagnostic_codes=("ADJACENT_RECONCILIATION_REQUIRED",),
            )
        elif partial and index == len(model_targets) - 1:
            candidate = ParentProvisionObjectCandidateV1(
                scope_status=ParentProvisionObjectScopeStatus.UNRESOLVED,
                diagnostic_codes=("SEMANTIC_CLASSIFICATION_UNRESOLVED",),
            )
        else:
            ordinal = target.segment_ordinals[0]
            entry = _entry_for(segments[ordinal], ordinal=ordinal)
            if entry_partial and index == len(model_targets) - 1:
                entry = entry.model_copy(
                    update={
                        "requirementness": Requirementness.UNRESOLVED,
                        "requirement_kind": None,
                        "technicality": None,
                    }
                )
            candidate = _closed_candidate(entry)
        results.append(
            compile_parent_provision_object_candidate(
                plan=plan,
                expected_plan_sha256=plan.plan_sha256,
                batch_id=target.batch_id,
                d2_identity=identity,
                source_segments=segments,
                kit_identity=kit.identity,
                candidate=candidate,
            )
        )
    return tuple(results)


def _batch_receipts(
    *,
    activation: CanonicalAppServerStageActivation,
    plan: ParentProvisionObjectBatchPlanV1,
    results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
):  # type: ignore[no-untyped-def]
    result_by_id = {item.batch_id: item for item in results}
    session_sha256 = canonical_json_sha256(
        activation.session_attestation.model_dump(mode="json")
    )
    stage_binding = activation.session_attestation.stages[0]
    batches = []
    for target in plan.targets:
        common = {
            "run_id": plan.run_id,
            "source_sha256": plan.source_sha256,
            "predecessor_artifact_id": plan.predecessor_artifact_id,
            "predecessor_artifact_sha256": plan.predecessor_artifact_sha256,
            "kit_bundle_sha256": plan.kit_bundle_sha256,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "batch_id": target.batch_id,
            "batch_order": target.batch_order,
            "target_scope_sha256": target.target_scope_sha256,
        }
        if (
            target.execution_mode
            is ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
        ):
            batches.append(
                build_parent_provision_recognition_batch_receipt(
                    **common,
                    attempts=(),
                    accepted_attempt_id=None,
                    accepted_attempt_receipt_sha256=None,
                    status=(
                        ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER
                    ),
                    semantic_unresolved_scope_count=0,
                    reason_codes=("HOST_EMPTY_CONTAINER",),
                )
            )
            continue
        result = result_by_id[target.batch_id]
        semantic_count = int(
            result.model_candidate.scope_status
            is ParentProvisionObjectScopeStatus.UNRESOLVED
        )
        semantic_entry_count = sum(
            item.requirementness is Requirementness.UNRESOLVED
            for item in result.model_candidate.entries
        )
        attempt = build_parent_provision_recognition_attempt_receipt(
            **common,
            attempt_ordinal=1,
            task_binding_sha256=canonical_json_sha256(
                {"batch_id": target.batch_id, "attempt": 1}
            ),
            stage_session_attestation_sha256=session_sha256,
            stage_context_sha256=activation.context_sha256,
            skill_sha256=stage_binding.skill.sha256,
            runner_output_schema_sha256=activation.runner_output_schema_sha256,
            model_id=activation.session_attestation.model_id,
            reasoning_effort=activation.session_attestation.reasoning_effort,
            outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
            transport_result_sha256="1" * 64,
            response_sha256="2" * 64,
            tool_receipt_sha256="3" * 64,
            runtime_thread_id_sha256="4" * 64,
            runtime_turn_id_sha256="5" * 64,
            candidate_sha256=result.model_candidate_sha256,
            batch_compilation_result_sha256=result.result_sha256,
            semantic_unresolved_scope_count=semantic_count,
            semantic_unresolved_entry_count=semantic_entry_count,
            cleanup_status=ParentProvisionRecognitionCleanupStatus.SUCCEEDED,
            workspace_unchanged=True,
            reason_codes=(),
        )
        if result.model_candidate.scope_status is (
            ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
        ):
            status = (
                ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
            )
            reasons = ("ADJACENT_RECONCILIATION_REQUIRED",)
        elif semantic_count or semantic_entry_count:
            status = (
                ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
            )
            reasons = ()
        else:
            status = ParentProvisionRecognitionBatchStatus.ACCEPTED
            reasons = ()
        batches.append(
            build_parent_provision_recognition_batch_receipt(
                **common,
                attempts=(attempt,),
                accepted_attempt_id=attempt.attempt_id,
                accepted_attempt_receipt_sha256=attempt.receipt_sha256,
                status=status,
                semantic_unresolved_scope_count=semantic_count,
                semantic_unresolved_entry_count=semantic_entry_count,
                reason_codes=reasons,
            )
        )
    return tuple(batches)


def _aggregate_values(
    *,
    activation: CanonicalAppServerStageActivation,
    plan: ParentProvisionObjectBatchPlanV1,
    batches,  # type: ignore[no-untyped-def]
    policy_closure_sha256: str,
) -> dict[str, object]:
    session = activation.session_attestation
    return {
        "run_id": plan.run_id,
        "source_sha256": plan.source_sha256,
        "predecessor_artifact_id": plan.predecessor_artifact_id,
        "predecessor_artifact_sha256": plan.predecessor_artifact_sha256,
        "kit_bundle_sha256": plan.kit_bundle_sha256,
        "policy_closure_sha256": policy_closure_sha256,
        "runner_profile_sha256": session.runner_profile_sha256,
        "model_id": session.model_id,
        "reasoning_effort": session.reasoning_effort,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "planned_batch_count": len(plan.targets),
        "batches": batches,
    }


def _build_finalized(
    *,
    initial: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    aggregate: ParentProvisionObjectBatchAggregateV1,
    replaced_batch_ids: tuple[str, ...] = (),
    reconciliation_plan_sha256: str | None = None,
    reconciliation_execution_receipts_sha256: str | None = None,
):  # type: ignore[no-untyped-def]
    decision = aggregate.compiled_decision
    evidence_refs = parent_provision_evidence_refs(
        declared_logical_object_ids=decision.declared_logical_object_ids,
        unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
        entries=decision.entries,
    )
    return build_parent_provision_recognition_finalized_execution_receipt(
        initial_aggregate_execution_receipt=initial,
        global_aggregate_sha256=aggregate.aggregate_sha256,
        global_candidate_sha256=aggregate.global_candidate_sha256,
        parent_provision_decision_sha256=decision.decision_sha256,
        evidence_refs_sha256=canonical_json_sha256(evidence_refs),
        status=(
            ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC
            if decision.status is ParentProvisionStatus.PARTIAL
            else ParentProvisionRecognitionAggregateStatus.COMPLETE
        ),
        semantic_unresolved_scope_count=len(decision.unresolved_logical_object_ids),
        semantic_unresolved_entry_count=sum(
            entry.requirementness.value == "UNRESOLVED" for entry in decision.entries
        ),
        predecessor_partial=("PREDECESSOR_D2_PARTIAL" in decision.diagnostic_codes),
        replaced_batch_ids=replaced_batch_ids,
        reconciliation_plan_sha256=reconciliation_plan_sha256,
        reconciliation_execution_receipts_sha256=(
            reconciliation_execution_receipts_sha256
        ),
    )


def _finalize(scenario: _Scenario, **overrides):  # type: ignore[no-untyped-def]
    values = {
        "kit": scenario.kit,
        "activation": scenario.activation,
        "plan": scenario.plan,
        "initial_aggregate_execution_receipt": scenario.initial_receipt,
        "finalized_execution_receipt": scenario.finalized_receipt,
        "global_aggregate_sha256": scenario.aggregate.aggregate_sha256,
        "global_candidate": scenario.aggregate.global_candidate,
        "compiled_decision": scenario.aggregate.compiled_decision,
        "ordered_batch_result_sha256s": (
            scenario.aggregate.ordered_batch_result_sha256s
            if scenario.finalized_receipt.finalization_mode
            is ParentProvisionRecognitionFinalizationMode.DIRECT
            else None
        ),
        "d2_identity": scenario.identity,
        "source_segments": scenario.segments,
    }
    values.update(overrides)
    return finalize_parent_provision_professional_aggregate(**values)


def _failed_initial_receipt(
    scenario: _Scenario,
) -> ParentProvisionRecognitionAggregateExecutionReceiptV2:
    target = next(
        item
        for item in scenario.plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    common = {
        "run_id": scenario.plan.run_id,
        "source_sha256": scenario.plan.source_sha256,
        "predecessor_artifact_id": scenario.plan.predecessor_artifact_id,
        "predecessor_artifact_sha256": scenario.plan.predecessor_artifact_sha256,
        "kit_bundle_sha256": scenario.plan.kit_bundle_sha256,
        "plan_id": scenario.plan.plan_id,
        "plan_sha256": scenario.plan.plan_sha256,
        "batch_id": target.batch_id,
        "batch_order": target.batch_order,
        "target_scope_sha256": target.target_scope_sha256,
    }
    session = scenario.activation.session_attestation
    attempt = build_parent_provision_recognition_attempt_receipt(
        **common,
        attempt_ordinal=1,
        task_binding_sha256="9" * 64,
        stage_session_attestation_sha256=canonical_json_sha256(
            session.model_dump(mode="json")
        ),
        stage_context_sha256=scenario.activation.context_sha256,
        skill_sha256=session.stages[0].skill.sha256,
        runner_output_schema_sha256=(scenario.activation.runner_output_schema_sha256),
        model_id=session.model_id,
        reasoning_effort=session.reasoning_effort,
        outcome=ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
        transport_result_sha256=None,
        response_sha256=None,
        tool_receipt_sha256=None,
        runtime_thread_id_sha256=None,
        runtime_turn_id_sha256=None,
        candidate_sha256=None,
        batch_compilation_result_sha256=None,
        semantic_unresolved_scope_count=0,
        cleanup_status=ParentProvisionRecognitionCleanupStatus.NOT_REQUIRED,
        workspace_unchanged=True,
        reason_codes=("TRANSPORT_UNAVAILABLE",),
    )
    failed_batch = build_parent_provision_recognition_batch_receipt(
        **common,
        attempts=(attempt,),
        accepted_attempt_id=None,
        accepted_attempt_receipt_sha256=None,
        status=ParentProvisionRecognitionBatchStatus.FAILED,
        semantic_unresolved_scope_count=0,
        reason_codes=("RETRY_BUDGET_EXHAUSTED",),
    )
    batches = tuple(
        failed_batch if item.batch_id == target.batch_id else item
        for item in scenario.initial_receipt.batches
    )
    return build_parent_provision_recognition_aggregate_execution_receipt(
        **_aggregate_values(
            activation=scenario.activation,
            plan=scenario.plan,
            batches=batches,
            policy_closure_sha256=(scenario.initial_receipt.policy_closure_sha256),
        ),
        status=ParentProvisionRecognitionAggregateStatus.FAILED,
        semantic_unresolved_scope_count=0,
        object_batch_aggregate_sha256=None,
        decision_candidate_sha256=None,
        parent_provision_decision_sha256=None,
        evidence_refs_sha256=None,
        reason_codes=("BATCH_EXECUTION_FAILED",),
    )


def _incomplete_initial_receipt(
    scenario: _Scenario,
) -> ParentProvisionRecognitionAggregateExecutionReceiptV2:
    first = scenario.initial_receipt.batches[0]
    second_target = scenario.plan.targets[1]
    cancelled = build_parent_provision_recognition_batch_receipt(
        run_id=scenario.plan.run_id,
        source_sha256=scenario.plan.source_sha256,
        predecessor_artifact_id=scenario.plan.predecessor_artifact_id,
        predecessor_artifact_sha256=scenario.plan.predecessor_artifact_sha256,
        kit_bundle_sha256=scenario.plan.kit_bundle_sha256,
        plan_id=scenario.plan.plan_id,
        plan_sha256=scenario.plan.plan_sha256,
        batch_id=second_target.batch_id,
        batch_order=second_target.batch_order,
        target_scope_sha256=second_target.target_scope_sha256,
        attempts=(),
        accepted_attempt_id=None,
        accepted_attempt_receipt_sha256=None,
        status=ParentProvisionRecognitionBatchStatus.CANCELLED,
        semantic_unresolved_scope_count=0,
        reason_codes=("USER_CANCELLED_BEFORE_ATTEMPT",),
    )
    return build_parent_provision_recognition_aggregate_execution_receipt(
        **_aggregate_values(
            activation=scenario.activation,
            plan=scenario.plan,
            batches=(first, cancelled),
            policy_closure_sha256=(scenario.initial_receipt.policy_closure_sha256),
        ),
        status=ParentProvisionRecognitionAggregateStatus.CANCELLED,
        semantic_unresolved_scope_count=0,
        object_batch_aggregate_sha256=None,
        decision_candidate_sha256=None,
        parent_provision_decision_sha256=None,
        evidence_refs_sha256=None,
        reason_codes=("USER_CANCELLED",),
    )


def _activation(kit: PortableAgentKit, identity) -> CanonicalAppServerStageActivation:  # type: ignore[no-untyped-def]
    closure = compile_d3_parent_provision_policy_closure(kit)
    profile = kit.runner_profile("app-server-product")
    predecessor = CanonicalStagePredecessorBindingV1(
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        predecessor_stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        run_id=identity.run_id,
        source_sha256=identity.source_sha256,
        artifact_id=identity.artifact_id,
        artifact_sha256=identity.artifact_sha256,
        validator_receipt_sha256=identity.validator_receipt_sha256,
        proposal_schema_id="logical-structure-proposal/v1",
        status=(
            AgentStageArtifactStatus.PARTIAL
            if identity.status is ParentProvisionStatus.PARTIAL
            else AgentStageArtifactStatus.VALIDATED
        ),
    )
    stage_input_values = {
        "content_free": True,
        "stage": AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        "run_id": identity.run_id,
        "source_sha256": identity.source_sha256,
        "kit_bundle_sha256": kit.identity.bundle_sha256,
        "predecessor": D3StageInputPredecessorBindingV1.model_validate(
            predecessor.model_dump(mode="json")
        ),
        "d2_identity_sha256": canonical_json_sha256(identity.model_dump(mode="json")),
        "source_scope_sha256": identity.source_scope_sha256,
        "files": (
            D3StageInputFileDigestV1(
                path="d2-logical-structure-proposal.json", sha256="1" * 64
            ),
            D3StageInputFileDigestV1(
                path="d2-logical-structure-validation-receipt.json",
                sha256="2" * 64,
            ),
            D3StageInputFileDigestV1(
                path="d3-exact-source-segments.json", sha256="3" * 64
            ),
        ),
    }
    stage_input_draft = D3StageInputBundleAttestationV1.model_construct(
        attestation_id="d3-stage-input-" + "0" * 32,
        bundle_sha256="0" * 64,
        **stage_input_values,
    )
    stage_input_sha256 = canonical_json_sha256(
        stage_input_draft.model_dump(
            mode="json", exclude={"attestation_id", "bundle_sha256"}
        )
    )
    stage_input = D3StageInputBundleAttestationV1(
        attestation_id=f"d3-stage-input-{stage_input_sha256[:32]}",
        bundle_sha256=stage_input_sha256,
        **stage_input_values,
    )
    stage_spec = next(
        item
        for item in kit.manifest.stages
        if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    stage_attestation = CanonicalAppServerStageAttestationV1(
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        implementation_status="AVAILABLE",
        context_kind="EXECUTABLE_STAGE_CONTEXT",
        skill_id=stage_spec.skill_id,
        skill=_file_digest(kit, closure.skill.path),
        policy=_file_digest(kit, closure.policy.path),
        references=tuple(_file_digest(kit, item.path) for item in closure.references),
        context_sha256="4" * 64,
        artifact_schema_id=stage_spec.artifact_schema_id,
        artifact_schema=_file_digest(kit, stage_spec.artifact_schema_path),
        runner_output_schema_id=stage_spec.runner_output_schema_id,
        runner_output_schema=_file_digest(kit, stage_spec.runner_output_schema_path),
        validator_id=stage_spec.validator_id,
        validator_version=stage_spec.validator_version,
        required_tools=(),
        inference_available=True,
        diagnostic_codes=(),
    )
    runtime = CanaryRuntimeState(
        codex_home=kit.root,
        command=("codex", "app-server"),
        environment={},
        log_dir=kit.root,
        protocol={"codex_version": "test-runtime"},
        sqlite_home=kit.root,
        state_path=kit.root,
        sterile_cwd=kit.root,
        tmp_dir=kit.root,
    )
    document_evidence = CanonicalDocumentEvidenceAttestationV1(
        run_id=identity.run_id,
        opaque_source_handle="source-handle-" + "5" * 32,
        source_sha256=identity.source_sha256,
        document_role="SYNTHETIC_TEST",
        page_count=2,
        source_ir_id=identity.source_ir_id,
        source_ir_canonical_sha256=identity.source_ir_canonical_sha256,
        workspace_bundle_sha256="6" * 64,
        evidence_inventory_sha256="7" * 64,
    )
    session_values = {
        "session_id": "canonical-document-session-" + "8" * 32,
        "content_free": True,
        "kit_identity": kit.identity,
        "runner_profile_id": "app-server-product",
        "runner_profile_sha256": kit.runner_profile_sha256["app-server-product"],
        "runner_kind": profile.runner_kind,
        "adapter_id": profile.adapter_id,
        "adapter_version": profile.adapter_version,
        "model_id": profile.model_id,
        "reasoning_effort": profile.reasoning_effort,
        "domain_logic_source": "PORTABLE_AGENT_KIT",
        "exact_bundle_materialized": True,
        "private_runtime_state": True,
        "runtime_assets_copied": False,
        "agent_native_read_capability": CanonicalAgentNativeReadCapabilityV1(
            status="ATTESTED",
            shell_tool_enabled=True,
            unified_exec_enabled=True,
            view_image_enabled=True,
            local_image_input_enabled=True,
            session_root_read_confinement_attested=True,
            network_access_enabled=False,
            filesystem_write_enabled=False,
            real_transport_eligible=True,
            diagnostic_codes=(),
        ),
        "native_runtime_policy": AgentNativeReadOnlyRuntimePolicyV1(
            readable_root_path_sha256=hashlib.sha256(
                str(kit.root.resolve()).encode("utf-8")
            ).hexdigest()
        ),
        "document_evidence": document_evidence,
        "stage_predecessors": (predecessor,),
        "d3_stage_input": stage_input,
        "workspace_manifest_sha256": "9" * 64,
        "workspace_manifest_unchanged": True,
        "stages": (stage_attestation,),
        "d3_policy_closure_sha256": canonical_json_sha256(
            closure.model_dump(mode="json")
        ),
    }
    session_draft = CanonicalAppServerDocumentSessionAttestationV1.model_construct(
        attestation_id="canonical-session-attestation-" + "0" * 32,
        **session_values,
    )
    session_sha256 = canonical_json_sha256(
        session_draft.model_dump(mode="json", exclude={"attestation_id"})
    )
    session = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=f"canonical-session-attestation-{session_sha256[:32]}",
        **session_values,
    )
    variant = stage_spec.runner_output_schema_variants[0]
    return CanonicalAppServerStageActivation(
        runtime=runtime,
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        skill_paths={stage_spec.skill_id: kit.root / closure.skill.path},
        context_text="SYNTHETIC PROFESSIONAL POLICY CONTEXT",
        context_sha256=stage_attestation.context_sha256,
        artifact_schema_path=kit.root / stage_spec.artifact_schema_path,
        artifact_schema_sha256=_sha256(kit.root / stage_spec.artifact_schema_path),
        runner_output_schema_id=variant.schema_id,
        runner_output_schema_path=kit.root / variant.schema_path,
        runner_output_schema_sha256=_sha256(kit.root / variant.schema_path),
        validator_id=stage_spec.validator_id,
        validator_version=stage_spec.validator_version,
        session_attestation=session,
        d3_policy_closure=closure,
        predecessor=predecessor,
        d3_stage_input_attestation=stage_input,
        runner_output_execution_mode="OBJECT_BATCH",
    )


def _file_digest(kit: PortableAgentKit, relative: str) -> CanonicalStageFileDigestV1:
    return CanonicalStageFileDigestV1(
        path=relative,
        sha256=_sha256(kit.root / relative),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
