"""Canonical object-batched parent-recognition workflow orchestration.

The orchestrator only connects already-validated stage drivers and Host gates.
It never turns execution failure into semantic uncertainty and never publishes a
proposal before the direct or reconciled aggregate has a professional proof.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Callable

from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerStageActivation,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageSessionPort,
    CanonicalStructuredStageTransportPort,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from reqpilotx_server.adapters.agents.codex_app_server.parent_provision_object_driver import (
    PARENT_PROVISION_OBJECT_EXECUTION_MODE,
    PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
    ParentProvisionObjectStageDriver,
    ParentProvisionObjectStageRunResult,
)
from reqpilotx_server.adapters.agents.codex_app_server.parent_provision_professional_aggregate import (
    ParentProvisionProfessionalAggregateError,
    ParentProvisionProfessionalAggregateResult,
    ParentProvisionRecognitionFinalizedExecutionReceiptV1,
    build_nested_evidence_assignment_closure_for_activation,
    build_parent_provision_recognition_finalized_execution_receipt,
    finalize_parent_provision_professional_aggregate,
)
from reqpilotx_server.adapters.agents.codex_app_server.parent_provision_reconciliation_driver import (
    ParentProvisionObjectReconciliationStageDriver,
    ParentProvisionObjectReconciliationStageRunResult,
)
from reqpilotx_server.adapters.agents.portable_kit import PortableAgentKit
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    ParentProvisionStatus,
    Requirementness,
    parent_provision_evidence_refs,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage
from reqpilotx_server.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionBatchReceiptV1,
    ParentProvisionRecognitionBatchStatus,
    canonical_json_sha256,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchPlanV1,
)
from reqpilotx_server.application.agent_workflow.parent_provision_nested_evidence import (
    ParentProvisionNestedEvidenceAssignmentClosureV1,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_reconciliation import (
    ParentProvisionObjectReconciliationCompilationV1,
    ParentProvisionObjectReconciliationGroupV1,
    ParentProvisionObjectReconciliationPlanV1,
)
from reqpilotx_server.application.agent_workflow.parent_provision_reconciliation_receipts import (
    ParentProvisionReconciliationAggregateReceiptV1,
    ParentProvisionReconciliationAggregateStatus,
    ParentProvisionReconciliationGroupReceiptV1,
)


_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CanonicalParentProvisionObjectWorkflowResult:
    object_result: ParentProvisionObjectStageRunResult
    coordination_result: ParentProvisionObjectReconciliationStageRunResult | None
    finalized_execution_receipt: (
        ParentProvisionRecognitionFinalizedExecutionReceiptV1 | None
    )
    professional_result: ParentProvisionProfessionalAggregateResult | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.reason_codes != tuple(sorted(set(self.reason_codes))) or any(
            _MACHINE_CODE_PATTERN.fullmatch(item) is None for item in self.reason_codes
        ):
            raise ValueError(
                "workflow reason codes must be sorted unique machine codes"
            )
        published = (
            self.finalized_execution_receipt is not None
            and self.professional_result is not None
        )
        if (self.finalized_execution_receipt is None) != (
            self.professional_result is None
        ):
            raise ValueError("finalized execution and professional result are atomic")
        if published == bool(self.reason_codes):
            raise ValueError(
                "published workflow cannot carry blockers and incomplete workflow needs reasons"
            )

    @property
    def publication_ready(self) -> bool:
        return self.professional_result is not None


class CanonicalParentProvisionObjectWorkflowOrchestrator:
    """Connect object execution, optional reconciliation, and professional finalization."""

    def __init__(
        self,
        *,
        kit: PortableAgentKit,
        object_compiler: CanonicalParentProvisionObjectBatchCompiler,
        object_transport: CanonicalStructuredStageTransportPort,
        reconciliation_transport: CanonicalStructuredStageTransportPort,
        max_attempts_per_object: int = 2,
        max_attempts_per_group: int = 2,
        max_targets_per_turn: int = 1,
        max_source_characters_per_turn: int = 60_000,
        window_checkpoint_sink: Callable[
            [
                tuple[
                    tuple[
                        ParentProvisionRecognitionBatchReceiptV1,
                        ParentProvisionObjectBatchCompilationV1,
                    ],
                    ...,
                ]
            ],
            None,
        ]
        | None = None,
        reconciliation_checkpoint_sink: Callable[
            [
                ParentProvisionObjectReconciliationPlanV1,
                ParentProvisionObjectReconciliationGroupV1,
                ParentProvisionReconciliationGroupReceiptV1,
                ParentProvisionObjectReconciliationCompilationV1,
            ],
            None,
        ]
        | None = None,
    ) -> None:
        self._kit = kit
        self._object_compiler = object_compiler
        self._object_execution_mode = (
            PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE
            if max_targets_per_turn > 1
            else PARENT_PROVISION_OBJECT_EXECUTION_MODE
        )
        self._object_driver = ParentProvisionObjectStageDriver(
            transport=object_transport,
            object_compiler=object_compiler,
            max_attempts_per_object=max_attempts_per_object,
            max_targets_per_turn=max_targets_per_turn,
            max_source_characters_per_turn=max_source_characters_per_turn,
            window_checkpoint_sink=window_checkpoint_sink,
        )
        self._reconciliation_driver = ParentProvisionObjectReconciliationStageDriver(
            transport=reconciliation_transport,
            object_compiler=object_compiler,
            max_attempts_per_group=max_attempts_per_group,
            group_checkpoint_sink=reconciliation_checkpoint_sink,
        )

    def run(
        self,
        session: CanonicalStageSessionPort,
        *,
        object_task_binding_sha256: str,
        reconciliation_task_binding_sha256: str | None = None,
        plan: ParentProvisionObjectBatchPlanV1 | None = None,
        resumed_compilations: tuple[ParentProvisionObjectBatchCompilationV1, ...] = (),
        resumed_batch_receipts: tuple[
            ParentProvisionRecognitionBatchReceiptV1, ...
        ] = (),
        resumed_reconciliation_receipts: tuple[
            ParentProvisionReconciliationGroupReceiptV1, ...
        ] = (),
        resumed_reconciliation_compilations: tuple[
            ParentProvisionObjectReconciliationCompilationV1, ...
        ] = (),
    ) -> CanonicalParentProvisionObjectWorkflowResult:
        object_result = self._run_objects(
            session,
            task_binding_sha256=object_task_binding_sha256,
            plan=plan,
            resumed_compilations=resumed_compilations,
            resumed_batch_receipts=resumed_batch_receipts,
        )
        if object_result.object_batch_aggregate is not None:
            try:
                return self._finalize_direct(session, object_result=object_result)
            except ParentProvisionProfessionalAggregateError as exc:
                return _incomplete_result(
                    object_result=object_result,
                    coordination_result=None,
                    reason_codes=(_professional_failure_reason(exc),),
                )

        if not _requires_only_reconciliation(object_result):
            return _incomplete_result(
                object_result=object_result,
                coordination_result=None,
                reason_codes=_object_incomplete_reason_codes(object_result),
            )
        if reconciliation_task_binding_sha256 is None:
            return _incomplete_result(
                object_result=object_result,
                coordination_result=None,
                reason_codes=("OBJECT_RECONCILIATION_TASK_BINDING_MISSING",),
            )

        try:
            coordination_result = self._run_reconciliation(
                session,
                initial_result=object_result,
                task_binding_sha256=reconciliation_task_binding_sha256,
                resumed_receipts=resumed_reconciliation_receipts,
                resumed_compilations=resumed_reconciliation_compilations,
            )
        except LifecycleProbeError as exc:
            return _incomplete_result(
                object_result=object_result,
                coordination_result=None,
                reason_codes=(_machine_code(exc.reason_code),),
            )
        except Exception as exc:
            logger.warning(
                "Parent reconciliation orchestration failed. error_type=%s error=%s",
                type(exc).__name__,
                re.sub(r"\s+", " ", str(exc).strip())[:500],
            )
            return _incomplete_result(
                object_result=object_result,
                coordination_result=None,
                reason_codes=("OBJECT_RECONCILIATION_ORCHESTRATION_FAILED",),
            )
        if coordination_result.reconciliation_aggregate is None:
            return _incomplete_result(
                object_result=object_result,
                coordination_result=coordination_result,
                reason_codes=_coordination_incomplete_reason_codes(coordination_result),
            )
        try:
            return self._finalize_reconciled(
                session,
                object_result=object_result,
                coordination_result=coordination_result,
            )
        except ParentProvisionProfessionalAggregateError as exc:
            return _incomplete_result(
                object_result=object_result,
                coordination_result=coordination_result,
                reason_codes=(_professional_failure_reason(exc),),
            )

    def _run_objects(
        self,
        session: CanonicalStageSessionPort,
        *,
        task_binding_sha256: str,
        plan: ParentProvisionObjectBatchPlanV1 | None,
        resumed_compilations: tuple[ParentProvisionObjectBatchCompilationV1, ...],
        resumed_batch_receipts: tuple[ParentProvisionRecognitionBatchReceiptV1, ...],
    ) -> ParentProvisionObjectStageRunResult:
        return self._object_driver.run(
            session,
            task_binding_sha256=task_binding_sha256,
            plan=plan,
            resumed_compilations=resumed_compilations,
            resumed_batch_receipts=resumed_batch_receipts,
        )

    def _run_reconciliation(
        self,
        session: CanonicalStageSessionPort,
        *,
        initial_result: ParentProvisionObjectStageRunResult,
        task_binding_sha256: str,
        resumed_receipts: tuple[ParentProvisionReconciliationGroupReceiptV1, ...],
        resumed_compilations: tuple[
            ParentProvisionObjectReconciliationCompilationV1, ...
        ],
    ) -> ParentProvisionObjectReconciliationStageRunResult:
        return self._reconciliation_driver.run(
            session,
            initial_result=initial_result,
            task_binding_sha256=task_binding_sha256,
            resumed_group_receipts=resumed_receipts,
            resumed_group_compilations=resumed_compilations,
        )

    def _finalize_direct(
        self,
        session: CanonicalStageSessionPort,
        *,
        object_result: ParentProvisionObjectStageRunResult,
    ) -> CanonicalParentProvisionObjectWorkflowResult:
        aggregate = object_result.object_batch_aggregate
        if aggregate is None:
            raise AssertionError("direct finalization requires a working aggregate")
        decision = aggregate.compiled_decision
        evidence_refs_sha256 = _evidence_refs_sha256(decision)
        activation = self._professional_activation(session)
        nested_assignment = build_nested_evidence_assignment_closure_for_activation(
            activation=activation,
            decision=decision,
            source_segments=self._object_compiler.source_segments,
        )
        finalized = build_parent_provision_recognition_finalized_execution_receipt(
            initial_aggregate_execution_receipt=object_result.execution_receipt,
            global_aggregate_sha256=aggregate.aggregate_sha256,
            global_candidate_sha256=aggregate.global_candidate_sha256,
            parent_provision_decision_sha256=decision.decision_sha256,
            evidence_refs_sha256=evidence_refs_sha256,
            status=_final_status(decision.status),
            semantic_unresolved_scope_count=len(decision.unresolved_logical_object_ids),
            semantic_unresolved_entry_count=_unresolved_entry_count(decision),
            predecessor_partial=_predecessor_partial(decision),
            nested_evidence_assignment_closure=nested_assignment,
        )
        professional = self._professional_finalize(
            activation,
            object_result=object_result,
            finalized=finalized,
            global_aggregate_sha256=aggregate.aggregate_sha256,
            global_candidate=aggregate.global_candidate,
            compiled_decision=decision,
            ordered_batch_result_sha256s=aggregate.ordered_batch_result_sha256s,
            nested_evidence_assignment_closure=nested_assignment,
        )
        return CanonicalParentProvisionObjectWorkflowResult(
            object_result=object_result,
            coordination_result=None,
            finalized_execution_receipt=finalized,
            professional_result=professional,
            reason_codes=(),
        )

    def _finalize_reconciled(
        self,
        session: CanonicalStageSessionPort,
        *,
        object_result: ParentProvisionObjectStageRunResult,
        coordination_result: ParentProvisionObjectReconciliationStageRunResult,
    ) -> CanonicalParentProvisionObjectWorkflowResult:
        receipt, aggregate = _validated_reconciliation_success(
            object_result=object_result,
            coordination_result=coordination_result,
        )
        decision = aggregate.compiled_decision
        replaced_ids = {
            replacement.batch_id
            for group in coordination_result.reconciliation_plan.groups
            for replacement in group.replacements
        }
        source_ordered_replaced_ids = tuple(
            target.batch_id
            for target in object_result.plan.targets
            if target.batch_id in replaced_ids
        )
        activation = self._professional_activation(session)
        nested_assignment = build_nested_evidence_assignment_closure_for_activation(
            activation=activation,
            decision=decision,
            source_segments=self._object_compiler.source_segments,
        )
        finalized = build_parent_provision_recognition_finalized_execution_receipt(
            initial_aggregate_execution_receipt=object_result.execution_receipt,
            global_aggregate_sha256=aggregate.aggregate_sha256,
            global_candidate_sha256=aggregate.global_candidate_sha256,
            parent_provision_decision_sha256=decision.decision_sha256,
            evidence_refs_sha256=_evidence_refs_sha256(decision),
            status=_final_status(decision.status),
            semantic_unresolved_scope_count=len(decision.unresolved_logical_object_ids),
            semantic_unresolved_entry_count=_unresolved_entry_count(decision),
            predecessor_partial=_predecessor_partial(decision),
            replaced_batch_ids=source_ordered_replaced_ids,
            reconciliation_plan_sha256=(
                coordination_result.reconciliation_plan.reconciliation_plan_sha256
            ),
            reconciliation_execution_receipts_sha256=receipt.receipt_sha256,
            nested_evidence_assignment_closure=nested_assignment,
        )
        professional = self._professional_finalize(
            activation,
            object_result=object_result,
            finalized=finalized,
            global_aggregate_sha256=aggregate.aggregate_sha256,
            global_candidate=aggregate.global_candidate,
            compiled_decision=decision,
            ordered_batch_result_sha256s=None,
            nested_evidence_assignment_closure=nested_assignment,
        )
        return CanonicalParentProvisionObjectWorkflowResult(
            object_result=object_result,
            coordination_result=coordination_result,
            finalized_execution_receipt=finalized,
            professional_result=professional,
            reason_codes=(),
        )

    def _professional_finalize(
        self,
        activation: CanonicalAppServerStageActivation,
        *,
        object_result: ParentProvisionObjectStageRunResult,
        finalized: ParentProvisionRecognitionFinalizedExecutionReceiptV1,
        global_aggregate_sha256: str,
        global_candidate: object,
        compiled_decision: object,
        ordered_batch_result_sha256s: tuple[str, ...] | None,
        nested_evidence_assignment_closure: (
            ParentProvisionNestedEvidenceAssignmentClosureV1 | None
        ),
    ) -> ParentProvisionProfessionalAggregateResult:
        # Professional finalization must bind the initial OBJECT_BATCH schema and
        # attempt receipts, never the later reconciliation output schema.
        try:
            return finalize_parent_provision_professional_aggregate(
                kit=self._kit,
                activation=activation,
                plan=object_result.plan,
                initial_aggregate_execution_receipt=(object_result.execution_receipt),
                finalized_execution_receipt=finalized,
                global_aggregate_sha256=global_aggregate_sha256,
                global_candidate=global_candidate,
                compiled_decision=compiled_decision,
                ordered_batch_result_sha256s=ordered_batch_result_sha256s,
                d2_identity=self._object_compiler.d2_identity,
                source_segments=self._object_compiler.source_segments,
                nested_evidence_assignment_closure=(nested_evidence_assignment_closure),
            )
        except ParentProvisionProfessionalAggregateError:
            raise
        except Exception as exc:
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_PROFESSIONAL_FINALIZATION_FAILED"
            ) from exc

    def _professional_activation(
        self,
        session: CanonicalStageSessionPort,
    ) -> CanonicalAppServerStageActivation:
        return session.require_inference_stage(
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            execution_mode=self._object_execution_mode,
        )


def _validated_reconciliation_success(
    *,
    object_result: ParentProvisionObjectStageRunResult,
    coordination_result: ParentProvisionObjectReconciliationStageRunResult,
):  # type: ignore[no-untyped-def]
    try:
        receipt = ParentProvisionReconciliationAggregateReceiptV1.model_validate(
            coordination_result.execution_receipt.model_dump(mode="json")
        )
        aggregate = type(coordination_result.reconciliation_aggregate).model_validate(
            coordination_result.reconciliation_aggregate.model_dump(mode="json")
        )
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_RECONCILIATION_RECEIPT_INVALID"
        ) from exc
    decision = aggregate.compiled_decision
    if (
        receipt.status
        not in {
            ParentProvisionReconciliationAggregateStatus.COMPLETE,
            ParentProvisionReconciliationAggregateStatus.PARTIAL_SEMANTIC,
        }
        or receipt.initial_object_execution_receipt_sha256
        != object_result.execution_receipt.receipt_sha256
        or receipt.initial_object_execution_trace_sha256
        != object_result.execution_receipt.aggregate_execution_trace_sha256
        or receipt.initial_object_batch_receipts_sha256
        != object_result.execution_receipt.batch_receipts_sha256
        or receipt.reconciliation_plan_sha256
        != coordination_result.reconciliation_plan.reconciliation_plan_sha256
        or receipt.reconciliation_aggregate_sha256 != aggregate.aggregate_sha256
        or receipt.global_candidate_sha256 != aggregate.global_candidate_sha256
        or receipt.parent_provision_decision_sha256 != decision.decision_sha256
        or receipt.evidence_refs_sha256 != _evidence_refs_sha256(decision)
        or receipt.reconciliation_execution_receipts_sha256 != receipt.receipt_sha256
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_RECONCILIATION_FINAL_HASH_DRIFT"
        )
    return receipt, aggregate


def _requires_only_reconciliation(
    result: ParentProvisionObjectStageRunResult,
) -> bool:
    statuses = tuple(item.status for item in result.batch_receipts)
    return (
        len(statuses) == len(result.plan.targets)
        and ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
        in statuses
        and all(
            item
            in {
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
            }
            for item in statuses
        )
    )


def _object_incomplete_reason_codes(
    result: ParentProvisionObjectStageRunResult,
) -> tuple[str, ...]:
    accepted_statuses = {
        ParentProvisionRecognitionBatchStatus.ACCEPTED,
        ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
        ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
    }
    reasons = {
        reason
        for batch in result.batch_receipts
        if batch.status not in accepted_statuses
        for reason in batch.reason_codes
        if _MACHINE_CODE_PATTERN.fullmatch(reason)
    }
    if len(result.batch_receipts) != len(result.plan.targets):
        reasons.add("OBJECT_BATCH_EXECUTION_INCOMPLETE")
    if not reasons:
        reasons.add(f"OBJECT_BATCH_{result.execution_receipt.status.value}")
    return tuple(sorted(reasons))


def _coordination_incomplete_reason_codes(
    result: ParentProvisionObjectReconciliationStageRunResult,
) -> tuple[str, ...]:
    reasons = {
        reason
        for reason in result.execution_receipt.reason_codes
        if _MACHINE_CODE_PATTERN.fullmatch(reason)
    }
    if not reasons:
        reasons.add(f"OBJECT_RECONCILIATION_{result.execution_receipt.status.value}")
    return tuple(sorted(reasons))


def _incomplete_result(
    *,
    object_result: ParentProvisionObjectStageRunResult,
    coordination_result: ParentProvisionObjectReconciliationStageRunResult | None,
    reason_codes: tuple[str, ...],
) -> CanonicalParentProvisionObjectWorkflowResult:
    return CanonicalParentProvisionObjectWorkflowResult(
        object_result=object_result,
        coordination_result=coordination_result,
        finalized_execution_receipt=None,
        professional_result=None,
        reason_codes=tuple(sorted(set(reason_codes))),
    )


def _final_status(
    status: ParentProvisionStatus,
) -> ParentProvisionRecognitionAggregateStatus:
    return (
        ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC
        if status is ParentProvisionStatus.PARTIAL
        else ParentProvisionRecognitionAggregateStatus.COMPLETE
    )


def _unresolved_entry_count(decision: object) -> int:
    return sum(
        entry.requirementness is Requirementness.UNRESOLVED
        for entry in decision.entries
    )


def _predecessor_partial(decision: object) -> bool:
    return "PREDECESSOR_D2_PARTIAL" in decision.diagnostic_codes


def _evidence_refs_sha256(decision: object) -> str:
    return canonical_json_sha256(
        parent_provision_evidence_refs(
            declared_logical_object_ids=decision.declared_logical_object_ids,
            unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
            entries=decision.entries,
        )
    )


def _machine_code(value: str) -> str:
    return (
        value
        if _MACHINE_CODE_PATTERN.fullmatch(value)
        else "PARENT_PROVISION_WORKFLOW_FAILED"
    )


def _professional_failure_reason(
    error: ParentProvisionProfessionalAggregateError,
) -> str:
    value = str(error)
    return (
        value
        if _MACHINE_CODE_PATTERN.fullmatch(value)
        else "PARENT_PROVISION_PROFESSIONAL_FINALIZATION_FAILED"
    )


__all__ = [
    "CanonicalParentProvisionObjectWorkflowOrchestrator",
    "CanonicalParentProvisionObjectWorkflowResult",
]
