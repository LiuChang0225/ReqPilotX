"""Real App Server driver for cross-object parent-provision reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageInvocation,
    CanonicalStageSessionPort,
    CanonicalStageTransportResult,
    CanonicalStructuredStageTransportPort,
    build_canonical_stage_invocation,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from reqpilotx_server.adapters.agents.codex_app_server.parent_provision_object_driver import (
    PARENT_PROVISION_OBJECT_EXECUTION_MODE,
    PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
    ParentProvisionObjectStageRunResult,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
    parent_provision_evidence_refs,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage
from reqpilotx_server.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateExecutionReceiptV2,
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionBatchStatus,
    ParentProvisionRecognitionCleanupStatus,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectExecutionMode,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_reconciliation import (
    PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION,
    ParentProvisionObjectReconciliationAggregateV1,
    ParentProvisionObjectReconciliationCandidateV1,
    ParentProvisionObjectReconciliationCompilationV1,
    ParentProvisionObjectReconciliationGroupV1,
    ParentProvisionObjectReconciliationPlanV1,
    aggregate_parent_provision_objects_with_reconciliation,
    build_parent_provision_object_reconciliation_plan,
    compile_parent_provision_object_reconciliation_candidate,
)
from reqpilotx_server.application.agent_workflow.parent_provision_reconciliation_receipts import (
    ParentProvisionReconciliationAggregateReceiptV1,
    ParentProvisionReconciliationAggregateStatus,
    ParentProvisionReconciliationAttemptReceiptV1,
    ParentProvisionReconciliationGroupReceiptV1,
    ParentProvisionReconciliationGroupStatus,
    build_parent_provision_reconciliation_aggregate_receipt,
    build_parent_provision_reconciliation_attempt_receipt,
    build_parent_provision_reconciliation_group_receipt,
    canonical_json_sha256,
    parent_provision_reconciliation_failure_feedback,
    parent_provision_reconciliation_failure_feedback_sha256,
)


PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE = "OBJECT_RECONCILIATION"
PARENT_PROVISION_OBJECT_RECONCILIATION_PROMPT_BINDING_VERSION = (
    "REQPILOTX_PARENT_PROVISION_OBJECT_RECONCILIATION_BINDING_V1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")


@dataclass(frozen=True, slots=True)
class ParentProvisionObjectReconciliationStageRunResult:
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1
    group_compilations_in_completion_order: tuple[
        ParentProvisionObjectReconciliationCompilationV1, ...
    ]
    group_receipts_in_completion_order: tuple[
        ParentProvisionReconciliationGroupReceiptV1, ...
    ]
    reconciliation_aggregate: ParentProvisionObjectReconciliationAggregateV1 | None
    execution_receipt: ParentProvisionReconciliationAggregateReceiptV1


class ParentProvisionObjectReconciliationStageDriver:
    """Run each Host-pinned reconciliation group through the real transport."""

    def __init__(
        self,
        *,
        transport: CanonicalStructuredStageTransportPort,
        object_compiler: CanonicalParentProvisionObjectBatchCompiler,
        max_attempts_per_group: int = 2,
        group_checkpoint_sink: Callable[
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
        if (
            isinstance(max_attempts_per_group, bool)
            or not isinstance(max_attempts_per_group, int)
            or not 1 <= max_attempts_per_group <= 64
        ):
            raise ValueError("reconciliation retry budget must be between 1 and 64")
        self._transport = transport
        self._object_compiler = object_compiler
        self._max_attempts_per_group = max_attempts_per_group
        self._group_checkpoint_sink = group_checkpoint_sink

    def run(
        self,
        session: CanonicalStageSessionPort,
        *,
        initial_result: ParentProvisionObjectStageRunResult,
        task_binding_sha256: str,
        reconciliation_plan: ParentProvisionObjectReconciliationPlanV1 | None = None,
        group_execution_order: tuple[str, ...] | None = None,
        resumed_group_receipts: tuple[
            ParentProvisionReconciliationGroupReceiptV1, ...
        ] = (),
        resumed_group_compilations: tuple[
            ParentProvisionObjectReconciliationCompilationV1, ...
        ] = (),
    ) -> ParentProvisionObjectReconciliationStageRunResult:
        if _SHA256_PATTERN.fullmatch(task_binding_sha256) is None:
            raise LifecycleProbeError("OBJECT_RECONCILIATION_TASK_BINDING_INVALID")
        activation = session.require_inference_stage(
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            execution_mode=PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
        )
        if (
            activation.runner_output_execution_mode
            != PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
            or activation.runner_output_schema_id
            != PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION
        ):
            raise LifecycleProbeError(
                "OBJECT_RECONCILIATION_SCHEMA_ACTIVATION_MISMATCH"
            )
        output_schema = _load_selected_output_schema(activation)
        initial_receipt = _require_initial_object_result(
            initial_result=initial_result,
            compiler=self._object_compiler,
            activation=activation,
        )
        canonical_plan = build_parent_provision_object_reconciliation_plan(
            object_batch_plan=initial_result.plan,
            expected_object_batch_plan_sha256=initial_result.plan.plan_sha256,
            local_results=initial_result.compilations,
            d2_identity=self._object_compiler.d2_identity,
            source_segments=self._object_compiler.source_segments,
            kit_identity=self._object_compiler.kit_identity,
        )
        selected_plan = reconciliation_plan or canonical_plan
        try:
            selected_plan = ParentProvisionObjectReconciliationPlanV1.model_validate(
                selected_plan.model_dump(mode="json")
            )
        except Exception as exc:
            raise LifecycleProbeError("OBJECT_RECONCILIATION_PLAN_INVALID") from exc
        if selected_plan != canonical_plan:
            raise LifecycleProbeError("OBJECT_RECONCILIATION_PLAN_SCOPE_DRIFT")

        groups = _ordered_groups(selected_plan, group_execution_order)
        resumed_receipt_by_group, resumed_compilation_by_group = (
            _validated_resumed_group_results(
                initial_result=initial_result,
                plan=selected_plan,
                groups=groups,
                compiler=self._object_compiler,
                receipts=resumed_group_receipts,
                compilations=resumed_group_compilations,
            )
        )
        completions: list[ParentProvisionObjectReconciliationCompilationV1] = []
        receipts: list[ParentProvisionReconciliationGroupReceiptV1] = []
        for group in groups:
            resumed_receipt = resumed_receipt_by_group.get(group.group_id)
            if resumed_receipt is not None:
                receipts.append(resumed_receipt)
                completions.append(resumed_compilation_by_group[group.group_id])
                continue
            receipt, compilation = self._run_group(
                activation=activation,
                output_schema=output_schema,
                initial_result=initial_result,
                initial_receipt=initial_receipt,
                reconciliation_plan=selected_plan,
                group=group,
                base_task_binding_sha256=task_binding_sha256,
            )
            receipts.append(receipt)
            if compilation is not None:
                completions.append(compilation)
                if self._group_checkpoint_sink is not None and receipt.status in {
                    ParentProvisionReconciliationGroupStatus.ACCEPTED,
                    ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
                }:
                    self._group_checkpoint_sink(
                        selected_plan,
                        group,
                        receipt,
                        compilation,
                    )
            if receipt.status in {
                ParentProvisionReconciliationGroupStatus.CANCELLED,
                ParentProvisionReconciliationGroupStatus.BLOCKED,
                ParentProvisionReconciliationGroupStatus.INDETERMINATE,
            }:
                break

        reconciliation_aggregate: (
            ParentProvisionObjectReconciliationAggregateV1 | None
        ) = None
        if len(receipts) == len(selected_plan.groups) and all(
            item.status
            in {
                ParentProvisionReconciliationGroupStatus.ACCEPTED,
                ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            }
            for item in receipts
        ):
            reconciliation_aggregate = (
                aggregate_parent_provision_objects_with_reconciliation(
                    object_batch_plan=initial_result.plan,
                    expected_object_batch_plan_sha256=initial_result.plan.plan_sha256,
                    reconciliation_plan=selected_plan,
                    expected_reconciliation_plan_sha256=(
                        selected_plan.reconciliation_plan_sha256
                    ),
                    local_results=initial_result.compilations,
                    reconciliation_results=tuple(completions),
                    d2_identity=self._object_compiler.d2_identity,
                    source_segments=self._object_compiler.source_segments,
                    kit_identity=self._object_compiler.kit_identity,
                )
            )
        execution_receipt = _build_aggregate_receipt(
            activation=activation,
            initial_receipt=initial_receipt,
            plan=selected_plan,
            group_receipts=tuple(receipts),
            reconciliation_aggregate=reconciliation_aggregate,
        )
        return ParentProvisionObjectReconciliationStageRunResult(
            reconciliation_plan=selected_plan,
            group_compilations_in_completion_order=tuple(completions),
            group_receipts_in_completion_order=tuple(receipts),
            reconciliation_aggregate=reconciliation_aggregate,
            execution_receipt=execution_receipt,
        )

    def _run_group(
        self,
        *,
        activation: Any,
        output_schema: Mapping[str, Any],
        initial_result: ParentProvisionObjectStageRunResult,
        initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
        reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
        group: ParentProvisionObjectReconciliationGroupV1,
        base_task_binding_sha256: str,
    ) -> tuple[
        ParentProvisionReconciliationGroupReceiptV1,
        ParentProvisionObjectReconciliationCompilationV1 | None,
    ]:
        attempts: list[ParentProvisionReconciliationAttemptReceiptV1] = []
        for attempt_ordinal in range(1, self._max_attempts_per_group + 1):
            previous = attempts[-1] if attempts else None
            task_binding_sha256 = _attempt_task_binding_sha256(
                base_task_binding_sha256=base_task_binding_sha256,
                initial_receipt=initial_receipt,
                reconciliation_plan=reconciliation_plan,
                group=group,
                attempt_ordinal=attempt_ordinal,
                previous=previous,
            )
            try:
                invocation = _build_reconciliation_invocation(
                    activation=activation,
                    output_schema=output_schema,
                    initial_receipt=initial_receipt,
                    reconciliation_plan=reconciliation_plan,
                    group=group,
                    task_binding_sha256=task_binding_sha256,
                    attempt_ordinal=attempt_ordinal,
                    previous=previous,
                )
                transport_result = _invoke_transport(self._transport, invocation)
            except LifecycleProbeError as exc:
                failure = _transport_failure_attempt_receipt(
                    activation=activation,
                    initial_receipt=initial_receipt,
                    reconciliation_plan=reconciliation_plan,
                    group=group,
                    attempt_ordinal=attempt_ordinal,
                    task_binding_sha256=task_binding_sha256,
                    previous=previous,
                    reason_code=exc.reason_code,
                )
                attempts.append(failure)
                if (
                    failure.outcome
                    is ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED
                    and attempt_ordinal < self._max_attempts_per_group
                ):
                    continue
                return _terminal_group_receipt(tuple(attempts)), None
            except Exception:
                failure = _transport_failure_attempt_receipt(
                    activation=activation,
                    initial_receipt=initial_receipt,
                    reconciliation_plan=reconciliation_plan,
                    group=group,
                    attempt_ordinal=attempt_ordinal,
                    task_binding_sha256=task_binding_sha256,
                    previous=previous,
                    reason_code="CANONICAL_STAGE_TRANSPORT_FAILED",
                )
                attempts.append(failure)
                if attempt_ordinal < self._max_attempts_per_group:
                    continue
                return _terminal_group_receipt(tuple(attempts)), None

            candidate: ParentProvisionObjectReconciliationCandidateV1 | None = None
            compilation: ParentProvisionObjectReconciliationCompilationV1 | None = None
            rejection_reason: str | None = None
            try:
                candidate = (
                    ParentProvisionObjectReconciliationCandidateV1.model_validate(
                        transport_result.structured_output
                    )
                )
            except ValidationError:
                rejection_reason = "OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_REJECTED"
            except Exception:
                rejection_reason = "OBJECT_RECONCILIATION_CANDIDATE_INVALID"
            if candidate is not None:
                try:
                    compilation = (
                        compile_parent_provision_object_reconciliation_candidate(
                            object_batch_plan=initial_result.plan,
                            expected_object_batch_plan_sha256=(
                                initial_result.plan.plan_sha256
                            ),
                            reconciliation_plan=reconciliation_plan,
                            expected_reconciliation_plan_sha256=(
                                reconciliation_plan.reconciliation_plan_sha256
                            ),
                            group_id=group.group_id,
                            local_results=initial_result.compilations,
                            d2_identity=self._object_compiler.d2_identity,
                            source_segments=self._object_compiler.source_segments,
                            kit_identity=self._object_compiler.kit_identity,
                            candidate=candidate,
                        )
                    )
                except (ValidationError, ValueError) as exc:
                    rejection_reason = _machine_reason_code(
                        exc, default="OBJECT_RECONCILIATION_HOST_REJECTED"
                    )
                except Exception:
                    rejection_reason = "OBJECT_RECONCILIATION_HOST_COMPILER_FAILED"

            if compilation is None:
                rejected = _completed_transport_attempt_receipt(
                    activation=activation,
                    initial_receipt=initial_receipt,
                    reconciliation_plan=reconciliation_plan,
                    group=group,
                    attempt_ordinal=attempt_ordinal,
                    task_binding_sha256=task_binding_sha256,
                    previous=previous,
                    transport_result=transport_result,
                    outcome=ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED,
                    candidate_sha256=(
                        candidate.candidate_sha256 if candidate is not None else None
                    ),
                    compilation=None,
                    reason_codes=(
                        rejection_reason or "OBJECT_RECONCILIATION_OUTPUT_REJECTED",
                    ),
                )
                attempts.append(rejected)
                if attempt_ordinal < self._max_attempts_per_group:
                    continue
                return _terminal_group_receipt(tuple(attempts)), None

            accepted = _completed_transport_attempt_receipt(
                activation=activation,
                initial_receipt=initial_receipt,
                reconciliation_plan=reconciliation_plan,
                group=group,
                attempt_ordinal=attempt_ordinal,
                task_binding_sha256=task_binding_sha256,
                previous=previous,
                transport_result=transport_result,
                outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
                candidate_sha256=compilation.model_candidate_sha256,
                compilation=compilation,
                reason_codes=(),
            )
            attempts.append(accepted)
            partial = (
                accepted.semantic_unresolved_scope_count
                or accepted.semantic_unresolved_entry_count
                or accepted.predecessor_partial
            )
            return (
                build_parent_provision_reconciliation_group_receipt(
                    **_group_common_values(accepted),
                    attempts=tuple(attempts),
                    accepted_attempt_id=accepted.attempt_id,
                    accepted_attempt_receipt_sha256=accepted.receipt_sha256,
                    status=(
                        ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
                        if partial
                        else ParentProvisionReconciliationGroupStatus.ACCEPTED
                    ),
                    semantic_unresolved_scope_count=(
                        accepted.semantic_unresolved_scope_count
                    ),
                    semantic_unresolved_entry_count=(
                        accepted.semantic_unresolved_entry_count
                    ),
                    predecessor_partial=accepted.predecessor_partial,
                    reconciliation_compilation_result_sha256=(
                        compilation.result_sha256
                    ),
                    compiled_decision_sha256=(
                        compilation.compiled_decision.decision_sha256
                    ),
                    group_disposition_sha256=compilation.group_disposition_sha256,
                    reason_codes=(),
                ),
                compilation,
            )
        raise AssertionError("finite reconciliation retry loop did not terminate")


def _build_reconciliation_invocation(
    *,
    activation: Any,
    output_schema: Mapping[str, Any],
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
    group: ParentProvisionObjectReconciliationGroupV1,
    task_binding_sha256: str,
    attempt_ordinal: int,
    previous: ParentProvisionReconciliationAttemptReceiptV1 | None,
) -> CanonicalStageInvocation:
    invocation = build_canonical_stage_invocation(
        activation,
        task_binding_sha256=task_binding_sha256,
        turn_output_schema_id=(
            PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION
        ),
        turn_output_schema=output_schema,
        expected_final_artifact_schema_id=PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
        expected_validator_id=PARENT_PROVISION_VALIDATOR_ID,
        expected_validator_version=PARENT_PROVISION_VALIDATOR_VERSION,
    )
    feedback = (
        parent_provision_reconciliation_failure_feedback(previous)
        if previous is not None
        else None
    )
    feedback_text = (
        json.dumps(
            feedback,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if feedback is not None
        else "NONE"
    )
    binding = (
        "\n\n"
        f"{PARENT_PROVISION_OBJECT_RECONCILIATION_PROMPT_BINDING_VERSION}\n"
        f"execution_mode={PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE}\n"
        f"object_batch_plan_id={reconciliation_plan.object_batch_plan_id}\n"
        f"object_batch_plan_sha256={reconciliation_plan.object_batch_plan_sha256}\n"
        f"initial_object_execution_receipt_sha256={initial_receipt.receipt_sha256}\n"
        f"initial_object_execution_trace_sha256="
        f"{initial_receipt.aggregate_execution_trace_sha256}\n"
        f"reconciliation_plan_id={reconciliation_plan.reconciliation_plan_id}\n"
        f"reconciliation_plan_sha256="
        f"{reconciliation_plan.reconciliation_plan_sha256}\n"
        f"reconciliation_group_count={len(reconciliation_plan.groups)}\n"
        f"reconciliation_group_id={group.group_id}\n"
        f"reconciliation_group_order={group.group_order}\n"
        f"reconciliation_group_scope_sha256={group.group_scope_sha256}\n"
        f"replacement_batch_ids="
        f"{','.join(item.batch_id for item in group.replacements)}\n"
        f"replacement_logical_object_ids="
        f"{','.join(item.logical_object_id for item in group.replacements)}\n"
        f"replaced_local_result_sha256s="
        f"{','.join(item.local_result_sha256 for item in group.replacements)}\n"
        f"allowed_global_segment_ordinals="
        f"{','.join(str(item) for item in group.global_segment_ordinals)}\n"
        f"attempt_ordinal={attempt_ordinal}\n"
        f"previous_failure_feedback={feedback_text}\n"
        "Every listed replacement and allowed ordinal is immutable. Replace the "
        "whole pinned group together. You may join a parent across replacement "
        "object edges, but every selected source span must remain inside the "
        "allowed global ordinals. Previous failure feedback is correction data, "
        "not document evidence. Execution failure must never be returned as "
        "semantic UNRESOLVED."
    )
    items = tuple(dict(item) for item in invocation.input_items)
    prompt_index = next(
        index for index, item in enumerate(items) if item.get("type") == "text"
    )
    bound_items = list(items)
    bound_items[prompt_index] = {
        "type": "text",
        "text": str(items[prompt_index]["text"]) + binding,
    }
    return replace(invocation, input_items=tuple(bound_items))


def _attempt_task_binding_sha256(
    *,
    base_task_binding_sha256: str,
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
    group: ParentProvisionObjectReconciliationGroupV1,
    attempt_ordinal: int,
    previous: ParentProvisionReconciliationAttemptReceiptV1 | None,
) -> str:
    return canonical_json_sha256(
        {
            "binding_version": (
                PARENT_PROVISION_OBJECT_RECONCILIATION_PROMPT_BINDING_VERSION
            ),
            "base_task_binding_sha256": base_task_binding_sha256,
            "execution_mode": PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
            "object_batch_plan_id": reconciliation_plan.object_batch_plan_id,
            "object_batch_plan_sha256": reconciliation_plan.object_batch_plan_sha256,
            "initial_object_execution_receipt_sha256": initial_receipt.receipt_sha256,
            "initial_object_execution_trace_sha256": (
                initial_receipt.aggregate_execution_trace_sha256
            ),
            "reconciliation_plan_id": reconciliation_plan.reconciliation_plan_id,
            "reconciliation_plan_sha256": (
                reconciliation_plan.reconciliation_plan_sha256
            ),
            "group_id": group.group_id,
            "group_order": group.group_order,
            "group_scope_sha256": group.group_scope_sha256,
            "replacement_batch_ids": tuple(
                item.batch_id for item in group.replacements
            ),
            "replacement_logical_object_ids": tuple(
                item.logical_object_id for item in group.replacements
            ),
            "replaced_local_result_sha256s": tuple(
                item.local_result_sha256 for item in group.replacements
            ),
            "allowed_global_segment_ordinals": group.global_segment_ordinals,
            "attempt_ordinal": attempt_ordinal,
            "previous_failure_feedback": (
                parent_provision_reconciliation_failure_feedback(previous)
                if previous is not None
                else None
            ),
        }
    )


def _completed_transport_attempt_receipt(
    *,
    activation: Any,
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
    group: ParentProvisionObjectReconciliationGroupV1,
    attempt_ordinal: int,
    task_binding_sha256: str,
    previous: ParentProvisionReconciliationAttemptReceiptV1 | None,
    transport_result: CanonicalStageTransportResult,
    outcome: ParentProvisionRecognitionAttemptOutcome,
    candidate_sha256: str | None,
    compilation: ParentProvisionObjectReconciliationCompilationV1 | None,
    reason_codes: tuple[str, ...],
) -> ParentProvisionReconciliationAttemptReceiptV1:
    decision = compilation.compiled_decision if compilation is not None else None
    return build_parent_provision_reconciliation_attempt_receipt(
        **_attempt_common_values(
            activation=activation,
            initial_receipt=initial_receipt,
            reconciliation_plan=reconciliation_plan,
            group=group,
            attempt_ordinal=attempt_ordinal,
            task_binding_sha256=task_binding_sha256,
            previous=previous,
        ),
        outcome=outcome,
        transport_result_sha256=_transport_result_sha256(transport_result),
        response_sha256=transport_result.response_sha256,
        tool_receipt_sha256=transport_result.tool_receipt_sha256,
        runtime_thread_id_sha256=_text_sha256(transport_result.runtime_thread_id),
        runtime_turn_id_sha256=_text_sha256(transport_result.runtime_turn_id),
        candidate_sha256=candidate_sha256,
        reconciliation_compilation_result_sha256=(
            compilation.result_sha256 if compilation is not None else None
        ),
        compiled_decision_sha256=(decision.decision_sha256 if decision else None),
        group_disposition_sha256=(
            compilation.group_disposition_sha256 if compilation is not None else None
        ),
        semantic_unresolved_scope_count=(
            len(decision.unresolved_logical_object_ids) if decision else 0
        ),
        semantic_unresolved_entry_count=(
            _unresolved_entry_count(decision.entries) if decision else 0
        ),
        predecessor_partial=(
            "PREDECESSOR_D2_PARTIAL" in decision.diagnostic_codes if decision else False
        ),
        cleanup_status=ParentProvisionRecognitionCleanupStatus.SUCCEEDED,
        workspace_unchanged=True,
        reason_codes=reason_codes,
    )


def _transport_failure_attempt_receipt(
    *,
    activation: Any,
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
    group: ParentProvisionObjectReconciliationGroupV1,
    attempt_ordinal: int,
    task_binding_sha256: str,
    previous: ParentProvisionReconciliationAttemptReceiptV1 | None,
    reason_code: str,
) -> ParentProvisionReconciliationAttemptReceiptV1:
    reason = (
        reason_code
        if _MACHINE_CODE_PATTERN.fullmatch(reason_code)
        else "CANONICAL_STAGE_TRANSPORT_FAILED"
    )
    outcome, cleanup, workspace_unchanged = _classify_transport_failure(reason)
    return build_parent_provision_reconciliation_attempt_receipt(
        **_attempt_common_values(
            activation=activation,
            initial_receipt=initial_receipt,
            reconciliation_plan=reconciliation_plan,
            group=group,
            attempt_ordinal=attempt_ordinal,
            task_binding_sha256=task_binding_sha256,
            previous=previous,
        ),
        outcome=outcome,
        transport_result_sha256=None,
        response_sha256=None,
        tool_receipt_sha256=None,
        runtime_thread_id_sha256=None,
        runtime_turn_id_sha256=None,
        candidate_sha256=None,
        reconciliation_compilation_result_sha256=None,
        compiled_decision_sha256=None,
        group_disposition_sha256=None,
        semantic_unresolved_scope_count=0,
        semantic_unresolved_entry_count=0,
        predecessor_partial=False,
        cleanup_status=cleanup,
        workspace_unchanged=workspace_unchanged,
        reason_codes=(reason,),
    )


def _attempt_common_values(
    *,
    activation: Any,
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
    group: ParentProvisionObjectReconciliationGroupV1,
    attempt_ordinal: int,
    task_binding_sha256: str,
    previous: ParentProvisionReconciliationAttemptReceiptV1 | None,
) -> dict[str, Any]:
    stage_binding = _stage_binding(activation)
    replacement_batch_ids = tuple(item.batch_id for item in group.replacements)
    replacement_object_ids = tuple(
        item.logical_object_id for item in group.replacements
    )
    return {
        "run_id": initial_receipt.run_id,
        "source_sha256": initial_receipt.source_sha256,
        "predecessor_artifact_id": initial_receipt.predecessor_artifact_id,
        "predecessor_artifact_sha256": initial_receipt.predecessor_artifact_sha256,
        "kit_bundle_sha256": initial_receipt.kit_bundle_sha256,
        "object_batch_plan_id": reconciliation_plan.object_batch_plan_id,
        "object_batch_plan_sha256": reconciliation_plan.object_batch_plan_sha256,
        "initial_object_execution_receipt_sha256": initial_receipt.receipt_sha256,
        "initial_object_execution_trace_sha256": (
            initial_receipt.aggregate_execution_trace_sha256
        ),
        "initial_object_batch_receipts_sha256": initial_receipt.batch_receipts_sha256,
        "reconciliation_plan_id": reconciliation_plan.reconciliation_plan_id,
        "reconciliation_plan_sha256": reconciliation_plan.reconciliation_plan_sha256,
        "group_id": group.group_id,
        "group_order": group.group_order,
        "group_scope_sha256": group.group_scope_sha256,
        "replacement_batch_ids": replacement_batch_ids,
        "replacement_logical_object_ids": replacement_object_ids,
        "allowed_global_segment_ordinals": group.global_segment_ordinals,
        "replacement_inventory_sha256": canonical_json_sha256(
            tuple(zip(replacement_batch_ids, replacement_object_ids, strict=True))
        ),
        "allowed_global_ordinals_sha256": canonical_json_sha256(
            group.global_segment_ordinals
        ),
        "attempt_ordinal": attempt_ordinal,
        "task_binding_sha256": task_binding_sha256,
        "previous_attempt_id": previous.attempt_id if previous else None,
        "previous_attempt_receipt_sha256": (
            previous.receipt_sha256 if previous else None
        ),
        "previous_failure_feedback_sha256": (
            parent_provision_reconciliation_failure_feedback_sha256(previous)
            if previous
            else None
        ),
        "stage_session_attestation_id": (activation.session_attestation.attestation_id),
        "stage_session_attestation_sha256": canonical_json_sha256(
            activation.session_attestation.model_dump(mode="json")
        ),
        "stage_context_sha256": activation.context_sha256,
        "skill_sha256": stage_binding.skill.sha256,
        "runner_output_execution_mode": (
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
        ),
        "runner_output_schema_id": (
            PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION
        ),
        "runner_output_schema_sha256": activation.runner_output_schema_sha256,
        "runner_output_schema_variants_sha256": (
            activation.runner_output_schema_variants_sha256
        ),
        "policy_closure_sha256": initial_receipt.policy_closure_sha256,
        "runner_profile_sha256": initial_receipt.runner_profile_sha256,
        "model_id": initial_receipt.model_id,
        "reasoning_effort": initial_receipt.reasoning_effort,
    }


def _group_common_values(
    attempt: ParentProvisionReconciliationAttemptReceiptV1,
) -> dict[str, Any]:
    return {
        key: getattr(attempt, key)
        for key in (
            "run_id",
            "source_sha256",
            "predecessor_artifact_id",
            "predecessor_artifact_sha256",
            "kit_bundle_sha256",
            "object_batch_plan_id",
            "object_batch_plan_sha256",
            "initial_object_execution_receipt_sha256",
            "initial_object_execution_trace_sha256",
            "initial_object_batch_receipts_sha256",
            "reconciliation_plan_id",
            "reconciliation_plan_sha256",
            "group_id",
            "group_order",
            "group_scope_sha256",
            "replacement_batch_ids",
            "replacement_logical_object_ids",
            "allowed_global_segment_ordinals",
            "replacement_inventory_sha256",
            "allowed_global_ordinals_sha256",
        )
    }


def _terminal_group_receipt(
    attempts: tuple[ParentProvisionReconciliationAttemptReceiptV1, ...],
) -> ParentProvisionReconciliationGroupReceiptV1:
    terminal = attempts[-1]
    status = {
        ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED: (
            ParentProvisionReconciliationGroupStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED: (
            ParentProvisionReconciliationGroupStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.CANCELLED: (
            ParentProvisionReconciliationGroupStatus.CANCELLED
        ),
        ParentProvisionRecognitionAttemptOutcome.BLOCKED: (
            ParentProvisionReconciliationGroupStatus.BLOCKED
        ),
        ParentProvisionRecognitionAttemptOutcome.INDETERMINATE: (
            ParentProvisionReconciliationGroupStatus.INDETERMINATE
        ),
    }[terminal.outcome]
    reasons = set(terminal.reason_codes)
    if status is ParentProvisionReconciliationGroupStatus.FAILED:
        reasons.add("RETRY_BUDGET_EXHAUSTED")
    return build_parent_provision_reconciliation_group_receipt(
        **_group_common_values(terminal),
        attempts=attempts,
        accepted_attempt_id=None,
        accepted_attempt_receipt_sha256=None,
        status=status,
        semantic_unresolved_scope_count=0,
        semantic_unresolved_entry_count=0,
        predecessor_partial=False,
        reconciliation_compilation_result_sha256=None,
        compiled_decision_sha256=None,
        group_disposition_sha256=None,
        reason_codes=tuple(sorted(reasons)),
    )


def _build_aggregate_receipt(
    *,
    activation: Any,
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    plan: ParentProvisionObjectReconciliationPlanV1,
    group_receipts: tuple[ParentProvisionReconciliationGroupReceiptV1, ...],
    reconciliation_aggregate: ParentProvisionObjectReconciliationAggregateV1 | None,
) -> ParentProvisionReconciliationAggregateReceiptV1:
    stage_binding = _stage_binding(activation)
    if reconciliation_aggregate is not None:
        decision = reconciliation_aggregate.compiled_decision
        unresolved_scopes = len(decision.unresolved_logical_object_ids)
        unresolved_entries = _unresolved_entry_count(decision.entries)
        predecessor_partial = "PREDECESSOR_D2_PARTIAL" in decision.diagnostic_codes
        partial = unresolved_scopes or unresolved_entries or predecessor_partial
        status = (
            ParentProvisionReconciliationAggregateStatus.PARTIAL_SEMANTIC
            if partial
            else ParentProvisionReconciliationAggregateStatus.COMPLETE
        )
        evidence_refs = parent_provision_evidence_refs(
            declared_logical_object_ids=decision.declared_logical_object_ids,
            unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
            entries=decision.entries,
        )
        final_values = {
            "semantic_unresolved_scope_count": unresolved_scopes,
            "semantic_unresolved_entry_count": unresolved_entries,
            "predecessor_partial": predecessor_partial,
            "reconciliation_aggregate_sha256": (
                reconciliation_aggregate.aggregate_sha256
            ),
            "global_candidate_sha256": (
                reconciliation_aggregate.global_candidate_sha256
            ),
            "parent_provision_decision_sha256": decision.decision_sha256,
            "evidence_refs_sha256": canonical_json_sha256(evidence_refs),
            "reason_codes": (),
        }
    else:
        nonaccepted = tuple(
            item
            for item in group_receipts
            if item.status
            not in {
                ParentProvisionReconciliationGroupStatus.ACCEPTED,
                ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            }
        )
        statuses = {item.status for item in nonaccepted}
        if ParentProvisionReconciliationGroupStatus.INDETERMINATE in statuses:
            status = ParentProvisionReconciliationAggregateStatus.INDETERMINATE
        elif ParentProvisionReconciliationGroupStatus.BLOCKED in statuses:
            status = ParentProvisionReconciliationAggregateStatus.BLOCKED
        elif ParentProvisionReconciliationGroupStatus.CANCELLED in statuses:
            status = ParentProvisionReconciliationAggregateStatus.CANCELLED
        else:
            status = ParentProvisionReconciliationAggregateStatus.FAILED
        final_values = {
            "semantic_unresolved_scope_count": 0,
            "semantic_unresolved_entry_count": 0,
            "predecessor_partial": False,
            "reconciliation_aggregate_sha256": None,
            "global_candidate_sha256": None,
            "parent_provision_decision_sha256": None,
            "evidence_refs_sha256": None,
            "reason_codes": tuple(
                sorted({reason for item in nonaccepted for reason in item.reason_codes})
            ),
        }
    return build_parent_provision_reconciliation_aggregate_receipt(
        run_id=initial_receipt.run_id,
        source_sha256=initial_receipt.source_sha256,
        predecessor_artifact_id=initial_receipt.predecessor_artifact_id,
        predecessor_artifact_sha256=initial_receipt.predecessor_artifact_sha256,
        kit_bundle_sha256=initial_receipt.kit_bundle_sha256,
        policy_closure_sha256=initial_receipt.policy_closure_sha256,
        runner_profile_sha256=initial_receipt.runner_profile_sha256,
        model_id=initial_receipt.model_id,
        reasoning_effort=initial_receipt.reasoning_effort,
        stage_session_attestation_id=activation.session_attestation.attestation_id,
        stage_session_attestation_sha256=canonical_json_sha256(
            activation.session_attestation.model_dump(mode="json")
        ),
        stage_context_sha256=activation.context_sha256,
        skill_sha256=stage_binding.skill.sha256,
        runner_output_execution_mode=(
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
        ),
        runner_output_schema_id=(
            PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION
        ),
        runner_output_schema_sha256=activation.runner_output_schema_sha256,
        runner_output_schema_variants_sha256=(
            activation.runner_output_schema_variants_sha256
        ),
        object_batch_plan_id=plan.object_batch_plan_id,
        object_batch_plan_sha256=plan.object_batch_plan_sha256,
        initial_object_execution_receipt_sha256=initial_receipt.receipt_sha256,
        initial_object_execution_trace_sha256=(
            initial_receipt.aggregate_execution_trace_sha256
        ),
        initial_object_batch_receipts_sha256=initial_receipt.batch_receipts_sha256,
        reconciliation_plan_id=plan.reconciliation_plan_id,
        reconciliation_plan_sha256=plan.reconciliation_plan_sha256,
        planned_group_ids=tuple(item.group_id for item in plan.groups),
        groups_in_completion_order=group_receipts,
        status=status,
        **final_values,
    )


def _require_initial_object_result(
    *,
    initial_result: ParentProvisionObjectStageRunResult,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    activation: Any,
) -> ParentProvisionRecognitionAggregateExecutionReceiptV2:
    try:
        receipt = ParentProvisionRecognitionAggregateExecutionReceiptV2.model_validate(
            initial_result.execution_receipt.model_dump(mode="json")
        )
    except Exception as exc:
        raise LifecycleProbeError(
            "OBJECT_RECONCILIATION_INITIAL_RECEIPT_INVALID"
        ) from exc
    plan = initial_result.plan
    if (
        receipt.status is not ParentProvisionRecognitionAggregateStatus.BLOCKED
        or receipt.run_id != compiler.d2_identity.run_id
        or receipt.source_sha256 != compiler.d2_identity.source_sha256
        or receipt.predecessor_artifact_id != compiler.d2_identity.artifact_id
        or receipt.predecessor_artifact_sha256 != compiler.d2_identity.artifact_sha256
        or receipt.kit_bundle_sha256 != compiler.kit_identity.bundle_sha256
        or receipt.plan_id != plan.plan_id
        or receipt.plan_sha256 != plan.plan_sha256
        or len(receipt.batches) != len(plan.targets)
        or receipt.planned_batch_count != len(plan.targets)
        or receipt.object_batch_aggregate_sha256 is not None
        or receipt.decision_candidate_sha256 is not None
        or receipt.parent_provision_decision_sha256 is not None
        or receipt.evidence_refs_sha256 is not None
    ):
        raise LifecycleProbeError("OBJECT_RECONCILIATION_INITIAL_LINEAGE_INVALID")
    accepted_statuses = {
        ParentProvisionRecognitionBatchStatus.ACCEPTED,
        ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
        ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
        ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
    }
    if any(item.status not in accepted_statuses for item in receipt.batches) or not any(
        item.status
        is ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
        for item in receipt.batches
    ):
        raise LifecycleProbeError(
            "OBJECT_RECONCILIATION_INITIAL_EXECUTION_FAILURE_PRESENT"
        )
    session = activation.session_attestation
    if (
        receipt.policy_closure_sha256 != session.d3_policy_closure_sha256
        or receipt.runner_profile_sha256 != session.runner_profile_sha256
        or receipt.model_id != session.model_id
        or receipt.reasoning_effort != session.reasoning_effort
        or session.kit_identity != compiler.kit_identity
    ):
        raise LifecycleProbeError("OBJECT_RECONCILIATION_SESSION_LINEAGE_INVALID")
    model_targets = tuple(
        target
        for target in plan.targets
        if target.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    if len(initial_result.compilations) != len(model_targets):
        raise LifecycleProbeError("OBJECT_RECONCILIATION_LOCAL_RESULTS_INCOMPLETE")
    compilation_by_batch = {item.batch_id: item for item in initial_result.compilations}
    if set(compilation_by_batch) != {item.batch_id for item in model_targets}:
        raise LifecycleProbeError("OBJECT_RECONCILIATION_LOCAL_RESULT_SET_DRIFT")
    stage = _stage_binding(activation)
    allowed_initial_schema_sha256s = {
        item.schema_file.sha256
        for item in stage.runner_output_schema_variants
        if item.execution_mode
        in {
            PARENT_PROVISION_OBJECT_EXECUTION_MODE,
            PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
        }
    }
    if stage.runner_output_schema is not None:
        allowed_initial_schema_sha256s.add(stage.runner_output_schema.sha256)
    for target, batch in zip(plan.targets, receipt.batches, strict=True):
        if (
            batch.batch_id != target.batch_id
            or batch.batch_order != target.batch_order
            or batch.target_scope_sha256 != target.target_scope_sha256
        ):
            raise LifecycleProbeError("OBJECT_RECONCILIATION_INITIAL_BATCH_DRIFT")
        if target.execution_mode is not ParentProvisionObjectExecutionMode.MODEL_TARGET:
            continue
        compilation = compilation_by_batch[target.batch_id]
        accepted = next(
            (
                item
                for item in batch.attempts
                if item.attempt_id == batch.accepted_attempt_id
            ),
            None,
        )
        if (
            accepted is None
            or accepted.batch_compilation_result_sha256 != compilation.result_sha256
            or accepted.stage_context_sha256 != stage.context_sha256
            or accepted.skill_sha256 != stage.skill.sha256
            or accepted.runner_output_schema_sha256
            not in allowed_initial_schema_sha256s
            or accepted.model_id != session.model_id
            or accepted.reasoning_effort != session.reasoning_effort
        ):
            raise LifecycleProbeError(
                "OBJECT_RECONCILIATION_INITIAL_ACCEPTED_ATTEMPT_DRIFT"
            )
    return receipt


def _validated_resumed_group_results(
    *,
    initial_result: ParentProvisionObjectStageRunResult,
    plan: ParentProvisionObjectReconciliationPlanV1,
    groups: tuple[ParentProvisionObjectReconciliationGroupV1, ...],
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    receipts: tuple[ParentProvisionReconciliationGroupReceiptV1, ...],
    compilations: tuple[ParentProvisionObjectReconciliationCompilationV1, ...],
) -> tuple[
    dict[str, ParentProvisionReconciliationGroupReceiptV1],
    dict[str, ParentProvisionObjectReconciliationCompilationV1],
]:
    if not receipts and not compilations:
        return {}, {}
    receipt_by_group = {item.group_id: item for item in receipts}
    compilation_by_group = {item.group_id: item for item in compilations}
    if (
        len(receipt_by_group) != len(receipts)
        or len(compilation_by_group) != len(compilations)
        or set(receipt_by_group) != set(compilation_by_group)
    ):
        raise LifecycleProbeError("OBJECT_RECONCILIATION_CHECKPOINT_SCOPE_INVALID")
    group_by_id = {item.group_id: item for item in plan.groups}
    for group_id, receipt in receipt_by_group.items():
        group = group_by_id.get(group_id)
        compilation = compilation_by_group[group_id]
        accepted = next(
            (
                item
                for item in receipt.attempts
                if item.attempt_id == receipt.accepted_attempt_id
            ),
            None,
        )
        if (
            group is None
            or receipt.status
            not in {
                ParentProvisionReconciliationGroupStatus.ACCEPTED,
                ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            }
            or receipt.reconciliation_plan_id != plan.reconciliation_plan_id
            or receipt.reconciliation_plan_sha256 != plan.reconciliation_plan_sha256
            or receipt.group_order != group.group_order
            or receipt.group_scope_sha256 != group.group_scope_sha256
            or compilation.reconciliation_plan_id != plan.reconciliation_plan_id
            or compilation.reconciliation_plan_sha256 != plan.reconciliation_plan_sha256
            or compilation.group_order != group.group_order
            or compilation.group_scope_sha256 != group.group_scope_sha256
            or accepted is None
            or accepted.reconciliation_compilation_result_sha256
            != compilation.result_sha256
        ):
            raise LifecycleProbeError(
                "OBJECT_RECONCILIATION_CHECKPOINT_LINEAGE_INVALID"
            )
        try:
            replayed = compile_parent_provision_object_reconciliation_candidate(
                object_batch_plan=initial_result.plan,
                expected_object_batch_plan_sha256=initial_result.plan.plan_sha256,
                reconciliation_plan=plan,
                expected_reconciliation_plan_sha256=plan.reconciliation_plan_sha256,
                group_id=group_id,
                local_results=initial_result.compilations,
                d2_identity=compiler.d2_identity,
                source_segments=compiler.source_segments,
                kit_identity=compiler.kit_identity,
                candidate=compilation.model_candidate,
            )
        except (ValidationError, ValueError) as exc:
            raise LifecycleProbeError(
                "OBJECT_RECONCILIATION_CHECKPOINT_REPLAY_FAILED"
            ) from exc
        if replayed != compilation:
            raise LifecycleProbeError(
                "OBJECT_RECONCILIATION_CHECKPOINT_COMPILATION_DRIFT"
            )
    ordered_ids = tuple(
        group.group_id for group in groups if group.group_id in receipt_by_group
    )
    if (
        tuple(item.group_id for item in receipts) != ordered_ids
        or tuple(item.group_id for item in compilations) != ordered_ids
    ):
        raise LifecycleProbeError("OBJECT_RECONCILIATION_CHECKPOINT_ORDER_INVALID")
    return receipt_by_group, compilation_by_group


def _ordered_groups(
    plan: ParentProvisionObjectReconciliationPlanV1,
    execution_order: tuple[str, ...] | None,
) -> tuple[ParentProvisionObjectReconciliationGroupV1, ...]:
    if execution_order is None:
        return plan.groups
    if len(execution_order) != len(set(execution_order)) or set(execution_order) != {
        item.group_id for item in plan.groups
    }:
        raise LifecycleProbeError("OBJECT_RECONCILIATION_EXECUTION_ORDER_INVALID")
    by_id = {item.group_id: item for item in plan.groups}
    return tuple(by_id[item] for item in execution_order)


def _load_selected_output_schema(activation: Any) -> dict[str, Any]:
    try:
        payload = activation.runner_output_schema_path.read_bytes()
        schema = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError(
            "OBJECT_RECONCILIATION_RUNNER_SCHEMA_INVALID"
        ) from exc
    if (
        not isinstance(schema, Mapping)
        or hashlib.sha256(payload).hexdigest() != activation.runner_output_schema_sha256
    ):
        raise LifecycleProbeError("OBJECT_RECONCILIATION_RUNNER_SCHEMA_INVALID")
    return dict(schema)


def _stage_binding(activation: Any) -> Any:
    try:
        return next(
            item
            for item in activation.session_attestation.stages
            if item.stage is activation.stage
        )
    except StopIteration as exc:
        raise LifecycleProbeError(
            "OBJECT_RECONCILIATION_STAGE_BINDING_MISSING"
        ) from exc


def _invoke_transport(
    transport: CanonicalStructuredStageTransportPort,
    invocation: CanonicalStageInvocation,
) -> CanonicalStageTransportResult:
    result = transport.invoke(invocation)
    if (
        not isinstance(result, CanonicalStageTransportResult)
        or result.stage is not invocation.stage
        or result.output_schema_sha256 != invocation.turn_output_schema_sha256
    ):
        raise LifecycleProbeError("CANONICAL_STAGE_TRANSPORT_RECEIPT_INVALID")
    return result


def _classify_transport_failure(
    reason_code: str,
) -> tuple[
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionCleanupStatus,
    bool,
]:
    if "CANCELLED" in reason_code:
        cleanup = (
            ParentProvisionRecognitionCleanupStatus.NOT_REQUIRED
            if reason_code.endswith("BEFORE_START")
            else ParentProvisionRecognitionCleanupStatus.SUCCEEDED
        )
        return ParentProvisionRecognitionAttemptOutcome.CANCELLED, cleanup, True
    if "INDETERMINATE" in reason_code or "STATE_UNKNOWN" in reason_code:
        return (
            ParentProvisionRecognitionAttemptOutcome.INDETERMINATE,
            ParentProvisionRecognitionCleanupStatus.UNKNOWN,
            True,
        )
    if reason_code in {"CANONICAL_STAGE_CLEANUP_FAILED", "MODEL_WORKSPACE_MUTATED"}:
        return (
            ParentProvisionRecognitionAttemptOutcome.BLOCKED,
            ParentProvisionRecognitionCleanupStatus.FAILED,
            False,
        )
    if reason_code in {
        "AUTH_REQUIRED",
        "MODEL_UNAVAILABLE",
        "CANONICAL_STAGE_APPROVAL_REQUEST_FORBIDDEN",
        "CANONICAL_STAGE_ACTIVATION_IDENTITY_MISMATCH",
        "CANONICAL_STAGE_INVOCATION_INVALID",
        "CANONICAL_STAGE_OUTPUT_SCHEMA_DRIFT",
        "CANONICAL_STAGE_RUNNER_SCHEMA_MISMATCH",
        "CANONICAL_STAGE_TRANSPORT_RECEIPT_INVALID",
        "OBJECT_RECONCILIATION_SCHEMA_ACTIVATION_MISMATCH",
    }:
        return (
            ParentProvisionRecognitionAttemptOutcome.BLOCKED,
            ParentProvisionRecognitionCleanupStatus.NOT_REQUIRED,
            True,
        )
    return (
        ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
        ParentProvisionRecognitionCleanupStatus.SUCCEEDED,
        True,
    )


def _machine_reason_code(exc: BaseException, *, default: str) -> str:
    value = str(exc).strip()
    return value if _MACHINE_CODE_PATTERN.fullmatch(value) else default


def _transport_result_sha256(result: CanonicalStageTransportResult) -> str:
    return canonical_json_sha256(
        {
            "stage": result.stage.value,
            "output_schema_sha256": result.output_schema_sha256,
            "response_sha256": result.response_sha256,
            "tool_receipt_sha256": result.tool_receipt_sha256,
            "runtime_thread_id_sha256": _text_sha256(result.runtime_thread_id),
            "runtime_turn_id_sha256": _text_sha256(result.runtime_turn_id),
        }
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unresolved_entry_count(entries: tuple[Any, ...]) -> int:
    return sum(
        getattr(item.requirementness, "value", item.requirementness) == "UNRESOLVED"
        for item in entries
    )


__all__ = [
    "PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE",
    "PARENT_PROVISION_OBJECT_RECONCILIATION_PROMPT_BINDING_VERSION",
    "ParentProvisionObjectReconciliationStageDriver",
    "ParentProvisionObjectReconciliationStageRunResult",
]
