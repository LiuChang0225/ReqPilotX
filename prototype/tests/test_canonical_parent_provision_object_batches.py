from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
    CanonicalSameEvidenceParentProvisionReplanner,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectExecutionMode,
)
from app.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionBatchStatus,
    ParentProvisionRecognitionCleanupStatus,
    build_parent_provision_recognition_aggregate_execution_receipt,
    build_parent_provision_recognition_attempt_receipt,
    build_parent_provision_recognition_batch_receipt,
    canonical_json_sha256,
)
from app.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceGap,
    ParentProvisionEvidenceRecoverability,
    ParentProvisionEvidenceRecoveryStrategy,
)
from app.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryOutcome,
    ParentProvisionEvidenceRecoveryState,
)
from app.ports.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryExecutionResultV1,
)

if __package__:
    from .test_parent_provision_object_batches import (
        STAGE_INPUT_BUNDLE_SHA256,
        _closed_candidate,
        _entry_for,
        _fixture,
    )
else:
    from test_parent_provision_object_batches import (
        STAGE_INPUT_BUNDLE_SHA256,
        _closed_candidate,
        _entry_for,
        _fixture,
    )


class CanonicalParentProvisionObjectBatchCompilerTests(unittest.TestCase):
    def test_host_seam_builds_compiles_and_aggregates_without_driver_switch(
        self,
    ) -> None:
        fixture = _fixture()
        host = CanonicalParentProvisionObjectBatchCompiler(
            d2_identity=fixture.identity,
            logical_objects=fixture.objects,
            source_segments=fixture.segments,
            stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
            kit_identity=fixture.kit,
        )

        plan = host.build_plan()
        model_targets = tuple(
            target
            for target in plan.targets
            if target.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
        )
        results = tuple(
            host.compile_object(
                plan=plan,
                batch_id=target.batch_id,
                candidate=_closed_candidate(
                    _entry_for(
                        fixture.segments[target.segment_ordinals[0]],
                        ordinal=target.segment_ordinals[0],
                    )
                ),
            )
            for target in model_targets
        )
        aggregate = host.aggregate(plan=plan, results=tuple(reversed(results)))

        self.assertEqual(
            aggregate.compiled_decision.declared_logical_object_ids,
            fixture.identity.logical_object_ids,
        )
        self.assertEqual(len(aggregate.compiled_decision.entries), 2)
        self.assertEqual(
            aggregate.ordered_batch_result_sha256s,
            tuple(item.result_sha256 for item in results),
        )

    def test_host_results_bind_directly_into_attempt_batch_and_aggregate_receipts(
        self,
    ) -> None:
        fixture = _fixture()
        host = CanonicalParentProvisionObjectBatchCompiler(
            d2_identity=fixture.identity,
            logical_objects=fixture.objects,
            source_segments=fixture.segments,
            stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
            kit_identity=fixture.kit,
        )
        plan = host.build_plan()
        results_by_batch = {}
        for target in plan.targets:
            if (
                target.execution_mode
                is not ParentProvisionObjectExecutionMode.MODEL_TARGET
            ):
                continue
            result = host.compile_object(
                plan=plan,
                batch_id=target.batch_id,
                candidate=_closed_candidate(
                    _entry_for(
                        fixture.segments[target.segment_ordinals[0]],
                        ordinal=target.segment_ordinals[0],
                    )
                ),
            )
            results_by_batch[target.batch_id] = result
        object_aggregate = host.aggregate(
            plan=plan,
            results=tuple(reversed(tuple(results_by_batch.values()))),
        )

        batch_receipts = []
        for target in plan.targets:
            if (
                target.execution_mode
                is ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
            ):
                batch_receipts.append(
                    build_parent_provision_recognition_batch_receipt(
                        run_id=fixture.identity.run_id,
                        source_sha256=fixture.identity.source_sha256,
                        predecessor_artifact_id=fixture.identity.artifact_id,
                        predecessor_artifact_sha256=fixture.identity.artifact_sha256,
                        kit_bundle_sha256=fixture.kit.bundle_sha256,
                        plan_id=plan.plan_id,
                        plan_sha256=plan.plan_sha256,
                        batch_id=target.batch_id,
                        batch_order=target.batch_order,
                        target_scope_sha256=target.target_scope_sha256,
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
            result = results_by_batch[target.batch_id]
            attempt = build_parent_provision_recognition_attempt_receipt(
                run_id=fixture.identity.run_id,
                source_sha256=fixture.identity.source_sha256,
                predecessor_artifact_id=fixture.identity.artifact_id,
                predecessor_artifact_sha256=fixture.identity.artifact_sha256,
                kit_bundle_sha256=fixture.kit.bundle_sha256,
                plan_id=plan.plan_id,
                plan_sha256=plan.plan_sha256,
                batch_id=target.batch_id,
                batch_order=target.batch_order,
                target_scope_sha256=target.target_scope_sha256,
                attempt_ordinal=1,
                task_binding_sha256=canonical_json_sha256(
                    {"plan": plan.plan_sha256, "batch": target.batch_id, "attempt": 1}
                ),
                stage_session_attestation_sha256="1" * 64,
                stage_context_sha256="2" * 64,
                skill_sha256="3" * 64,
                runner_output_schema_sha256="4" * 64,
                model_id="gpt-5.6-sol",
                reasoning_effort="high",
                outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
                transport_result_sha256="5" * 64,
                response_sha256="6" * 64,
                tool_receipt_sha256="7" * 64,
                runtime_thread_id_sha256="8" * 64,
                runtime_turn_id_sha256="9" * 64,
                candidate_sha256=result.model_candidate_sha256,
                batch_compilation_result_sha256=result.result_sha256,
                semantic_unresolved_scope_count=0,
                cleanup_status=ParentProvisionRecognitionCleanupStatus.SUCCEEDED,
                workspace_unchanged=True,
                reason_codes=(),
            )
            batch_receipts.append(
                build_parent_provision_recognition_batch_receipt(
                    run_id=fixture.identity.run_id,
                    source_sha256=fixture.identity.source_sha256,
                    predecessor_artifact_id=fixture.identity.artifact_id,
                    predecessor_artifact_sha256=fixture.identity.artifact_sha256,
                    kit_bundle_sha256=fixture.kit.bundle_sha256,
                    plan_id=plan.plan_id,
                    plan_sha256=plan.plan_sha256,
                    batch_id=target.batch_id,
                    batch_order=target.batch_order,
                    target_scope_sha256=target.target_scope_sha256,
                    attempts=(attempt,),
                    accepted_attempt_id=attempt.attempt_id,
                    accepted_attempt_receipt_sha256=attempt.receipt_sha256,
                    status=ParentProvisionRecognitionBatchStatus.ACCEPTED,
                    semantic_unresolved_scope_count=0,
                    reason_codes=(),
                )
            )

        execution = build_parent_provision_recognition_aggregate_execution_receipt(
            run_id=fixture.identity.run_id,
            source_sha256=fixture.identity.source_sha256,
            predecessor_artifact_id=fixture.identity.artifact_id,
            predecessor_artifact_sha256=fixture.identity.artifact_sha256,
            kit_bundle_sha256=fixture.kit.bundle_sha256,
            policy_closure_sha256="a" * 64,
            runner_profile_sha256="b" * 64,
            model_id="gpt-5.6-sol",
            reasoning_effort="high",
            plan_id=plan.plan_id,
            plan_sha256=plan.plan_sha256,
            planned_batch_count=len(plan.targets),
            batches=tuple(batch_receipts),
            status=ParentProvisionRecognitionAggregateStatus.COMPLETE,
            semantic_unresolved_scope_count=0,
            object_batch_aggregate_sha256=object_aggregate.aggregate_sha256,
            decision_candidate_sha256=object_aggregate.global_candidate_sha256,
            parent_provision_decision_sha256=(
                object_aggregate.compiled_decision.decision_sha256
            ),
            evidence_refs_sha256=canonical_json_sha256(
                tuple(
                    sorted(
                        {
                            ref
                            for entry in object_aggregate.compiled_decision.entries
                            for ref in entry.evidence_refs
                        }
                    )
                )
            ),
            reason_codes=(),
        )

        self.assertEqual(len(execution.batches), len(plan.targets))
        self.assertEqual(
            len(execution.accepted_attempt_ids),
            len(results_by_batch),
        )
        self.assertEqual(
            execution.object_batch_aggregate_sha256,
            object_aggregate.aggregate_sha256,
        )

    def test_host_seam_routes_partial_evidence_to_a_finite_recovery_session(
        self,
    ) -> None:
        fixture = _fixture()
        host = CanonicalParentProvisionObjectBatchCompiler(
            d2_identity=fixture.identity,
            logical_objects=fixture.objects,
            source_segments=fixture.segments,
            stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
            kit_identity=fixture.kit,
        )
        plan = host.build_plan()
        target = next(
            item
            for item in plan.targets
            if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
        )

        recovery_object_plan, recovery_plan, session = host.require_evidence_recovery(
            plan=plan,
            logical_object_id=target.logical_object_id,
            gap=ParentProvisionEvidenceGap.MISSING_PAGE_RENDER,
            recoverability=ParentProvisionEvidenceRecoverability.AUTOMATIC,
            trigger_failure_code="PAGE_RENDER_MISSING",
            ordered_strategies=(
                ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES,
                ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            ),
        )
        recovery_target = next(
            item
            for item in recovery_object_plan.targets
            if item.logical_object_id == target.logical_object_id
        )

        self.assertEqual(
            recovery_target.execution_mode,
            ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
        )
        self.assertEqual(
            recovery_target.target_evidence_sha256,
            target.target_evidence_sha256,
        )
        self.assertEqual(recovery_plan.max_attempts, 2)
        self.assertEqual(session.plan, recovery_plan)

        already_required_plan, already_required_recovery, _ = (
            host.require_evidence_recovery(
                plan=recovery_object_plan,
                logical_object_id=target.logical_object_id,
                gap=ParentProvisionEvidenceGap.MISSING_PAGE_RENDER,
                recoverability=ParentProvisionEvidenceRecoverability.AUTOMATIC,
                trigger_failure_code="PAGE_RENDER_MISSING",
                ordered_strategies=(
                    ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES,
                    (ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE),
                ),
            )
        )
        self.assertEqual(already_required_plan, recovery_object_plan)
        self.assertEqual(
            already_required_recovery.object_plan_sha256,
            recovery_object_plan.plan_sha256,
        )

        execution_target = host.build_evidence_recovery_execution_target(
            plan=recovery_object_plan,
            logical_object_id=target.logical_object_id,
            source_handle="source-handle-" + "f" * 32,
        )
        logical_object = next(
            item
            for item in fixture.objects
            if item.logical_object_id == target.logical_object_id
        )
        self.assertEqual(execution_target.page_numbers, logical_object.page_numbers)

        new_stage_input_sha256 = "f" * 64
        execution_result = ParentProvisionEvidenceRecoveryExecutionResultV1(
            request_sha256="e" * 64,
            attempt_id=("parent-provision-evidence-recovery-attempt-" + "d" * 32),
            strategy=(ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES),
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            new_stage_input_bundle_sha256=new_stage_input_sha256,
            new_target_evidence_sha256=recovery_target.target_evidence_sha256,
        )
        replanned = CanonicalSameEvidenceParentProvisionReplanner(
            d2_identity=fixture.identity,
            logical_objects=fixture.objects,
            source_segments=fixture.segments,
        ).rebuild_object_plan(
            old_plan=recovery_object_plan,
            session=session,
            execution_result=execution_result,
        )
        replanned_target = next(
            item
            for item in replanned.targets
            if item.logical_object_id == target.logical_object_id
        )
        self.assertEqual(
            replanned_target.execution_mode,
            ParentProvisionObjectExecutionMode.MODEL_TARGET,
        )
        self.assertEqual(
            replanned_target.target_evidence_sha256,
            recovery_target.target_evidence_sha256,
        )


if __name__ == "__main__":
    unittest.main()
