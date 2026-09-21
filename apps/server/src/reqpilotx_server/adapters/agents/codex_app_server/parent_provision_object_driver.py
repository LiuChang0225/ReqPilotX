from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
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
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
    ParentProvisionStatus,
    Requirementness,
    parent_provision_evidence_refs,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage
from reqpilotx_server.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateExecutionReceiptV2,
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionAttemptReceiptV1,
    ParentProvisionRecognitionBatchReceiptV1,
    ParentProvisionRecognitionBatchStatus,
    ParentProvisionRecognitionCleanupStatus,
    build_parent_provision_recognition_aggregate_execution_receipt,
    build_parent_provision_recognition_attempt_receipt,
    build_parent_provision_recognition_batch_receipt,
    canonical_json_sha256,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION,
    PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION,
    ParentProvisionObjectBatchAggregateV1,
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectBatchTargetV1,
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectScopeStatus,
    ParentProvisionObjectWindowCandidateV1,
)


PARENT_PROVISION_OBJECT_EXECUTION_MODE = "OBJECT_BATCH"
PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE = "OBJECT_WINDOW"
PARENT_PROVISION_OBJECT_PROMPT_BINDING_VERSION = (
    "REQPILOTX_PARENT_PROVISION_OBJECT_BINDING_V1"
)
PARENT_PROVISION_OBJECT_WINDOW_PROMPT_BINDING_VERSION = (
    "REQPILOTX_PARENT_PROVISION_OBJECT_WINDOW_BINDING_V1"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
logger = logging.getLogger(__name__)
_CORRECTION_HINT_BY_CODE = {
    "OBJECT_BATCH_SPAN_ESCAPES_TARGET": (
        "Use only this batch's target_segment_ordinals. If the correct parent "
        "crosses the object edge, return ADJACENT_RECONCILIATION_REQUIRED "
        "without emitting an out-of-scope span."
    ),
    "OBJECT_WINDOW_WORKING_DECISION_REQUIRED": (
        "Return a concrete working requirementness, requirement kind, and "
        "technicality; express uncertainty through confidence and reason codes."
    ),
    "OBJECT_BATCH_SOURCE_COVERAGE_INCOMPLETE": (
        "Partition every non-whitespace character in this object exactly once. "
        "Do not omit headings, notes, or residual text; keep them as requirement "
        "or non-requirement parents as supported by the source."
    ),
    "OBJECT_BATCH_SOURCE_SPANS_OVERLAP": (
        "Remove overlapping source spans and keep one exact owner for every "
        "source character."
    ),
    "OBJECT_BATCH_SOURCE_ORDER_INVALID": (
        "Return parents and spans in validated source order without reversal."
    ),
    "OBJECT_BATCH_EXPLICIT_LABEL_UNGROUNDED": (
        "Use an exact source-grounded label or null; do not normalize or invent "
        "a label."
    ),
}
_HOST_COMPILER_REASON_BY_MESSAGE = {
    "must disposition every non-whitespace source character": (
        "OBJECT_BATCH_SOURCE_COVERAGE_INCOMPLETE"
    ),
    "candidate parent spans overlap": "OBJECT_BATCH_SOURCE_SPANS_OVERLAP",
    "parent source ranges overlap": "OBJECT_BATCH_SOURCE_SPANS_OVERLAP",
    "candidate spans must preserve validated D2 order": (
        "OBJECT_BATCH_SOURCE_ORDER_INVALID"
    ),
    "candidate parents must preserve global D2 source order": (
        "OBJECT_BATCH_SOURCE_ORDER_INVALID"
    ),
    "candidate explicit label is not source-grounded": (
        "OBJECT_BATCH_EXPLICIT_LABEL_UNGROUNDED"
    ),
    "candidate parent span cannot be whitespace-only": (
        "OBJECT_BATCH_WHITESPACE_ONLY_PARENT"
    ),
    "requirements must declare a requirement kind": (
        "OBJECT_BATCH_REQUIREMENT_KIND_REQUIRED"
    ),
    "non-requirements cannot claim a requirement kind": (
        "OBJECT_BATCH_NON_REQUIREMENT_KIND_FORBIDDEN"
    ),
}


@dataclass(frozen=True, slots=True)
class ParentProvisionObjectStageRunResult:
    """Working object-run closure; no professional final proposal is published."""

    plan: ParentProvisionObjectBatchPlanV1
    compilations: tuple[ParentProvisionObjectBatchCompilationV1, ...]
    batch_receipts: tuple[ParentProvisionRecognitionBatchReceiptV1, ...]
    object_batch_aggregate: ParentProvisionObjectBatchAggregateV1 | None
    execution_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2


class ParentProvisionObjectStageDriver:
    """Run the parent-provision stage once per immutable logical object.

    This driver deliberately stops before adjacent-object reconciliation and
    professional aggregate attestation.  Its successful aggregate is only the
    Host-compiled working decision used by those later steps.
    """

    def __init__(
        self,
        *,
        transport: CanonicalStructuredStageTransportPort,
        object_compiler: CanonicalParentProvisionObjectBatchCompiler,
        max_attempts_per_object: int = 2,
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
    ) -> None:
        if (
            isinstance(max_attempts_per_object, bool)
            or not isinstance(max_attempts_per_object, int)
            or not 1 <= max_attempts_per_object <= 64
        ):
            raise ValueError("object retry budget must be between 1 and 64")
        if (
            isinstance(max_targets_per_turn, bool)
            or not isinstance(max_targets_per_turn, int)
            or not 1 <= max_targets_per_turn <= 32
        ):
            raise ValueError("object window target budget must be between 1 and 32")
        if (
            isinstance(max_source_characters_per_turn, bool)
            or not isinstance(max_source_characters_per_turn, int)
            or not 1_000 <= max_source_characters_per_turn <= 2_000_000
        ):
            raise ValueError("object window source-character budget must be bounded")
        self._transport = transport
        self._object_compiler = object_compiler
        self._max_attempts_per_object = max_attempts_per_object
        self._max_targets_per_turn = max_targets_per_turn
        self._max_source_characters_per_turn = max_source_characters_per_turn
        self._window_checkpoint_sink = window_checkpoint_sink

    def run(
        self,
        session: CanonicalStageSessionPort,
        *,
        task_binding_sha256: str,
        plan: ParentProvisionObjectBatchPlanV1 | None = None,
        resumed_compilations: tuple[ParentProvisionObjectBatchCompilationV1, ...] = (),
        resumed_batch_receipts: tuple[
            ParentProvisionRecognitionBatchReceiptV1, ...
        ] = (),
    ) -> ParentProvisionObjectStageRunResult:
        if _SHA256_PATTERN.fullmatch(task_binding_sha256) is None:
            raise LifecycleProbeError("OBJECT_BATCH_TASK_BINDING_INVALID")
        windowed = self._max_targets_per_turn > 1
        execution_mode = (
            PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE
            if windowed
            else PARENT_PROVISION_OBJECT_EXECUTION_MODE
        )
        expected_schema_id = (
            PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION
            if windowed
            else PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION
        )
        activation = session.require_inference_stage(
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            execution_mode=execution_mode,
        )
        if (
            activation.runner_output_execution_mode != execution_mode
            or activation.runner_output_schema_id != expected_schema_id
        ):
            raise LifecycleProbeError("OBJECT_BATCH_SCHEMA_ACTIVATION_MISMATCH")
        output_schema = _load_selected_output_schema(activation)
        object_plan = plan or self._object_compiler.build_plan()
        _require_plan_lineage(object_plan, self._object_compiler)
        _require_activation_lineage(activation, self._object_compiler)
        resumed_receipt_by_batch, resumed_compilation_by_batch = (
            _validated_resumed_window_results(
                plan=object_plan,
                compiler=self._object_compiler,
                receipts=resumed_batch_receipts,
                compilations=resumed_compilations,
            )
        )
        resumed_batch_ids = frozenset(resumed_receipt_by_batch)
        checkpointed_batch_ids = set(resumed_batch_ids)

        compilations: list[ParentProvisionObjectBatchCompilationV1] = []
        batch_receipts: list[ParentProvisionRecognitionBatchReceiptV1] = []
        target_index = 0
        while target_index < len(object_plan.targets):
            target = object_plan.targets[target_index]
            resumed_receipt = resumed_receipt_by_batch.get(target.batch_id)
            if resumed_receipt is not None:
                batch_receipts.append(resumed_receipt)
                resumed_compilation = resumed_compilation_by_batch[target.batch_id]
                compilations.append(resumed_compilation)
                target_index += 1
                continue
            if (
                target.execution_mode
                is ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
            ):
                batch_receipts.append(
                    _build_batch_receipt(
                        compiler=self._object_compiler,
                        plan=object_plan,
                        target=target,
                        attempts=(),
                        status=(
                            ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER
                        ),
                        reason_codes=("HOST_EMPTY_CONTAINER",),
                    )
                )
                target_index += 1
                continue
            if (
                target.execution_mode
                is ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
            ):
                batch_receipts.append(
                    _build_batch_receipt(
                        compiler=self._object_compiler,
                        plan=object_plan,
                        target=target,
                        attempts=(),
                        status=ParentProvisionRecognitionBatchStatus.BLOCKED,
                        reason_codes=("OBJECT_BATCH_EVIDENCE_RECOVERY_REQUIRED",),
                    )
                )
                # Evidence recovery is object-local.  Preserve the checkpoint and
                # keep running independent objects; only whole-document publication
                # remains unavailable until this object is recovered.
                target_index += 1
                continue

            if windowed:
                window = _source_window_targets(
                    object_plan.targets,
                    start_index=target_index,
                    max_targets=self._max_targets_per_turn,
                    max_source_characters=self._max_source_characters_per_turn,
                    excluded_batch_ids=resumed_batch_ids,
                )
                window_results = self._run_model_window(
                    activation=activation,
                    output_schema=output_schema,
                    plan=object_plan,
                    targets=window,
                    base_task_binding_sha256=task_binding_sha256,
                    _checkpointed_batch_ids=checkpointed_batch_ids,
                )
                self._checkpoint_accepted_results(
                    window_results,
                    checkpointed_batch_ids=checkpointed_batch_ids,
                )
                target_index += len(window)
            else:
                window_results = (
                    self._run_model_target(
                        activation=activation,
                        output_schema=output_schema,
                        plan=object_plan,
                        target=target,
                        base_task_binding_sha256=task_binding_sha256,
                    ),
                )
                target_index += 1
            stop = False
            for batch, compilation in window_results:
                batch_receipts.append(batch)
                if compilation is not None:
                    compilations.append(compilation)
                if batch.status in {
                    ParentProvisionRecognitionBatchStatus.CANCELLED,
                    ParentProvisionRecognitionBatchStatus.BLOCKED,
                    ParentProvisionRecognitionBatchStatus.INDETERMINATE,
                }:
                    stop = True
            if stop:
                break

        object_aggregate: ParentProvisionObjectBatchAggregateV1 | None = None
        if len(batch_receipts) == len(object_plan.targets) and all(
            item.status
            in {
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
            }
            for item in batch_receipts
        ):
            object_aggregate = self._object_compiler.aggregate(
                plan=object_plan,
                results=tuple(compilations),
            )

        execution_receipt = _build_aggregate_receipt(
            activation=activation,
            compiler=self._object_compiler,
            plan=object_plan,
            batches=tuple(batch_receipts),
            object_aggregate=object_aggregate,
        )
        return ParentProvisionObjectStageRunResult(
            plan=object_plan,
            compilations=tuple(compilations),
            batch_receipts=tuple(batch_receipts),
            object_batch_aggregate=object_aggregate,
            execution_receipt=execution_receipt,
        )

    def _run_model_target(
        self,
        *,
        activation: Any,
        output_schema: Mapping[str, Any],
        plan: ParentProvisionObjectBatchPlanV1,
        target: ParentProvisionObjectBatchTargetV1,
        base_task_binding_sha256: str,
    ) -> tuple[
        ParentProvisionRecognitionBatchReceiptV1,
        ParentProvisionObjectBatchCompilationV1 | None,
    ]:
        attempts: list[ParentProvisionRecognitionAttemptReceiptV1] = []
        for attempt_ordinal in range(1, self._max_attempts_per_object + 1):
            previous = attempts[-1] if attempts else None
            attempt_task_binding_sha256 = _attempt_task_binding_sha256(
                base_task_binding_sha256=base_task_binding_sha256,
                plan=plan,
                target=target,
                attempt_ordinal=attempt_ordinal,
                previous=previous,
            )
            try:
                invocation = _build_object_invocation(
                    activation=activation,
                    output_schema=output_schema,
                    plan=plan,
                    target=target,
                    task_binding_sha256=attempt_task_binding_sha256,
                    attempt_ordinal=attempt_ordinal,
                    previous=previous,
                )
                transport_result = _invoke_transport(self._transport, invocation)
            except LifecycleProbeError as exc:
                failure = _transport_failure_receipt(
                    activation=activation,
                    compiler=self._object_compiler,
                    plan=plan,
                    target=target,
                    attempt_ordinal=attempt_ordinal,
                    task_binding_sha256=attempt_task_binding_sha256,
                    reason_code=exc.reason_code,
                )
                attempts.append(failure)
                if (
                    failure.outcome
                    is ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED
                    and attempt_ordinal < self._max_attempts_per_object
                ):
                    continue
                return (
                    _terminal_batch_receipt(
                        compiler=self._object_compiler,
                        plan=plan,
                        target=target,
                        attempts=tuple(attempts),
                    ),
                    None,
                )
            except Exception:
                failure = _transport_failure_receipt(
                    activation=activation,
                    compiler=self._object_compiler,
                    plan=plan,
                    target=target,
                    attempt_ordinal=attempt_ordinal,
                    task_binding_sha256=attempt_task_binding_sha256,
                    reason_code="CANONICAL_STAGE_TRANSPORT_FAILED",
                )
                attempts.append(failure)
                if attempt_ordinal < self._max_attempts_per_object:
                    continue
                return (
                    _terminal_batch_receipt(
                        compiler=self._object_compiler,
                        plan=plan,
                        target=target,
                        attempts=tuple(attempts),
                    ),
                    None,
                )

            candidate: ParentProvisionObjectCandidateV1 | None = None
            rejection_reason: str | None = None
            compilation: ParentProvisionObjectBatchCompilationV1 | None = None
            try:
                candidate = ParentProvisionObjectCandidateV1.model_validate(
                    transport_result.structured_output
                )
            except ValidationError:
                rejection_reason = "OBJECT_BATCH_CANDIDATE_SCHEMA_REJECTED"
            except Exception:
                rejection_reason = "OBJECT_BATCH_CANDIDATE_INVALID"
            if candidate is not None:
                try:
                    compilation = self._object_compiler.compile_object(
                        plan=plan,
                        batch_id=target.batch_id,
                        candidate=candidate,
                    )
                except (ValidationError, ValueError) as exc:
                    rejection_reason = _machine_reason_code(
                        exc,
                        default="OBJECT_BATCH_HOST_REJECTED",
                    )
                except Exception:
                    rejection_reason = "OBJECT_BATCH_HOST_COMPILER_FAILED"

            if compilation is None:
                rejected = _completed_transport_attempt_receipt(
                    activation=activation,
                    compiler=self._object_compiler,
                    plan=plan,
                    target=target,
                    attempt_ordinal=attempt_ordinal,
                    task_binding_sha256=attempt_task_binding_sha256,
                    transport_result=transport_result,
                    outcome=ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED,
                    candidate_sha256=(
                        candidate.candidate_sha256 if candidate is not None else None
                    ),
                    batch_compilation_result_sha256=None,
                    semantic_unresolved_scope_count=0,
                    semantic_unresolved_entry_count=0,
                    reason_codes=(rejection_reason or "OBJECT_BATCH_OUTPUT_REJECTED",),
                )
                attempts.append(rejected)
                if attempt_ordinal < self._max_attempts_per_object:
                    continue
                return (
                    _terminal_batch_receipt(
                        compiler=self._object_compiler,
                        plan=plan,
                        target=target,
                        attempts=tuple(attempts),
                    ),
                    None,
                )

            semantic_unresolved_scopes = (
                len(compilation.compiled_decision.unresolved_logical_object_ids)
                if compilation.compiled_decision is not None
                else 0
            )
            semantic_unresolved_entries = (
                sum(
                    entry.requirementness is Requirementness.UNRESOLVED
                    for entry in compilation.compiled_decision.entries
                )
                if compilation.compiled_decision is not None
                else 0
            )
            accepted_attempt = _completed_transport_attempt_receipt(
                activation=activation,
                compiler=self._object_compiler,
                plan=plan,
                target=target,
                attempt_ordinal=attempt_ordinal,
                task_binding_sha256=attempt_task_binding_sha256,
                transport_result=transport_result,
                outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
                candidate_sha256=compilation.model_candidate_sha256,
                batch_compilation_result_sha256=compilation.result_sha256,
                semantic_unresolved_scope_count=semantic_unresolved_scopes,
                semantic_unresolved_entry_count=semantic_unresolved_entries,
                reason_codes=(),
            )
            attempts.append(accepted_attempt)
            if (
                compilation.model_candidate.scope_status
                is ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
            ):
                status = ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
                reason_codes = ("ADJACENT_RECONCILIATION_REQUIRED",)
            elif semantic_unresolved_scopes or semantic_unresolved_entries:
                status = ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
                reason_codes = ()
            else:
                status = ParentProvisionRecognitionBatchStatus.ACCEPTED
                reason_codes = ()
            return (
                _build_batch_receipt(
                    compiler=self._object_compiler,
                    plan=plan,
                    target=target,
                    attempts=tuple(attempts),
                    accepted_attempt=accepted_attempt,
                    status=status,
                    semantic_unresolved_scope_count=semantic_unresolved_scopes,
                    semantic_unresolved_entry_count=semantic_unresolved_entries,
                    reason_codes=reason_codes,
                ),
                compilation,
            )
        raise AssertionError("finite retry loop exited without a terminal receipt")

    def _run_model_window(
        self,
        *,
        activation: Any,
        output_schema: Mapping[str, Any],
        plan: ParentProvisionObjectBatchPlanV1,
        targets: tuple[ParentProvisionObjectBatchTargetV1, ...],
        base_task_binding_sha256: str,
        _initial_attempts_by_batch: Mapping[
            str,
            tuple[ParentProvisionRecognitionAttemptReceiptV1, ...],
        ]
        | None = None,
        _start_attempt_ordinal: int = 1,
        _checkpointed_batch_ids: set[str] | None = None,
    ) -> tuple[
        tuple[
            ParentProvisionRecognitionBatchReceiptV1,
            ParentProvisionObjectBatchCompilationV1 | None,
        ],
        ...,
    ]:
        if not 1 <= _start_attempt_ordinal <= self._max_attempts_per_object:
            raise LifecycleProbeError("OBJECT_WINDOW_RETRY_ORDINAL_INVALID")
        expected_batch_ids = {target.batch_id for target in targets}
        if (
            _initial_attempts_by_batch is not None
            and set(_initial_attempts_by_batch) != expected_batch_ids
        ):
            raise LifecycleProbeError("OBJECT_WINDOW_RETRY_SCOPE_DRIFT")
        attempts_by_batch: dict[
            str,
            list[ParentProvisionRecognitionAttemptReceiptV1],
        ] = {
            target.batch_id: list(
                (_initial_attempts_by_batch or {}).get(target.batch_id, ())
            )
            for target in targets
        }
        accepted_by_batch: dict[
            str,
            tuple[
                ParentProvisionRecognitionBatchReceiptV1,
                ParentProvisionObjectBatchCompilationV1,
            ],
        ] = {}
        pending_targets = targets
        checkpointed_batch_ids = (
            _checkpointed_batch_ids if _checkpointed_batch_ids is not None else set()
        )
        for attempt_ordinal in range(
            _start_attempt_ordinal,
            self._max_attempts_per_object + 1,
        ):
            previous_by_batch = {
                target.batch_id: attempts_by_batch[target.batch_id][-1]
                for target in pending_targets
                if attempts_by_batch[target.batch_id]
            }
            window_task_binding = _window_attempt_task_binding_sha256(
                base_task_binding_sha256=base_task_binding_sha256,
                plan=plan,
                targets=pending_targets,
                attempt_ordinal=attempt_ordinal,
                previous_by_batch=previous_by_batch,
            )
            try:
                invocation = _build_object_window_invocation(
                    activation=activation,
                    output_schema=output_schema,
                    plan=plan,
                    targets=pending_targets,
                    task_binding_sha256=window_task_binding,
                    attempt_ordinal=attempt_ordinal,
                    previous_by_batch=previous_by_batch,
                )
                transport_result = _invoke_transport(self._transport, invocation)
            except LifecycleProbeError as exc:
                for target in pending_targets:
                    attempts_by_batch[target.batch_id].append(
                        _transport_failure_receipt(
                            activation=activation,
                            compiler=self._object_compiler,
                            plan=plan,
                            target=target,
                            attempt_ordinal=attempt_ordinal,
                            task_binding_sha256=_window_target_task_binding_sha256(
                                window_task_binding=window_task_binding,
                                target=target,
                            ),
                            reason_code=exc.reason_code,
                        )
                    )
                if (
                    exc.reason_code == "APP_SERVER_TIMEOUT"
                    and len(pending_targets) > 1
                    and attempt_ordinal < self._max_attempts_per_object
                ):
                    split_at = len(pending_targets) // 2
                    split_results: dict[
                        str,
                        tuple[
                            ParentProvisionRecognitionBatchReceiptV1,
                            ParentProvisionObjectBatchCompilationV1 | None,
                        ],
                    ] = {}
                    for subgroup in (
                        pending_targets[:split_at],
                        pending_targets[split_at:],
                    ):
                        subgroup_results = self._run_model_window(
                            activation=activation,
                            output_schema=output_schema,
                            plan=plan,
                            targets=subgroup,
                            base_task_binding_sha256=base_task_binding_sha256,
                            _initial_attempts_by_batch={
                                target.batch_id: tuple(
                                    attempts_by_batch[target.batch_id]
                                )
                                for target in subgroup
                            },
                            _start_attempt_ordinal=attempt_ordinal + 1,
                            _checkpointed_batch_ids=checkpointed_batch_ids,
                        )
                        self._checkpoint_accepted_results(
                            subgroup_results,
                            checkpointed_batch_ids=checkpointed_batch_ids,
                        )
                        split_results.update(
                            {
                                target.batch_id: result
                                for target, result in zip(
                                    subgroup,
                                    subgroup_results,
                                    strict=True,
                                )
                            }
                        )
                    return tuple(
                        accepted_by_batch.get(target.batch_id)
                        or split_results[target.batch_id]
                        for target in targets
                    )
                if attempt_ordinal < self._max_attempts_per_object and all(
                    attempts_by_batch[target.batch_id][-1].outcome
                    is ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED
                    for target in pending_targets
                ):
                    continue
                return tuple(
                    accepted_by_batch.get(target.batch_id)
                    or (
                        _terminal_batch_receipt(
                            compiler=self._object_compiler,
                            plan=plan,
                            target=target,
                            attempts=tuple(attempts_by_batch[target.batch_id]),
                        ),
                        None,
                    )
                    for target in targets
                )
            except Exception:
                for target in pending_targets:
                    attempts_by_batch[target.batch_id].append(
                        _transport_failure_receipt(
                            activation=activation,
                            compiler=self._object_compiler,
                            plan=plan,
                            target=target,
                            attempt_ordinal=attempt_ordinal,
                            task_binding_sha256=_window_target_task_binding_sha256(
                                window_task_binding=window_task_binding,
                                target=target,
                            ),
                            reason_code="CANONICAL_STAGE_TRANSPORT_FAILED",
                        )
                    )
                if attempt_ordinal < self._max_attempts_per_object:
                    continue
                return tuple(
                    accepted_by_batch.get(target.batch_id)
                    or (
                        _terminal_batch_receipt(
                            compiler=self._object_compiler,
                            plan=plan,
                            target=target,
                            attempts=tuple(attempts_by_batch[target.batch_id]),
                        ),
                        None,
                    )
                    for target in targets
                )

            window_candidate: ParentProvisionObjectWindowCandidateV1 | None = None
            rejection_by_batch: dict[str, str] = {}
            item_by_batch: dict[str, ParentProvisionObjectCandidateV1] = {}
            compilation_by_batch: dict[
                str,
                ParentProvisionObjectBatchCompilationV1,
            ] = {}
            try:
                window_candidate = (
                    ParentProvisionObjectWindowCandidateV1.model_validate(
                        transport_result.structured_output
                    )
                )
                observed_ids = tuple(item.batch_id for item in window_candidate.items)
                expected_ids = tuple(target.batch_id for target in pending_targets)
                if observed_ids != expected_ids:
                    raise ValueError("OBJECT_WINDOW_BATCH_SCOPE_DRIFT")
                item_by_batch = {
                    item.batch_id: item.candidate for item in window_candidate.items
                }
            except ValidationError:
                rejection_by_batch = {
                    target.batch_id: "OBJECT_WINDOW_CANDIDATE_SCHEMA_REJECTED"
                    for target in pending_targets
                }
            except ValueError as exc:
                reason = _machine_reason_code(
                    exc,
                    default="OBJECT_WINDOW_BATCH_SCOPE_DRIFT",
                )
                rejection_by_batch = {
                    target.batch_id: reason for target in pending_targets
                }
            except Exception:
                rejection_by_batch = {
                    target.batch_id: "OBJECT_WINDOW_CANDIDATE_INVALID"
                    for target in pending_targets
                }

            if not rejection_by_batch:
                for target in pending_targets:
                    candidate = item_by_batch[target.batch_id]
                    if (
                        candidate.scope_status
                        is ParentProvisionObjectScopeStatus.UNRESOLVED
                    ):
                        rejection_by_batch[target.batch_id] = (
                            "OBJECT_WINDOW_WORKING_DECISION_REQUIRED"
                        )
                        continue
                    try:
                        compilation = self._object_compiler.compile_object(
                            plan=plan,
                            batch_id=target.batch_id,
                            candidate=candidate,
                        )
                    except (ValidationError, ValueError) as exc:
                        rejection_reason = _machine_reason_code(
                            exc,
                            default="OBJECT_WINDOW_HOST_REJECTED",
                        )
                        rejection_by_batch[target.batch_id] = rejection_reason
                        if rejection_reason == "OBJECT_WINDOW_HOST_REJECTED":
                            logger.warning(
                                "Object-window Host rejection was not classified. "
                                "batch_id=%s compiler_error=%s",
                                target.batch_id,
                                _safe_compiler_error_summary(exc),
                            )
                        continue
                    except Exception:
                        rejection_by_batch[target.batch_id] = (
                            "OBJECT_WINDOW_HOST_COMPILER_FAILED"
                        )
                        continue
                    if _compilation_has_semantic_unresolved(compilation):
                        rejection_by_batch[target.batch_id] = (
                            "OBJECT_WINDOW_WORKING_DECISION_REQUIRED"
                        )
                        continue
                    compilation_by_batch[target.batch_id] = compilation

            next_pending: list[ParentProvisionObjectBatchTargetV1] = []
            for target in pending_targets:
                if target.batch_id in rejection_by_batch:
                    candidate = item_by_batch.get(target.batch_id)
                    reason = rejection_by_batch[target.batch_id]
                    attempts_by_batch[target.batch_id].append(
                        _completed_transport_attempt_receipt(
                            activation=activation,
                            compiler=self._object_compiler,
                            plan=plan,
                            target=target,
                            attempt_ordinal=attempt_ordinal,
                            task_binding_sha256=_window_target_task_binding_sha256(
                                window_task_binding=window_task_binding,
                                target=target,
                            ),
                            transport_result=transport_result,
                            outcome=(
                                ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED
                            ),
                            candidate_sha256=(
                                candidate.candidate_sha256
                                if candidate is not None
                                else None
                            ),
                            batch_compilation_result_sha256=None,
                            semantic_unresolved_scope_count=0,
                            semantic_unresolved_entry_count=0,
                            reason_codes=(reason,),
                        )
                    )
                    next_pending.append(target)
                    continue
                compilation = compilation_by_batch[target.batch_id]
                accepted_attempt = _completed_transport_attempt_receipt(
                    activation=activation,
                    compiler=self._object_compiler,
                    plan=plan,
                    target=target,
                    attempt_ordinal=attempt_ordinal,
                    task_binding_sha256=_window_target_task_binding_sha256(
                        window_task_binding=window_task_binding,
                        target=target,
                    ),
                    transport_result=transport_result,
                    outcome=(
                        ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE
                    ),
                    candidate_sha256=compilation.model_candidate_sha256,
                    batch_compilation_result_sha256=compilation.result_sha256,
                    semantic_unresolved_scope_count=0,
                    semantic_unresolved_entry_count=0,
                    reason_codes=(),
                )
                attempts_by_batch[target.batch_id].append(accepted_attempt)
                if (
                    compilation.model_candidate.scope_status
                    is ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ):
                    status = ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
                    reasons = ("ADJACENT_RECONCILIATION_REQUIRED",)
                else:
                    status = ParentProvisionRecognitionBatchStatus.ACCEPTED
                    reasons = ()
                accepted_by_batch[target.batch_id] = (
                    _build_batch_receipt(
                        compiler=self._object_compiler,
                        plan=plan,
                        target=target,
                        attempts=tuple(attempts_by_batch[target.batch_id]),
                        accepted_attempt=accepted_attempt,
                        status=status,
                        reason_codes=reasons,
                    ),
                    compilation,
                )
            if not next_pending:
                return tuple(accepted_by_batch[target.batch_id] for target in targets)
            self._checkpoint_accepted_results(
                tuple(
                    accepted_by_batch[target.batch_id]
                    for target in pending_targets
                    if target.batch_id in accepted_by_batch
                ),
                checkpointed_batch_ids=checkpointed_batch_ids,
            )
            pending_targets = tuple(next_pending)
            if attempt_ordinal < self._max_attempts_per_object:
                continue
            return tuple(
                accepted_by_batch.get(target.batch_id)
                or (
                    _terminal_batch_receipt(
                        compiler=self._object_compiler,
                        plan=plan,
                        target=target,
                        attempts=tuple(attempts_by_batch[target.batch_id]),
                    ),
                    None,
                )
                for target in targets
            )
        raise AssertionError("finite window retry loop exited without a receipt")

    def _checkpoint_accepted_results(
        self,
        results: tuple[
            tuple[
                ParentProvisionRecognitionBatchReceiptV1,
                ParentProvisionObjectBatchCompilationV1 | None,
            ],
            ...,
        ],
        *,
        checkpointed_batch_ids: set[str],
    ) -> None:
        values = tuple(
            (batch, compilation)
            for batch, compilation in results
            if batch.batch_id not in checkpointed_batch_ids
            and compilation is not None
            and batch.status
            in {
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
            }
        )
        if self._window_checkpoint_sink is None or not values:
            return
        self._window_checkpoint_sink(values)
        checkpointed_batch_ids.update(batch.batch_id for batch, _ in values)


def _source_window_targets(
    targets: tuple[ParentProvisionObjectBatchTargetV1, ...],
    *,
    start_index: int,
    max_targets: int,
    max_source_characters: int,
    excluded_batch_ids: frozenset[str] = frozenset(),
) -> tuple[ParentProvisionObjectBatchTargetV1, ...]:
    result: list[ParentProvisionObjectBatchTargetV1] = []
    source_characters = 0
    for target in targets[start_index:]:
        # A resumed target is already backed by a Host-validated checkpoint.
        # Treat it as a hard source-window boundary so a later retry never asks
        # the Agent to regenerate an already accepted object.
        if target.batch_id in excluded_batch_ids:
            break
        if target.execution_mode is not ParentProvisionObjectExecutionMode.MODEL_TARGET:
            break
        target_characters = sum(
            item.source_text_length for item in target.segment_bindings
        )
        if result and (
            len(result) >= max_targets
            or source_characters + target_characters > max_source_characters
        ):
            break
        result.append(target)
        source_characters += target_characters
        if len(result) >= max_targets:
            break
    if not result:
        raise LifecycleProbeError("OBJECT_WINDOW_TARGETS_MISSING")
    return tuple(result)


def _validated_resumed_window_results(
    *,
    plan: ParentProvisionObjectBatchPlanV1,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    receipts: tuple[ParentProvisionRecognitionBatchReceiptV1, ...],
    compilations: tuple[ParentProvisionObjectBatchCompilationV1, ...],
) -> tuple[
    dict[str, ParentProvisionRecognitionBatchReceiptV1],
    dict[str, ParentProvisionObjectBatchCompilationV1],
]:
    if not receipts and not compilations:
        return {}, {}
    receipt_by_batch = {item.batch_id: item for item in receipts}
    compilation_by_batch = {item.batch_id: item for item in compilations}
    if (
        len(receipt_by_batch) != len(receipts)
        or len(compilation_by_batch) != len(compilations)
        or set(receipt_by_batch) != set(compilation_by_batch)
    ):
        raise LifecycleProbeError("OBJECT_WINDOW_CHECKPOINT_SCOPE_INVALID")
    target_by_batch = {item.batch_id: item for item in plan.targets}
    for batch_id, receipt in receipt_by_batch.items():
        target = target_by_batch.get(batch_id)
        compilation = compilation_by_batch[batch_id]
        accepted = next(
            (
                item
                for item in receipt.attempts
                if item.attempt_id == receipt.accepted_attempt_id
            ),
            None,
        )
        if (
            target is None
            or target.execution_mode
            is not ParentProvisionObjectExecutionMode.MODEL_TARGET
            or receipt.run_id != compiler.d2_identity.run_id
            or receipt.source_sha256 != compiler.d2_identity.source_sha256
            or receipt.predecessor_artifact_id != compiler.d2_identity.artifact_id
            or receipt.predecessor_artifact_sha256
            != compiler.d2_identity.artifact_sha256
            or receipt.kit_bundle_sha256 != compiler.kit_identity.bundle_sha256
            or receipt.plan_id != plan.plan_id
            or receipt.plan_sha256 != plan.plan_sha256
            or receipt.batch_order != target.batch_order
            or receipt.target_scope_sha256 != target.target_scope_sha256
            or receipt.status
            not in {
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
            }
            or compilation.plan_id != plan.plan_id
            or compilation.plan_sha256 != plan.plan_sha256
            or compilation.batch_order != target.batch_order
            or compilation.logical_object_id != target.logical_object_id
            or compilation.target_scope_sha256 != target.target_scope_sha256
            or accepted is None
            or accepted.batch_compilation_result_sha256 != compilation.result_sha256
        ):
            raise LifecycleProbeError("OBJECT_WINDOW_CHECKPOINT_LINEAGE_INVALID")
        try:
            replayed = compiler.compile_object(
                plan=plan,
                batch_id=batch_id,
                candidate=compilation.model_candidate,
            )
        except (ValidationError, ValueError) as exc:
            raise LifecycleProbeError("OBJECT_WINDOW_CHECKPOINT_REPLAY_FAILED") from exc
        if replayed != compilation:
            raise LifecycleProbeError("OBJECT_WINDOW_CHECKPOINT_COMPILATION_DRIFT")
    ordered_ids = tuple(
        target.batch_id
        for target in plan.targets
        if target.batch_id in receipt_by_batch
    )
    if (
        tuple(item.batch_id for item in receipts) != ordered_ids
        or tuple(item.batch_id for item in compilations) != ordered_ids
    ):
        raise LifecycleProbeError("OBJECT_WINDOW_CHECKPOINT_ORDER_INVALID")
    return receipt_by_batch, compilation_by_batch


def _build_object_window_invocation(
    *,
    activation: Any,
    output_schema: Mapping[str, Any],
    plan: ParentProvisionObjectBatchPlanV1,
    targets: tuple[ParentProvisionObjectBatchTargetV1, ...],
    task_binding_sha256: str,
    attempt_ordinal: int,
    previous_by_batch: Mapping[str, ParentProvisionRecognitionAttemptReceiptV1],
) -> CanonicalStageInvocation:
    invocation = build_canonical_stage_invocation(
        activation,
        task_binding_sha256=task_binding_sha256,
        turn_output_schema_id=(PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION),
        turn_output_schema=output_schema,
        expected_final_artifact_schema_id=PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
        expected_validator_id=PARENT_PROVISION_VALIDATOR_ID,
        expected_validator_version=PARENT_PROVISION_VALIDATOR_VERSION,
    )
    target_payload = [
        {
            "batch_id": target.batch_id,
            "batch_order": target.batch_order,
            "logical_object_id": target.logical_object_id,
            "target_segment_ordinals": list(target.segment_ordinals),
            "target_scope_sha256": target.target_scope_sha256,
            "target_evidence_sha256": target.target_evidence_sha256,
        }
        for target in targets
    ]
    previous_failures = [
        {
            "batch_id": target.batch_id,
            **(_previous_failure_feedback(previous_by_batch[target.batch_id]) or {}),
        }
        for target in targets
        if target.batch_id in previous_by_batch
    ]
    binding = (
        "\n\n"
        f"{PARENT_PROVISION_OBJECT_WINDOW_PROMPT_BINDING_VERSION}\n"
        f"execution_mode={PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE}\n"
        f"object_batch_plan_id={plan.plan_id}\n"
        f"object_batch_plan_sha256={plan.plan_sha256}\n"
        f"attempt_ordinal={attempt_ordinal}\n"
        "window_targets="
        + json.dumps(
            target_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\nprevious_window_failures="
        + (
            json.dumps(
                previous_failures,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if previous_failures
            else "NONE"
        )
        + "\nEvery listed batch is immutable and must appear exactly once in the "
        "same order in the window output. Read the whole evidence workspace for "
        "context, but select source spans only from each batch's listed global "
        "segment ordinals. Keep outputs object-local; request adjacent "
        "reconciliation only when a correct parent crosses that object edge. "
        "Return a working judgment for every closed object; UNRESOLVED is not a "
        "publishable product decision."
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


def _window_attempt_task_binding_sha256(
    *,
    base_task_binding_sha256: str,
    plan: ParentProvisionObjectBatchPlanV1,
    targets: tuple[ParentProvisionObjectBatchTargetV1, ...],
    attempt_ordinal: int,
    previous_by_batch: Mapping[str, ParentProvisionRecognitionAttemptReceiptV1],
) -> str:
    return canonical_json_sha256(
        {
            "binding_version": PARENT_PROVISION_OBJECT_WINDOW_PROMPT_BINDING_VERSION,
            "base_task_binding_sha256": base_task_binding_sha256,
            "execution_mode": PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "targets": [
                {
                    "batch_id": target.batch_id,
                    "batch_order": target.batch_order,
                    "logical_object_id": target.logical_object_id,
                    "segment_ordinals": target.segment_ordinals,
                    "target_scope_sha256": target.target_scope_sha256,
                    "target_evidence_sha256": target.target_evidence_sha256,
                    "previous_failure_feedback": _previous_failure_feedback(
                        previous_by_batch.get(target.batch_id)
                    ),
                }
                for target in targets
            ],
            "attempt_ordinal": attempt_ordinal,
        }
    )


def _window_target_task_binding_sha256(
    *,
    window_task_binding: str,
    target: ParentProvisionObjectBatchTargetV1,
) -> str:
    return canonical_json_sha256(
        {
            "window_task_binding_sha256": window_task_binding,
            "batch_id": target.batch_id,
            "batch_order": target.batch_order,
            "target_scope_sha256": target.target_scope_sha256,
        }
    )


def _compilation_has_semantic_unresolved(
    compilation: ParentProvisionObjectBatchCompilationV1,
) -> bool:
    decision = compilation.compiled_decision
    return bool(
        decision is not None
        and (
            decision.unresolved_logical_object_ids
            or decision.diagnostic_codes
            or any(
                entry.requirementness is Requirementness.UNRESOLVED
                for entry in decision.entries
            )
        )
    )


def _build_object_invocation(
    *,
    activation: Any,
    output_schema: Mapping[str, Any],
    plan: ParentProvisionObjectBatchPlanV1,
    target: ParentProvisionObjectBatchTargetV1,
    task_binding_sha256: str,
    attempt_ordinal: int,
    previous: ParentProvisionRecognitionAttemptReceiptV1 | None,
) -> CanonicalStageInvocation:
    invocation = build_canonical_stage_invocation(
        activation,
        task_binding_sha256=task_binding_sha256,
        turn_output_schema_id=PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION,
        turn_output_schema=output_schema,
        expected_final_artifact_schema_id=PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
        expected_validator_id=PARENT_PROVISION_VALIDATOR_ID,
        expected_validator_version=PARENT_PROVISION_VALIDATOR_VERSION,
    )
    previous_reason = previous.reason_codes[0] if previous is not None else "NONE"
    previous_attempt_id = previous.attempt_id if previous is not None else "NONE"
    previous_receipt_sha256 = (
        previous.receipt_sha256 if previous is not None else "NONE"
    )
    previous_feedback = _previous_failure_feedback(previous)
    previous_feedback_text = (
        json.dumps(
            previous_feedback,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if previous_feedback is not None
        else "NONE"
    )
    ordinal_text = ",".join(str(item) for item in target.segment_ordinals)
    binding = (
        "\n\n"
        f"{PARENT_PROVISION_OBJECT_PROMPT_BINDING_VERSION}\n"
        f"execution_mode={PARENT_PROVISION_OBJECT_EXECUTION_MODE}\n"
        f"object_batch_plan_id={plan.plan_id}\n"
        f"object_batch_plan_sha256={plan.plan_sha256}\n"
        f"object_batch_id={target.batch_id}\n"
        f"object_batch_order={target.batch_order}\n"
        f"logical_object_id={target.logical_object_id}\n"
        f"target_segment_ordinals={ordinal_text}\n"
        f"target_scope_sha256={target.target_scope_sha256}\n"
        f"target_evidence_sha256={target.target_evidence_sha256}\n"
        f"attempt_ordinal={attempt_ordinal}\n"
        f"previous_failure_code={previous_reason}\n"
        f"previous_attempt_id={previous_attempt_id}\n"
        f"previous_attempt_receipt_sha256={previous_receipt_sha256}\n"
        f"previous_failure_feedback={previous_feedback_text}\n"
        "The plan, batch, logical object, segment ordinals, target scope, and "
        "target evidence above are immutable. Read the whole evidence workspace "
        "for context, but select source spans only from the listed global segment "
        "ordinals. Treat previous failure feedback only as a correction signal, "
        "never as source evidence or boundary authority."
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
    plan: ParentProvisionObjectBatchPlanV1,
    target: ParentProvisionObjectBatchTargetV1,
    attempt_ordinal: int,
    previous: ParentProvisionRecognitionAttemptReceiptV1 | None,
) -> str:
    return canonical_json_sha256(
        {
            "binding_version": PARENT_PROVISION_OBJECT_PROMPT_BINDING_VERSION,
            "base_task_binding_sha256": base_task_binding_sha256,
            "execution_mode": PARENT_PROVISION_OBJECT_EXECUTION_MODE,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "batch_id": target.batch_id,
            "batch_order": target.batch_order,
            "logical_object_id": target.logical_object_id,
            "segment_ordinals": target.segment_ordinals,
            "target_scope_sha256": target.target_scope_sha256,
            "target_evidence_sha256": target.target_evidence_sha256,
            "attempt_ordinal": attempt_ordinal,
            "previous_failure_feedback": _previous_failure_feedback(previous),
        }
    )


def _completed_transport_attempt_receipt(
    *,
    activation: Any,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    plan: ParentProvisionObjectBatchPlanV1,
    target: ParentProvisionObjectBatchTargetV1,
    attempt_ordinal: int,
    task_binding_sha256: str,
    transport_result: CanonicalStageTransportResult,
    outcome: ParentProvisionRecognitionAttemptOutcome,
    candidate_sha256: str | None,
    batch_compilation_result_sha256: str | None,
    semantic_unresolved_scope_count: int,
    semantic_unresolved_entry_count: int,
    reason_codes: tuple[str, ...],
) -> ParentProvisionRecognitionAttemptReceiptV1:
    return build_parent_provision_recognition_attempt_receipt(
        **_attempt_common_values(
            activation=activation,
            compiler=compiler,
            plan=plan,
            target=target,
            attempt_ordinal=attempt_ordinal,
            task_binding_sha256=task_binding_sha256,
        ),
        outcome=outcome,
        transport_result_sha256=_transport_result_sha256(transport_result),
        response_sha256=transport_result.response_sha256,
        tool_receipt_sha256=transport_result.tool_receipt_sha256,
        runtime_thread_id_sha256=_text_sha256(transport_result.runtime_thread_id),
        runtime_turn_id_sha256=_text_sha256(transport_result.runtime_turn_id),
        candidate_sha256=candidate_sha256,
        batch_compilation_result_sha256=batch_compilation_result_sha256,
        semantic_unresolved_scope_count=semantic_unresolved_scope_count,
        semantic_unresolved_entry_count=semantic_unresolved_entry_count,
        cleanup_status=ParentProvisionRecognitionCleanupStatus.SUCCEEDED,
        workspace_unchanged=True,
        reason_codes=reason_codes,
    )


def _transport_failure_receipt(
    *,
    activation: Any,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    plan: ParentProvisionObjectBatchPlanV1,
    target: ParentProvisionObjectBatchTargetV1,
    attempt_ordinal: int,
    task_binding_sha256: str,
    reason_code: str,
) -> ParentProvisionRecognitionAttemptReceiptV1:
    reason = (
        reason_code
        if _MACHINE_CODE_PATTERN.fullmatch(reason_code)
        else "CANONICAL_STAGE_TRANSPORT_FAILED"
    )
    outcome, cleanup, workspace_unchanged = _classify_transport_failure(reason)
    return build_parent_provision_recognition_attempt_receipt(
        **_attempt_common_values(
            activation=activation,
            compiler=compiler,
            plan=plan,
            target=target,
            attempt_ordinal=attempt_ordinal,
            task_binding_sha256=task_binding_sha256,
        ),
        outcome=outcome,
        transport_result_sha256=None,
        response_sha256=None,
        tool_receipt_sha256=None,
        runtime_thread_id_sha256=None,
        runtime_turn_id_sha256=None,
        candidate_sha256=None,
        batch_compilation_result_sha256=None,
        semantic_unresolved_scope_count=0,
        semantic_unresolved_entry_count=0,
        cleanup_status=cleanup,
        workspace_unchanged=workspace_unchanged,
        reason_codes=(reason,),
    )


def _attempt_common_values(
    *,
    activation: Any,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    plan: ParentProvisionObjectBatchPlanV1,
    target: ParentProvisionObjectBatchTargetV1,
    attempt_ordinal: int,
    task_binding_sha256: str,
) -> dict[str, Any]:
    stage_binding = _stage_binding(activation)
    return {
        "run_id": compiler.d2_identity.run_id,
        "source_sha256": compiler.d2_identity.source_sha256,
        "predecessor_artifact_id": compiler.d2_identity.artifact_id,
        "predecessor_artifact_sha256": compiler.d2_identity.artifact_sha256,
        "kit_bundle_sha256": compiler.kit_identity.bundle_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "batch_id": target.batch_id,
        "batch_order": target.batch_order,
        "target_scope_sha256": target.target_scope_sha256,
        "attempt_ordinal": attempt_ordinal,
        "task_binding_sha256": task_binding_sha256,
        "stage_session_attestation_sha256": canonical_json_sha256(
            activation.session_attestation.model_dump(mode="json")
        ),
        "stage_context_sha256": activation.context_sha256,
        "skill_sha256": stage_binding.skill.sha256,
        "runner_output_schema_sha256": activation.runner_output_schema_sha256,
        "model_id": activation.session_attestation.model_id,
        "reasoning_effort": activation.session_attestation.reasoning_effort,
    }


def _build_batch_receipt(
    *,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    plan: ParentProvisionObjectBatchPlanV1,
    target: ParentProvisionObjectBatchTargetV1,
    attempts: tuple[ParentProvisionRecognitionAttemptReceiptV1, ...],
    status: ParentProvisionRecognitionBatchStatus,
    reason_codes: tuple[str, ...],
    accepted_attempt: ParentProvisionRecognitionAttemptReceiptV1 | None = None,
    semantic_unresolved_scope_count: int = 0,
    semantic_unresolved_entry_count: int = 0,
) -> ParentProvisionRecognitionBatchReceiptV1:
    return build_parent_provision_recognition_batch_receipt(
        run_id=compiler.d2_identity.run_id,
        source_sha256=compiler.d2_identity.source_sha256,
        predecessor_artifact_id=compiler.d2_identity.artifact_id,
        predecessor_artifact_sha256=compiler.d2_identity.artifact_sha256,
        kit_bundle_sha256=compiler.kit_identity.bundle_sha256,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        batch_id=target.batch_id,
        batch_order=target.batch_order,
        target_scope_sha256=target.target_scope_sha256,
        attempts=attempts,
        accepted_attempt_id=(
            accepted_attempt.attempt_id if accepted_attempt is not None else None
        ),
        accepted_attempt_receipt_sha256=(
            accepted_attempt.receipt_sha256 if accepted_attempt is not None else None
        ),
        status=status,
        semantic_unresolved_scope_count=semantic_unresolved_scope_count,
        semantic_unresolved_entry_count=semantic_unresolved_entry_count,
        reason_codes=tuple(sorted(set(reason_codes))),
    )


def _terminal_batch_receipt(
    *,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    plan: ParentProvisionObjectBatchPlanV1,
    target: ParentProvisionObjectBatchTargetV1,
    attempts: tuple[ParentProvisionRecognitionAttemptReceiptV1, ...],
) -> ParentProvisionRecognitionBatchReceiptV1:
    terminal = attempts[-1]
    status = {
        ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED: (
            ParentProvisionRecognitionBatchStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED: (
            ParentProvisionRecognitionBatchStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.CANCELLED: (
            ParentProvisionRecognitionBatchStatus.CANCELLED
        ),
        ParentProvisionRecognitionAttemptOutcome.BLOCKED: (
            ParentProvisionRecognitionBatchStatus.BLOCKED
        ),
        ParentProvisionRecognitionAttemptOutcome.INDETERMINATE: (
            ParentProvisionRecognitionBatchStatus.INDETERMINATE
        ),
    }[terminal.outcome]
    reasons = set(terminal.reason_codes)
    if status is ParentProvisionRecognitionBatchStatus.FAILED:
        reasons.add("RETRY_BUDGET_EXHAUSTED")
    return _build_batch_receipt(
        compiler=compiler,
        plan=plan,
        target=target,
        attempts=attempts,
        status=status,
        reason_codes=tuple(sorted(reasons)),
    )


def _build_aggregate_receipt(
    *,
    activation: Any,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
    plan: ParentProvisionObjectBatchPlanV1,
    batches: tuple[ParentProvisionRecognitionBatchReceiptV1, ...],
    object_aggregate: ParentProvisionObjectBatchAggregateV1 | None,
) -> ParentProvisionRecognitionAggregateExecutionReceiptV2:
    policy_sha256 = activation.session_attestation.d3_policy_closure_sha256
    if policy_sha256 is None:
        raise LifecycleProbeError("CANONICAL_D3_POLICY_CLOSURE_MISSING")
    if object_aggregate is not None:
        semantic_unresolved_scopes = sum(
            item.semantic_unresolved_scope_count for item in batches
        )
        semantic_unresolved_entries = sum(
            item.semantic_unresolved_entry_count for item in batches
        )
        predecessor_partial = (
            compiler.d2_identity.status is ParentProvisionStatus.PARTIAL
        )
        status = (
            ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC
            if object_aggregate.compiled_decision.status
            is ParentProvisionStatus.PARTIAL
            else ParentProvisionRecognitionAggregateStatus.COMPLETE
        )
        evidence_refs = parent_provision_evidence_refs(
            declared_logical_object_ids=(
                object_aggregate.compiled_decision.declared_logical_object_ids
            ),
            unresolved_logical_object_ids=(
                object_aggregate.compiled_decision.unresolved_logical_object_ids
            ),
            entries=object_aggregate.compiled_decision.entries,
        )
        final_values = {
            "object_batch_aggregate_sha256": object_aggregate.aggregate_sha256,
            "decision_candidate_sha256": object_aggregate.global_candidate_sha256,
            "parent_provision_decision_sha256": (
                object_aggregate.compiled_decision.decision_sha256
            ),
            "evidence_refs_sha256": canonical_json_sha256(evidence_refs),
            "reason_codes": (),
        }
    else:
        semantic_unresolved_scopes = 0
        semantic_unresolved_entries = 0
        predecessor_partial = (
            compiler.d2_identity.status is ParentProvisionStatus.PARTIAL
        )
        nonaccepted = tuple(
            item
            for item in batches
            if item.status
            not in {
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
            }
        )
        statuses = {item.status for item in nonaccepted}
        if ParentProvisionRecognitionBatchStatus.INDETERMINATE in statuses:
            status = ParentProvisionRecognitionAggregateStatus.INDETERMINATE
        elif statuses & {
            ParentProvisionRecognitionBatchStatus.BLOCKED,
            ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
        }:
            status = ParentProvisionRecognitionAggregateStatus.BLOCKED
        elif ParentProvisionRecognitionBatchStatus.CANCELLED in statuses:
            status = ParentProvisionRecognitionAggregateStatus.CANCELLED
        else:
            status = ParentProvisionRecognitionAggregateStatus.FAILED
        final_values = {
            "object_batch_aggregate_sha256": None,
            "decision_candidate_sha256": None,
            "parent_provision_decision_sha256": None,
            "evidence_refs_sha256": None,
            "reason_codes": tuple(
                sorted({reason for item in nonaccepted for reason in item.reason_codes})
            ),
        }
    return build_parent_provision_recognition_aggregate_execution_receipt(
        run_id=compiler.d2_identity.run_id,
        source_sha256=compiler.d2_identity.source_sha256,
        predecessor_artifact_id=compiler.d2_identity.artifact_id,
        predecessor_artifact_sha256=compiler.d2_identity.artifact_sha256,
        kit_bundle_sha256=compiler.kit_identity.bundle_sha256,
        policy_closure_sha256=policy_sha256,
        runner_profile_sha256=(activation.session_attestation.runner_profile_sha256),
        model_id=activation.session_attestation.model_id,
        reasoning_effort=activation.session_attestation.reasoning_effort,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        planned_batch_count=len(plan.targets),
        batches=batches,
        status=status,
        semantic_unresolved_scope_count=semantic_unresolved_scopes,
        semantic_unresolved_entry_count=semantic_unresolved_entries,
        predecessor_partial=predecessor_partial,
        **final_values,
    )


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
        "OBJECT_BATCH_SCHEMA_ACTIVATION_MISMATCH",
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


def _load_selected_output_schema(activation: Any) -> dict[str, Any]:
    try:
        payload = activation.runner_output_schema_path.read_bytes()
        schema = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("OBJECT_BATCH_RUNNER_SCHEMA_INVALID") from exc
    if (
        not isinstance(schema, Mapping)
        or hashlib.sha256(payload).hexdigest() != activation.runner_output_schema_sha256
    ):
        raise LifecycleProbeError("OBJECT_BATCH_RUNNER_SCHEMA_INVALID")
    return dict(schema)


def _require_plan_lineage(
    plan: ParentProvisionObjectBatchPlanV1,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
) -> None:
    if (
        plan.run_id != compiler.d2_identity.run_id
        or plan.source_sha256 != compiler.d2_identity.source_sha256
        or plan.kit_bundle_sha256 != compiler.kit_identity.bundle_sha256
        or plan.predecessor_artifact_id != compiler.d2_identity.artifact_id
        or plan.predecessor_artifact_sha256 != compiler.d2_identity.artifact_sha256
        or plan.predecessor_validator_receipt_sha256
        != compiler.d2_identity.validator_receipt_sha256
        or plan.source_scope_sha256 != compiler.d2_identity.source_scope_sha256
        or plan.stage_input_bundle_sha256 != compiler.stage_input_bundle_sha256
        or plan.stage_input_manifest_sha256 != compiler.stage_input_manifest_sha256
        or plan.runtime_adapter_sha256 != compiler.runtime_adapter_sha256
    ):
        raise LifecycleProbeError("OBJECT_BATCH_PLAN_LINEAGE_INVALID")


def _require_activation_lineage(
    activation: Any,
    compiler: CanonicalParentProvisionObjectBatchCompiler,
) -> None:
    evidence = activation.session_attestation.document_evidence
    predecessor = activation.predecessor
    legacy_stage_input = getattr(activation, "d3_stage_input_attestation", None)
    region_stage_input = getattr(
        activation,
        "d3_region_stage_input_attestation",
        None,
    )
    if (
        activation.session_attestation.kit_identity != compiler.kit_identity
        or evidence is None
        or evidence.run_id != compiler.d2_identity.run_id
        or evidence.source_sha256 != compiler.d2_identity.source_sha256
        or evidence.source_ir_id != compiler.d2_identity.source_ir_id
        or evidence.source_ir_canonical_sha256
        != compiler.d2_identity.source_ir_canonical_sha256
        or predecessor is None
        or predecessor.artifact_id != compiler.d2_identity.artifact_id
        or predecessor.artifact_sha256 != compiler.d2_identity.artifact_sha256
        or predecessor.validator_receipt_sha256
        != compiler.d2_identity.validator_receipt_sha256
    ):
        raise LifecycleProbeError("OBJECT_BATCH_STAGE_LINEAGE_INVALID")
    if legacy_stage_input is not None:
        if (
            region_stage_input is not None
            or compiler.stage_input_manifest_sha256 is not None
            or compiler.runtime_adapter_sha256 is not None
            or legacy_stage_input.bundle_sha256 != compiler.stage_input_bundle_sha256
        ):
            raise LifecycleProbeError("OBJECT_BATCH_STAGE_LINEAGE_INVALID")
        return
    if (
        region_stage_input is None
        or compiler.stage_input_manifest_sha256 is None
        or compiler.runtime_adapter_sha256 is None
        or region_stage_input.bundle_sha256 != compiler.stage_input_bundle_sha256
        or region_stage_input.stage_input_manifest.manifest_sha256
        != compiler.stage_input_manifest_sha256
        or region_stage_input.stage_input_manifest.lineage.source_ir_id
        != compiler.d2_identity.source_ir_id
        or region_stage_input.stage_input_manifest.lineage.source_ir_canonical_sha256
        != compiler.d2_identity.source_ir_canonical_sha256
    ):
        raise LifecycleProbeError("OBJECT_BATCH_STAGE_LINEAGE_INVALID")


def _stage_binding(activation: Any) -> Any:
    try:
        return next(
            item
            for item in activation.session_attestation.stages
            if item.stage is activation.stage
        )
    except StopIteration as exc:
        raise LifecycleProbeError("OBJECT_BATCH_STAGE_BINDING_MISSING") from exc


def _machine_reason_code(exc: BaseException, *, default: str) -> str:
    value = str(exc).strip()
    if _MACHINE_CODE_PATTERN.fullmatch(value):
        return value
    lowered = value.lower()
    for message, reason_code in _HOST_COMPILER_REASON_BY_MESSAGE.items():
        if message.lower() in lowered:
            return reason_code
    embedded = re.search(r"\b[A-Z][A-Z0-9_]{2,119}\b", value)
    return embedded.group(0) if embedded is not None else default


def _safe_compiler_error_summary(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        projected = [
            {
                "type": str(item.get("type") or ""),
                "loc": [str(value) for value in item.get("loc", ())],
                "msg": str(item.get("msg") or ""),
            }
            for item in exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        ]
        return json.dumps(
            projected,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )[:1000]
    return re.sub(r"\s+", " ", str(exc).strip())[:500]


def _previous_failure_feedback(
    previous: ParentProvisionRecognitionAttemptReceiptV1 | None,
) -> dict[str, str] | None:
    if previous is None:
        return None
    feedback = {
        "attempt_id": previous.attempt_id,
        "attempt_receipt_sha256": previous.receipt_sha256,
        "failure_code": previous.reason_codes[0],
    }
    correction_hint = _CORRECTION_HINT_BY_CODE.get(previous.reason_codes[0])
    if correction_hint:
        feedback["correction_hint"] = correction_hint
    return feedback


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


__all__ = [
    "PARENT_PROVISION_OBJECT_EXECUTION_MODE",
    "PARENT_PROVISION_OBJECT_PROMPT_BINDING_VERSION",
    "PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE",
    "PARENT_PROVISION_OBJECT_WINDOW_PROMPT_BINDING_VERSION",
    "ParentProvisionObjectStageDriver",
    "ParentProvisionObjectStageRunResult",
]
