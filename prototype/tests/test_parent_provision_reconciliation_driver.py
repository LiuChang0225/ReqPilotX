from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalStageFileDigestV1,
    CanonicalStageRunnerOutputSchemaVariantV1,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
)
from app.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.adapters.agents.codex_app_server.parent_provision_object_driver import (
    PARENT_PROVISION_OBJECT_EXECUTION_MODE,
    ParentProvisionObjectStageDriver,
)
from app.adapters.agents.codex_app_server.parent_provision_reconciliation_driver import (
    PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
    ParentProvisionObjectReconciliationStageDriver,
)
from app.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
    ParentProvisionProposalV3,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionAttemptOutcome,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION,
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectScopeStatus,
)
from app.application.agent_workflow.parent_provision_object_reconciliation import (
    PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION,
    ParentProvisionObjectReconciliationCandidateV1,
    ParentProvisionObjectReconciliationStatus,
    build_parent_provision_object_reconciliation_plan,
)
from app.application.agent_workflow.parent_provision_reconciliation_receipts import (
    ParentProvisionReconciliationAggregateReceiptV1,
    ParentProvisionReconciliationAggregateStatus,
    ParentProvisionReconciliationGroupStatus,
    canonical_json_sha256,
    parent_provision_reconciliation_failure_feedback,
)
from app.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)

if __package__:
    from .test_canonical_stage_driver import _ready_activation
    from .test_parent_provision_object_batches import _closed_candidate
    from .test_parent_provision_object_reconciliation import (
        _Fixture,
        _entry_for,
        _fixture,
        _resolved_candidate,
    )
else:
    from test_canonical_stage_driver import _ready_activation
    from test_parent_provision_object_batches import _closed_candidate
    from test_parent_provision_object_reconciliation import (
        _Fixture,
        _entry_for,
        _fixture,
        _resolved_candidate,
    )


TASK_BINDING_SHA256 = "d" * 64


class ParentProvisionObjectReconciliationStageDriverTests(unittest.TestCase):
    def test_validated_group_checkpoint_resumes_without_an_agent_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={1},
            )
            plan = _reconciliation_plan(initial, compiler)
            saved = []
            first_transport = _QueueTransport(
                (_candidate_for_group(fixture, plan.groups[0]),)
            )
            first = ParentProvisionObjectReconciliationStageDriver(
                transport=first_transport,
                object_compiler=compiler,
                group_checkpoint_sink=(lambda *values: saved.append(values)),
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )
            _, _, receipt, compilation = saved[0]
            second_transport = _QueueTransport(())

            second = ParentProvisionObjectReconciliationStageDriver(
                transport=second_transport,
                object_compiler=compiler,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
                resumed_group_receipts=(receipt,),
                resumed_group_compilations=(compilation,),
            )

        self.assertEqual(first_transport.calls, 1)
        self.assertEqual(second_transport.calls, 0)
        self.assertEqual(
            first.reconciliation_aggregate, second.reconciliation_aggregate
        )
        self.assertEqual(first.execution_receipt, second.execution_receipt)

    def test_durable_object_receipts_can_reconcile_in_a_fresh_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={1},
            )
            plan = _reconciliation_plan(initial, compiler)
            fresh_session = reconciliation_activation.session_attestation.model_copy(
                update={"session_id": "canonical-document-session-" + "f" * 32}
            )
            digest = canonical_json_sha256(
                fresh_session.model_dump(mode="json", exclude={"attestation_id"})
            )
            fresh_session = fresh_session.model_copy(
                update={
                    "attestation_id": f"canonical-session-attestation-{digest[:32]}"
                }
            )
            fresh_activation = replace(
                reconciliation_activation,
                session_attestation=fresh_session,
            )

            result = ParentProvisionObjectReconciliationStageDriver(
                transport=_QueueTransport(
                    (_candidate_for_group(fixture, plan.groups[0]),)
                ),
                object_compiler=compiler,
            ).run(
                _Session(fresh_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )

        self.assertIsNotNone(result.reconciliation_aggregate)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionReconciliationAggregateStatus.COMPLETE,
        )

    def test_two_groups_complete_in_reverse_order_and_bind_professional_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={0, 4},
            )
            plan = _reconciliation_plan(initial, compiler)
            self.assertEqual(len(plan.groups), 2)
            reverse_groups = tuple(reversed(plan.groups))
            transport = _QueueTransport(
                tuple(_candidate_for_group(fixture, group) for group in reverse_groups)
            )

            result = ParentProvisionObjectReconciliationStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
                group_execution_order=tuple(item.group_id for item in reverse_groups),
            )

        self.assertEqual(transport.calls, 2)
        self.assertEqual(
            tuple(item.group_id for item in result.group_receipts_in_completion_order),
            tuple(item.group_id for item in reverse_groups),
        )
        self.assertIsNotNone(result.reconciliation_aggregate)
        assert result.reconciliation_aggregate is not None
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionReconciliationAggregateStatus.COMPLETE,
        )
        self.assertEqual(
            result.execution_receipt.reconciliation_aggregate_sha256,
            result.reconciliation_aggregate.aggregate_sha256,
        )
        self.assertEqual(
            result.execution_receipt.reconciliation_execution_receipts_sha256,
            result.execution_receipt.receipt_sha256,
        )

    def test_first_rejection_retries_with_complete_feedback_and_same_group(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={1},
            )
            plan = _reconciliation_plan(initial, compiler)
            group = plan.groups[0]
            transport = _QueueTransport(({}, _candidate_for_group(fixture, group)))
            result = ParentProvisionObjectReconciliationStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )

        receipt = result.group_receipts_in_completion_order[0]
        self.assertEqual(
            tuple(item.outcome for item in receipt.attempts),
            (
                ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED,
                ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
            ),
        )
        first, second = receipt.attempts
        feedback = parent_provision_reconciliation_failure_feedback(first)
        feedback_text = json.dumps(
            feedback,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        retry_prompt = _prompt(transport.invocations[1])
        self.assertIn(f"previous_failure_feedback={feedback_text}", retry_prompt)
        self.assertEqual(second.previous_attempt_id, first.attempt_id)
        self.assertEqual(second.previous_attempt_receipt_sha256, first.receipt_sha256)
        for field in (
            "reconciliation_plan_sha256",
            "reconciliation_group_id",
            "reconciliation_group_scope_sha256",
            "replacement_batch_ids",
            "replacement_logical_object_ids",
            "allowed_global_segment_ordinals",
        ):
            self.assertEqual(
                _prompt_value(_prompt(transport.invocations[0]), field),
                _prompt_value(retry_prompt, field),
            )

    def test_failed_group_does_not_stop_an_independent_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={0, 4},
            )
            plan = _reconciliation_plan(initial, compiler)
            transport = _QueueTransport(
                ({}, _candidate_for_group(fixture, plan.groups[1]))
            )
            result = ParentProvisionObjectReconciliationStageDriver(
                transport=transport,
                object_compiler=compiler,
                max_attempts_per_group=1,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 2)
        self.assertEqual(
            tuple(item.status for item in result.group_receipts_in_completion_order),
            (
                ParentProvisionReconciliationGroupStatus.FAILED,
                ParentProvisionReconciliationGroupStatus.ACCEPTED,
            ),
        )
        self.assertIsNone(result.reconciliation_aggregate)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionReconciliationAggregateStatus.FAILED,
        )
        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 0)
        self.assertEqual(result.execution_receipt.semantic_unresolved_entry_count, 0)
        self.assertFalse(result.execution_receipt.predecessor_partial)

    def test_cancel_stops_all_remaining_groups(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={0, 4},
            )
            transport = _QueueTransport(
                (LifecycleProbeError("CANONICAL_STAGE_CANCELLED"),)
            )
            result = ParentProvisionObjectReconciliationStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(result.group_receipts_in_completion_order), 1)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionReconciliationAggregateStatus.CANCELLED,
        )
        self.assertIsNone(result.reconciliation_aggregate)

    def test_host_valid_semantic_unresolved_is_not_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={1},
            )
            plan = _reconciliation_plan(initial, compiler)
            group = plan.groups[0]
            unresolved_object_id = group.replacements[1].logical_object_id
            candidate = ParentProvisionObjectReconciliationCandidateV1(
                status=ParentProvisionObjectReconciliationStatus.UNRESOLVED,
                entries=(
                    _entry_for(fixture.segments[0], ordinal=0),
                    _entry_for(fixture.segments[2], ordinal=2),
                ),
                unresolved_logical_object_ids=(unresolved_object_id,),
                diagnostic_codes=("SOURCE_BOUNDARY_AMBIGUOUS",),
            ).model_dump(mode="json")
            result = ParentProvisionObjectReconciliationStageDriver(
                transport=_QueueTransport((candidate,)),
                object_compiler=compiler,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )

        self.assertEqual(
            result.group_receipts_in_completion_order[0].status,
            ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
        )
        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 1)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionReconciliationAggregateStatus.PARTIAL_SEMANTIC,
        )

    def test_cross_group_ordinal_is_host_rejected_then_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={1},
            )
            plan = _reconciliation_plan(initial, compiler)
            group = plan.groups[0]
            escaping = _resolved_candidate(
                _entry_for(fixture.segments[4], ordinal=4)
            ).model_dump(mode="json")
            transport = _QueueTransport(
                (escaping, _candidate_for_group(fixture, group))
            )
            result = ParentProvisionObjectReconciliationStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )

        first = result.group_receipts_in_completion_order[0].attempts[0]
        self.assertEqual(
            first.reason_codes,
            ("OBJECT_RECONCILIATION_SPAN_ESCAPES_GROUP",),
        )
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionReconciliationAggregateStatus.COMPLETE,
        )

    def test_tampered_schema_and_receipt_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            object_activation, reconciliation_activation = _dual_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={1},
            )
            tampered_activation = replace(
                reconciliation_activation,
                runner_output_schema_sha256="0" * 64,
            )
            transport = _QueueTransport(())
            with self.assertRaises(LifecycleProbeError):
                ParentProvisionObjectReconciliationStageDriver(
                    transport=transport,
                    object_compiler=compiler,
                ).run(
                    _Session(tampered_activation),
                    initial_result=initial,
                    task_binding_sha256=TASK_BINDING_SHA256,
                )
            self.assertEqual(transport.calls, 0)

            plan = _reconciliation_plan(initial, compiler)
            result = ParentProvisionObjectReconciliationStageDriver(
                transport=_QueueTransport(
                    (_candidate_for_group(fixture, plan.groups[0]),)
                ),
                object_compiler=compiler,
            ).run(
                _Session(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=TASK_BINDING_SHA256,
            )
        payload = result.execution_receipt.model_dump(mode="json")
        payload["receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "receipt SHA drifted"):
            ParentProvisionReconciliationAggregateReceiptV1.model_validate(payload)


class _Session:
    def __init__(self, activation: object) -> None:
        self.activation = activation
        self.execution_modes: list[str | None] = []

    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ):  # type: ignore[no-untyped-def]
        if stage is not AgentExtractionStage.PARENT_PROVISION_RECOGNITION:
            raise AssertionError("unexpected stage")
        self.execution_modes.append(execution_mode)
        return self.activation


class _QueueTransport:
    def __init__(self, outcomes: tuple[object, ...]) -> None:
        self.outcomes = list(outcomes)
        self.invocations = []
        self.calls = 0

    def invoke(self, invocation):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.invocations.append(invocation)
        if not self.outcomes:
            raise AssertionError("unexpected transport call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, dict)
        return CanonicalStageTransportResult(
            stage=invocation.stage,
            structured_output=outcome,
            output_schema_sha256=invocation.turn_output_schema_sha256,
            response_sha256=canonical_json_sha256(outcome),
            tool_receipt_sha256=hashlib.sha256(
                f"tool-{self.calls}".encode("utf-8")
            ).hexdigest(),
            runtime_thread_id=f"thread-{self.calls}",
            runtime_turn_id=f"turn-{self.calls}",
        )


def _dual_activation(state: Path):  # type: ignore[no-untyped-def]
    activation = _ready_activation(
        state,
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        skill_id="reqpilotx-parent-provision-recognition",
        artifact_schema_id=PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
        artifact_schema=ParentProvisionProposalV3.model_json_schema(),
        validator_id=PARENT_PROVISION_VALIDATOR_ID,
        validator_version=PARENT_PROVISION_VALIDATOR_VERSION,
        include_d3_closure=True,
    )
    kit_root = next(iter(activation.skill_paths.values())).parents[3]
    variant_specs = (
        (
            PARENT_PROVISION_OBJECT_EXECUTION_MODE,
            PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION,
            "schemas/parent-provision-object-candidate.v1.schema.json",
            ParentProvisionObjectCandidateV1,
        ),
        (
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
            PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION,
            "schemas/parent-provision-object-reconciliation-candidate.v1.schema.json",
            ParentProvisionObjectReconciliationCandidateV1,
        ),
    )
    variants = []
    paths = {}
    for execution_mode, schema_id, schema_relative, model in variant_specs:
        payload = json.dumps(
            compile_strict_runner_output_schema(model),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        path = kit_root / schema_relative
        path.write_bytes(payload)
        paths[execution_mode] = path.resolve()
        schema_file = CanonicalStageFileDigestV1(
            path=schema_relative,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        draft = CanonicalStageRunnerOutputSchemaVariantV1.model_construct(
            content_free=True,
            execution_mode=execution_mode,
            schema_id=schema_id,
            schema_file=schema_file,
            variant_sha256="0" * 64,
        )
        variants.append(
            CanonicalStageRunnerOutputSchemaVariantV1(
                content_free=True,
                execution_mode=execution_mode,
                schema_id=schema_id,
                schema_file=schema_file,
                variant_sha256=canonical_json_sha256(
                    draft.model_dump(mode="json", exclude={"variant_sha256"})
                ),
            )
        )
    variants_tuple = tuple(variants)
    variants_sha256 = canonical_json_sha256(
        [item.model_dump(mode="json") for item in variants_tuple]
    )
    old_stage = activation.session_attestation.stages[0]
    stage_payload = old_stage.model_dump(mode="json")
    stage_payload.update(
        {
            "runner_output_schema_variants": [
                item.model_dump(mode="json") for item in variants_tuple
            ],
            "runner_output_schema_variants_sha256": variants_sha256,
        }
    )
    new_stage = type(old_stage).model_validate(stage_payload)
    attestation_payload = activation.session_attestation.model_dump(
        mode="json", exclude={"attestation_id"}
    )
    attestation_payload["stages"] = [new_stage.model_dump(mode="json")]
    attestation = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-"
            + canonical_json_sha256(attestation_payload)[:32]
        ),
        **attestation_payload,
    )
    object_variant, reconciliation_variant = variants_tuple
    object_activation = replace(
        activation,
        runner_output_schema_id=object_variant.schema_id,
        runner_output_schema_path=paths[PARENT_PROVISION_OBJECT_EXECUTION_MODE],
        runner_output_schema_sha256=object_variant.schema_file.sha256,
        runner_output_execution_mode=PARENT_PROVISION_OBJECT_EXECUTION_MODE,
        runner_output_schema_variants_sha256=variants_sha256,
        session_attestation=attestation,
    )
    reconciliation_activation = replace(
        activation,
        runner_output_schema_id=reconciliation_variant.schema_id,
        runner_output_schema_path=paths[
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
        ],
        runner_output_schema_sha256=reconciliation_variant.schema_file.sha256,
        runner_output_execution_mode=(
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
        ),
        runner_output_schema_variants_sha256=variants_sha256,
        session_attestation=attestation,
    )
    return object_activation, reconciliation_activation


def _compiler_for_activation(activation):  # type: ignore[no-untyped-def]
    base = _fixture()
    predecessor = activation.predecessor
    stage_input = activation.d3_stage_input_attestation
    evidence = activation.session_attestation.document_evidence
    assert predecessor is not None
    assert stage_input is not None
    assert evidence is not None
    identity = base.identity.model_copy(
        update={
            "run_id": predecessor.run_id,
            "source_sha256": predecessor.source_sha256,
            "artifact_id": predecessor.artifact_id,
            "artifact_sha256": predecessor.artifact_sha256,
            "validator_receipt_sha256": predecessor.validator_receipt_sha256,
            "kit_identity": activation.session_attestation.kit_identity,
            "source_ir_id": evidence.source_ir_id,
            "source_ir_canonical_sha256": evidence.source_ir_canonical_sha256,
        }
    )
    fixture = _Fixture(
        kit=activation.session_attestation.kit_identity,
        identity=identity,
        objects=base.objects,
        segments=base.segments,
        plan=base.plan,
        model_targets=base.model_targets,
    )
    compiler = CanonicalParentProvisionObjectBatchCompiler(
        d2_identity=identity,
        logical_objects=base.objects,
        source_segments=base.segments,
        stage_input_bundle_sha256=stage_input.bundle_sha256,
        kit_identity=activation.session_attestation.kit_identity,
    )
    plan = compiler.build_plan()
    fixture = replace(
        fixture,
        plan=plan,
        model_targets=tuple(
            item for item in plan.targets if item.execution_mode.value == "MODEL_TARGET"
        ),
    )
    return fixture, compiler


def _initial_result(
    *,
    activation,
    compiler,
    fixture: _Fixture,
    marked_model_indexes: set[int],
):  # type: ignore[no-untyped-def]
    outcomes = []
    for index, target in enumerate(fixture.model_targets):
        if index in marked_model_indexes:
            outcomes.append(
                ParentProvisionObjectCandidateV1(
                    scope_status=(
                        ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                    ),
                    diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
                ).model_dump(mode="json")
            )
        else:
            ordinal = target.segment_ordinals[0]
            outcomes.append(
                _closed_candidate(
                    _entry_for(fixture.segments[ordinal], ordinal=ordinal)
                ).model_dump(mode="json")
            )
    result = ParentProvisionObjectStageDriver(
        transport=_QueueTransport(tuple(outcomes)),
        object_compiler=compiler,
    ).run(
        _Session(activation),
        task_binding_sha256="c" * 64,
    )
    if (
        result.execution_receipt.status
        is not ParentProvisionRecognitionAggregateStatus.BLOCKED
    ):
        raise AssertionError("initial object execution must await reconciliation")
    return result


def _reconciliation_plan(initial, compiler):  # type: ignore[no-untyped-def]
    return build_parent_provision_object_reconciliation_plan(
        object_batch_plan=initial.plan,
        expected_object_batch_plan_sha256=initial.plan.plan_sha256,
        local_results=initial.compilations,
        d2_identity=compiler.d2_identity,
        source_segments=compiler.source_segments,
        kit_identity=compiler.kit_identity,
    )


def _candidate_for_group(fixture: _Fixture, group) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return _resolved_candidate(
        *(
            _entry_for(fixture.segments[ordinal], ordinal=ordinal)
            for ordinal in group.global_segment_ordinals
        )
    ).model_dump(mode="json")


def _prompt(invocation) -> str:  # type: ignore[no-untyped-def]
    return next(
        str(item["text"])
        for item in invocation.input_items
        if item.get("type") == "text"
    )


def _prompt_value(prompt: str, field: str) -> str:
    prefix = f"{field}="
    return next(
        line[len(prefix) :] for line in prompt.splitlines() if line.startswith(prefix)
    )


if __name__ == "__main__":
    unittest.main()
