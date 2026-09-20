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
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
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
    PARENT_PROVISION_OBJECT_PROMPT_BINDING_VERSION,
    PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
    ParentProvisionObjectStageDriver,
    _machine_reason_code,
    _require_activation_lineage,
)
from app.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
    ParentProvisionProposalV3,
    ParentProvisionStatus,
    RequirementKind,
    RequirementTechnicality,
    Requirementness,
    parent_provision_evidence_refs,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionBatchStatus,
    canonical_json_sha256,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION,
    PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION,
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectScopeStatus,
    ParentProvisionObjectWindowCandidateV1,
    ParentProvisionObjectWindowItemV1,
    require_parent_provision_object_evidence_recovery,
)
from app.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)

if __package__:
    from .test_canonical_stage_driver import _ready_activation
    from .test_parent_provision_object_batches import (
        _Fixture,
        _closed_candidate,
        _entry_for,
        _fixture,
    )
else:
    from test_canonical_stage_driver import _ready_activation
    from test_parent_provision_object_batches import (
        _Fixture,
        _closed_candidate,
        _entry_for,
        _fixture,
    )


BASE_TASK_BINDING_SHA256 = "e" * 64


class ParentProvisionObjectStageDriverTests(unittest.TestCase):
    def test_host_compiler_messages_become_actionable_machine_codes(self) -> None:
        self.assertEqual(
            _machine_reason_code(
                ValueError(
                    "D3 candidate must disposition every non-whitespace source character"
                ),
                default="OBJECT_WINDOW_HOST_REJECTED",
            ),
            "OBJECT_BATCH_SOURCE_COVERAGE_INCOMPLETE",
        )
        self.assertEqual(
            _machine_reason_code(
                ValueError("wrapper: OBJECT_BATCH_SPAN_INVALID"),
                default="OBJECT_WINDOW_HOST_REJECTED",
            ),
            "OBJECT_BATCH_SPAN_INVALID",
        )

    def test_source_window_preserves_object_local_results_in_one_agent_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw), windowed=True)
            fixture, compiler = _compiler_for_activation(activation)
            plan = compiler.build_plan()
            model_targets = tuple(
                item
                for item in plan.targets
                if item.execution_mode.value == "MODEL_TARGET"
            )
            window = ParentProvisionObjectWindowCandidateV1(
                items=tuple(
                    ParentProvisionObjectWindowItemV1(
                        batch_id=target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1.model_validate(
                            _candidate_for_target(fixture, ordinal=ordinal)
                        ),
                    )
                    for ordinal, target in enumerate(model_targets)
                )
            )
            transport = _QueueTransport((window.model_dump(mode="json"),))
            session = _ObjectSession(activation)

            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
                max_targets_per_turn=8,
            ).run(
                session,
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
            )

        self.assertEqual(session.execution_modes, ("OBJECT_WINDOW",))
        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(result.compilations), 2)
        self.assertIsNotNone(result.object_batch_aggregate)
        self.assertEqual(
            tuple(item.status for item in result.batch_receipts),
            (
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
            ),
        )
        first_attempt = result.batch_receipts[1].attempts[0]
        second_attempt = result.batch_receipts[2].attempts[0]
        self.assertNotEqual(first_attempt.attempt_id, second_attempt.attempt_id)
        self.assertEqual(
            first_attempt.runtime_thread_id_sha256,
            second_attempt.runtime_thread_id_sha256,
        )
        prompt = _prompt(transport.invocations[0])
        self.assertIn("execution_mode=OBJECT_WINDOW", prompt)
        self.assertIn("window_targets=[", prompt)

    def test_source_window_retries_only_the_rejected_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw), windowed=True)
            fixture, compiler = _compiler_for_activation(activation)
            plan = compiler.build_plan()
            model_targets = tuple(
                item
                for item in plan.targets
                if item.execution_mode.value == "MODEL_TARGET"
            )
            first_target, second_target = model_targets
            first_window = ParentProvisionObjectWindowCandidateV1(
                items=(
                    ParentProvisionObjectWindowItemV1(
                        batch_id=first_target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1.model_validate(
                            _candidate_for_target(fixture, ordinal=0)
                        ),
                    ),
                    ParentProvisionObjectWindowItemV1(
                        batch_id=second_target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1(
                            scope_status=ParentProvisionObjectScopeStatus.UNRESOLVED,
                            diagnostic_codes=("WORKING_DECISION_MISSING",),
                        ),
                    ),
                )
            )
            second_window = ParentProvisionObjectWindowCandidateV1(
                items=(
                    ParentProvisionObjectWindowItemV1(
                        batch_id=second_target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1.model_validate(
                            _candidate_for_target(fixture, ordinal=1)
                        ),
                    ),
                )
            )
            transport = _QueueTransport(
                (
                    first_window.model_dump(mode="json"),
                    second_window.model_dump(mode="json"),
                )
            )
            saved = []

            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
                max_targets_per_turn=8,
                window_checkpoint_sink=saved.append,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
            )

        self.assertEqual(transport.calls, 2)
        receipt_by_id = {item.batch_id: item for item in result.batch_receipts}
        self.assertEqual(len(receipt_by_id[first_target.batch_id].attempts), 1)
        self.assertEqual(len(receipt_by_id[second_target.batch_id].attempts), 2)
        retry_prompt = _prompt(transport.invocations[1])
        self.assertNotIn(first_target.batch_id, retry_prompt)
        self.assertIn(second_target.batch_id, retry_prompt)
        self.assertIn("OBJECT_WINDOW_WORKING_DECISION_REQUIRED", retry_prompt)
        self.assertIn("Return a concrete working requirementness", retry_prompt)
        self.assertIsNotNone(result.object_batch_aggregate)
        self.assertEqual(
            tuple(
                tuple(item[0].batch_id for item in checkpoint) for checkpoint in saved
            ),
            ((first_target.batch_id,), (second_target.batch_id,)),
        )

    def test_source_window_checkpoints_valid_siblings_when_retry_is_exhausted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw), windowed=True)
            fixture, compiler = _compiler_for_activation(activation)
            plan = compiler.build_plan()
            model_targets = tuple(
                item
                for item in plan.targets
                if item.execution_mode.value == "MODEL_TARGET"
            )
            first_target, second_target = model_targets
            window = ParentProvisionObjectWindowCandidateV1(
                items=(
                    ParentProvisionObjectWindowItemV1(
                        batch_id=first_target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1.model_validate(
                            _candidate_for_target(fixture, ordinal=0)
                        ),
                    ),
                    ParentProvisionObjectWindowItemV1(
                        batch_id=second_target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1(
                            scope_status=ParentProvisionObjectScopeStatus.UNRESOLVED,
                            diagnostic_codes=("WORKING_DECISION_MISSING",),
                        ),
                    ),
                )
            )
            saved = []

            result = ParentProvisionObjectStageDriver(
                transport=_QueueTransport((window.model_dump(mode="json"),)),
                object_compiler=compiler,
                max_attempts_per_object=1,
                max_targets_per_turn=8,
                window_checkpoint_sink=saved.append,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
            )

        self.assertEqual(len(saved), 1)
        self.assertEqual(
            tuple(item[0].batch_id for item in saved[0]), (first_target.batch_id,)
        )
        receipt_by_id = {item.batch_id: item for item in result.batch_receipts}
        self.assertEqual(
            receipt_by_id[first_target.batch_id].status,
            ParentProvisionRecognitionBatchStatus.ACCEPTED,
        )
        self.assertEqual(
            receipt_by_id[second_target.batch_id].status,
            ParentProvisionRecognitionBatchStatus.FAILED,
        )

    def test_source_window_timeout_splits_the_pending_target_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw), windowed=True)
            fixture, compiler = _compiler_for_activation(activation)
            plan = compiler.build_plan()
            model_targets = tuple(
                item
                for item in plan.targets
                if item.execution_mode.value == "MODEL_TARGET"
            )
            first_target, second_target = model_targets

            def one_item_window(target, ordinal):  # type: ignore[no-untyped-def]
                return ParentProvisionObjectWindowCandidateV1(
                    items=(
                        ParentProvisionObjectWindowItemV1(
                            batch_id=target.batch_id,
                            candidate=ParentProvisionObjectCandidateV1.model_validate(
                                _candidate_for_target(fixture, ordinal=ordinal)
                            ),
                        ),
                    )
                ).model_dump(mode="json")

            transport = _QueueTransport(
                (
                    LifecycleProbeError("APP_SERVER_TIMEOUT"),
                    one_item_window(first_target, 0),
                    one_item_window(second_target, 1),
                )
            )

            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
                max_attempts_per_object=2,
                max_targets_per_turn=8,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
            )

        self.assertEqual(transport.calls, 3)
        receipt_by_id = {item.batch_id: item for item in result.batch_receipts}
        for target in model_targets:
            self.assertEqual(
                tuple(
                    attempt.outcome
                    for attempt in receipt_by_id[target.batch_id].attempts
                ),
                (
                    ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
                    ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
                ),
            )
        first_retry_prompt = _prompt(transport.invocations[1])
        second_retry_prompt = _prompt(transport.invocations[2])
        self.assertIn(first_target.batch_id, first_retry_prompt)
        self.assertNotIn(second_target.batch_id, first_retry_prompt)
        self.assertIn(second_target.batch_id, second_retry_prompt)
        self.assertNotIn(first_target.batch_id, second_retry_prompt)
        self.assertIn("APP_SERVER_TIMEOUT", first_retry_prompt)
        self.assertIn("APP_SERVER_TIMEOUT", second_retry_prompt)
        self.assertIsNotNone(result.object_batch_aggregate)

    def test_source_window_checkpoint_resumes_without_second_agent_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw), windowed=True)
            fixture, compiler = _compiler_for_activation(activation)
            plan = compiler.build_plan()
            model_targets = tuple(
                item
                for item in plan.targets
                if item.execution_mode.value == "MODEL_TARGET"
            )
            window = ParentProvisionObjectWindowCandidateV1(
                items=tuple(
                    ParentProvisionObjectWindowItemV1(
                        batch_id=target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1.model_validate(
                            _candidate_for_target(fixture, ordinal=ordinal)
                        ),
                    )
                    for ordinal, target in enumerate(model_targets)
                )
            )
            saved = []
            first_transport = _QueueTransport((window.model_dump(mode="json"),))
            first = ParentProvisionObjectStageDriver(
                transport=first_transport,
                object_compiler=compiler,
                max_targets_per_turn=8,
                window_checkpoint_sink=saved.append,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
            )
            resumed_receipts = tuple(item[0] for item in saved[0])
            resumed_compilations = tuple(item[1] for item in saved[0])
            second_transport = _QueueTransport(())

            second = ParentProvisionObjectStageDriver(
                transport=second_transport,
                object_compiler=compiler,
                max_targets_per_turn=8,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
                resumed_compilations=resumed_compilations,
                resumed_batch_receipts=resumed_receipts,
            )

        self.assertEqual(first_transport.calls, 1)
        self.assertEqual(second_transport.calls, 0)
        self.assertEqual(first.object_batch_aggregate, second.object_batch_aggregate)
        self.assertEqual(first.batch_receipts, second.batch_receipts)

    def test_resumed_target_is_a_hard_boundary_for_a_partial_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw), windowed=True)
            fixture, compiler = _compiler_for_activation(activation)
            plan = compiler.build_plan()
            model_targets = tuple(
                item
                for item in plan.targets
                if item.execution_mode.value == "MODEL_TARGET"
            )
            full_window = ParentProvisionObjectWindowCandidateV1(
                items=tuple(
                    ParentProvisionObjectWindowItemV1(
                        batch_id=target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1.model_validate(
                            _candidate_for_target(fixture, ordinal=ordinal)
                        ),
                    )
                    for ordinal, target in enumerate(model_targets)
                )
            )
            first = ParentProvisionObjectStageDriver(
                transport=_QueueTransport((full_window.model_dump(mode="json"),)),
                object_compiler=compiler,
                max_targets_per_turn=8,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
            )
            receipt_by_id = {item.batch_id: item for item in first.batch_receipts}
            compilation_by_id = {item.batch_id: item for item in first.compilations}
            resumed_target = model_targets[-1]
            remaining_target = model_targets[0]
            partial_window = ParentProvisionObjectWindowCandidateV1(
                items=(
                    ParentProvisionObjectWindowItemV1(
                        batch_id=remaining_target.batch_id,
                        candidate=ParentProvisionObjectCandidateV1.model_validate(
                            _candidate_for_target(fixture, ordinal=0)
                        ),
                    ),
                )
            )
            transport = _QueueTransport((partial_window.model_dump(mode="json"),))

            resumed = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
                max_targets_per_turn=8,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=plan,
                resumed_batch_receipts=(receipt_by_id[resumed_target.batch_id],),
                resumed_compilations=(compilation_by_id[resumed_target.batch_id],),
            )

        self.assertEqual(transport.calls, 1)
        self.assertIsNotNone(resumed.object_batch_aggregate)
        self.assertEqual(
            tuple(item.batch_id for item in resumed.batch_receipts),
            tuple(item.batch_id for item in plan.targets),
        )

    def test_activation_lineage_accepts_exactly_one_legacy_or_region_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            _, compiler = _compiler_for_activation(activation)
            legacy = activation.d3_stage_input_attestation
            assert legacy is not None
            manifest_sha256 = "a" * 64
            runtime_sha256 = "b" * 64
            region = SimpleNamespace(
                bundle_sha256=legacy.bundle_sha256,
                stage_input_manifest=SimpleNamespace(
                    manifest_sha256=manifest_sha256,
                    lineage=SimpleNamespace(
                        source_ir_id=compiler.d2_identity.source_ir_id,
                        source_ir_canonical_sha256=(
                            compiler.d2_identity.source_ir_canonical_sha256
                        ),
                    ),
                ),
            )
            region_compiler = replace(
                compiler,
                stage_input_manifest_sha256=manifest_sha256,
                runtime_adapter_sha256=runtime_sha256,
            )

            region_activation = replace(
                activation,
                d3_stage_input_attestation=None,
                d3_region_stage_input_attestation=region,
            )
            _require_activation_lineage(region_activation, region_compiler)
            _require_activation_lineage(activation, compiler)

            with self.assertRaisesRegex(
                LifecycleProbeError,
                "OBJECT_BATCH_STAGE_LINEAGE_INVALID",
            ):
                _require_activation_lineage(
                    replace(
                        activation,
                        d3_region_stage_input_attestation=region,
                    ),
                    compiler,
                )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "OBJECT_BATCH_STAGE_LINEAGE_INVALID",
            ):
                _require_activation_lineage(region_activation, compiler)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "OBJECT_BATCH_STAGE_LINEAGE_INVALID",
            ):
                _require_activation_lineage(activation, region_compiler)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "OBJECT_BATCH_STAGE_LINEAGE_INVALID",
            ):
                _require_activation_lineage(
                    replace(
                        region_activation,
                        d3_region_stage_input_attestation=SimpleNamespace(
                            bundle_sha256="f" * 64,
                            stage_input_manifest=region.stage_input_manifest,
                        ),
                    ),
                    region_compiler,
                )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "OBJECT_BATCH_STAGE_LINEAGE_INVALID",
            ):
                _require_activation_lineage(
                    replace(
                        region_activation,
                        d3_region_stage_input_attestation=SimpleNamespace(
                            bundle_sha256=region.bundle_sha256,
                            stage_input_manifest=SimpleNamespace(
                                manifest_sha256="f" * 64,
                                lineage=region.stage_input_manifest.lineage,
                            ),
                        ),
                    ),
                    region_compiler,
                )

    def test_rejected_output_retries_with_exact_feedback_and_immutable_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            first_valid = _candidate_for_target(fixture, ordinal=0)
            second_valid = _candidate_for_target(fixture, ordinal=1)
            transport = _QueueTransport(({}, first_valid, second_valid))
            session = _ObjectSession(activation)

            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(session, task_binding_sha256=BASE_TASK_BINDING_SHA256)

        self.assertEqual(session.execution_modes, ("OBJECT_BATCH",))
        self.assertEqual(transport.calls, 3)
        retried_batch = result.batch_receipts[1]
        self.assertEqual(
            tuple(item.outcome for item in retried_batch.attempts),
            (
                ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED,
                ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
            ),
        )
        first_attempt = retried_batch.attempts[0]
        retry_prompt = _prompt(transport.invocations[1])
        self.assertIn(
            "previous_failure_code=OBJECT_BATCH_CANDIDATE_SCHEMA_REJECTED",
            retry_prompt,
        )
        self.assertIn(f"previous_attempt_id={first_attempt.attempt_id}", retry_prompt)
        self.assertIn(
            "previous_attempt_receipt_sha256=" + first_attempt.receipt_sha256,
            retry_prompt,
        )
        feedback = {
            "attempt_id": first_attempt.attempt_id,
            "attempt_receipt_sha256": first_attempt.receipt_sha256,
            "failure_code": "OBJECT_BATCH_CANDIDATE_SCHEMA_REJECTED",
        }
        feedback_text = json.dumps(
            feedback,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assertIn(f"previous_failure_feedback={feedback_text}", retry_prompt)
        for field in (
            "object_batch_plan_id",
            "object_batch_plan_sha256",
            "object_batch_id",
            "logical_object_id",
            "target_segment_ordinals",
            "target_scope_sha256",
            "target_evidence_sha256",
        ):
            self.assertEqual(
                _prompt_value(_prompt(transport.invocations[0]), field),
                _prompt_value(retry_prompt, field),
            )
        target = result.plan.targets[1]
        expected_retry_binding = canonical_json_sha256(
            {
                "binding_version": PARENT_PROVISION_OBJECT_PROMPT_BINDING_VERSION,
                "base_task_binding_sha256": BASE_TASK_BINDING_SHA256,
                "execution_mode": PARENT_PROVISION_OBJECT_EXECUTION_MODE,
                "plan_id": result.plan.plan_id,
                "plan_sha256": result.plan.plan_sha256,
                "batch_id": target.batch_id,
                "batch_order": target.batch_order,
                "logical_object_id": target.logical_object_id,
                "segment_ordinals": target.segment_ordinals,
                "target_scope_sha256": target.target_scope_sha256,
                "target_evidence_sha256": target.target_evidence_sha256,
                "attempt_ordinal": 2,
                "previous_failure_feedback": feedback,
            }
        )
        self.assertEqual(
            transport.invocations[1].task_binding_sha256,
            expected_retry_binding,
        )
        self.assertIsNotNone(result.object_batch_aggregate)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionRecognitionAggregateStatus.COMPLETE,
        )

    def test_host_empty_is_accepted_without_a_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            transport = _QueueTransport(
                (
                    _candidate_for_target(fixture, ordinal=0),
                    _candidate_for_target(fixture, ordinal=1),
                )
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 2)
        self.assertEqual(
            result.batch_receipts[0].status,
            ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
        )
        self.assertEqual(result.batch_receipts[0].attempts, ())

    def test_recovery_blocks_only_its_object_and_independent_object_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            original = compiler.build_plan()
            recovery_plan = require_parent_provision_object_evidence_recovery(
                plan=original,
                expected_plan_sha256=original.plan_sha256,
                logical_object_id=original.targets[1].logical_object_id,
            )
            transport = _QueueTransport((_candidate_for_target(fixture, ordinal=1),))
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                plan=recovery_plan,
            )

        self.assertEqual(transport.calls, 1)
        self.assertEqual(
            tuple(item.status for item in result.batch_receipts),
            (
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
                ParentProvisionRecognitionBatchStatus.BLOCKED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
            ),
        )
        self.assertIsNone(result.object_batch_aggregate)
        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 0)

    def test_adjacent_coordination_does_not_run_global_aggregate_or_stop_next_object(
        self,
    ) -> None:
        reconciliation = ParentProvisionObjectCandidateV1(
            scope_status=(
                ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
            ),
            diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
        ).model_dump(mode="json")
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            transport = _QueueTransport(
                (reconciliation, _candidate_for_target(fixture, ordinal=1))
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 2)
        self.assertEqual(
            result.batch_receipts[1].status,
            ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(
            result.batch_receipts[2].status,
            ParentProvisionRecognitionBatchStatus.ACCEPTED,
        )
        self.assertIsNone(result.object_batch_aggregate)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionRecognitionAggregateStatus.BLOCKED,
        )

    def test_transport_failure_is_retried_and_does_not_become_semantic_unresolved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            transport = _QueueTransport(
                (
                    LifecycleProbeError("APP_SERVER_TIMEOUT"),
                    _candidate_for_target(fixture, ordinal=0),
                    _candidate_for_target(fixture, ordinal=1),
                )
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 3)
        self.assertEqual(
            result.batch_receipts[1].attempts[0].outcome,
            ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
        )
        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 0)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionRecognitionAggregateStatus.COMPLETE,
        )

    def test_unresolved_requirementness_is_counted_separately_from_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            unresolved_entry = _entry_for(fixture.segments[0], ordinal=0).model_copy(
                update={
                    "requirementness": Requirementness.UNRESOLVED,
                    "requirement_kind": RequirementKind.UNRESOLVED,
                    "technicality": RequirementTechnicality.UNRESOLVED,
                    "reason_codes": ("REQUIREMENTNESS_AMBIGUOUS",),
                }
            )
            transport = _QueueTransport(
                (
                    _closed_candidate(unresolved_entry).model_dump(mode="json"),
                    _candidate_for_target(fixture, ordinal=1),
                )
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 0)
        self.assertEqual(result.execution_receipt.semantic_unresolved_entry_count, 1)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
        )

    def test_partial_predecessor_keeps_working_aggregate_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            partial_identity = compiler.d2_identity.model_copy(
                update={"status": ParentProvisionStatus.PARTIAL}
            )
            partial_compiler = replace(compiler, d2_identity=partial_identity)
            transport = _QueueTransport(
                (
                    _candidate_for_target(fixture, ordinal=0),
                    _candidate_for_target(fixture, ordinal=1),
                )
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=partial_compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertTrue(result.execution_receipt.predecessor_partial)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
        )

    def test_exhausted_output_failure_does_not_stop_independent_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            transport = _QueueTransport(({}, _candidate_for_target(fixture, ordinal=1)))
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
                max_attempts_per_object=1,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 2)
        self.assertEqual(
            result.batch_receipts[1].status,
            ParentProvisionRecognitionBatchStatus.FAILED,
        )
        self.assertEqual(
            result.batch_receipts[2].status,
            ParentProvisionRecognitionBatchStatus.ACCEPTED,
        )
        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 0)
        self.assertIsNone(result.execution_receipt.decision_candidate_sha256)

    def test_exhausted_transport_failure_does_not_stop_independent_object(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            transport = _QueueTransport(
                (
                    LifecycleProbeError("APP_SERVER_TIMEOUT"),
                    _candidate_for_target(fixture, ordinal=1),
                )
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
                max_attempts_per_object=1,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 2)
        self.assertEqual(
            tuple(item.status for item in result.batch_receipts),
            (
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
                ParentProvisionRecognitionBatchStatus.FAILED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
            ),
        )
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionRecognitionAggregateStatus.FAILED,
        )
        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 0)

    def test_cancellation_stops_remaining_model_targets_without_semantic_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            _fixture_value, compiler = _compiler_for_activation(activation)
            transport = _QueueTransport(
                (LifecycleProbeError("CANONICAL_STAGE_CANCELLED"),)
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(result.batch_receipts), 2)
        self.assertEqual(
            result.execution_receipt.status,
            ParentProvisionRecognitionAggregateStatus.CANCELLED,
        )
        self.assertEqual(result.execution_receipt.semantic_unresolved_scope_count, 0)
        self.assertIsNone(result.object_batch_aggregate)

    def test_normal_object_runs_create_one_working_host_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            transport = _QueueTransport(
                (
                    _candidate_for_target(fixture, ordinal=0),
                    _candidate_for_target(fixture, ordinal=1),
                )
            )
            result = ParentProvisionObjectStageDriver(
                transport=transport,
                object_compiler=compiler,
            ).run(
                _ObjectSession(activation),
                task_binding_sha256=BASE_TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 2)
        self.assertEqual(len(result.compilations), 2)
        self.assertIsNotNone(result.object_batch_aggregate)
        assert result.object_batch_aggregate is not None
        self.assertEqual(
            result.execution_receipt.object_batch_aggregate_sha256,
            result.object_batch_aggregate.aggregate_sha256,
        )
        self.assertEqual(len(result.execution_receipt.accepted_attempt_ids), 2)
        expected_evidence_refs = parent_provision_evidence_refs(
            declared_logical_object_ids=(
                result.object_batch_aggregate.compiled_decision.declared_logical_object_ids
            ),
            unresolved_logical_object_ids=(
                result.object_batch_aggregate.compiled_decision.unresolved_logical_object_ids
            ),
            entries=result.object_batch_aggregate.compiled_decision.entries,
        )
        self.assertEqual(
            result.execution_receipt.evidence_refs_sha256,
            canonical_json_sha256(expected_evidence_refs),
        )

    def test_variant_activation_rejects_tampered_mode_path_hash_and_variant_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _object_activation(Path(raw))
            fixture, compiler = _compiler_for_activation(activation)
            stage_binding = activation.session_attestation.stages[0]
            assert stage_binding.runner_output_schema is not None
            default_path = (
                next(iter(activation.skill_paths.values())).parents[3]
                / stage_binding.runner_output_schema.path
            ).resolve()
            cases = {
                "mode": replace(
                    activation,
                    runner_output_execution_mode="OTHER_MODE",
                ),
                "path": replace(
                    activation,
                    runner_output_schema_path=default_path,
                    runner_output_schema_sha256=(
                        stage_binding.runner_output_schema.sha256
                    ),
                ),
                "hash": replace(
                    activation,
                    runner_output_schema_sha256="0" * 64,
                ),
                "variant_root": replace(
                    activation,
                    runner_output_schema_variants_sha256="0" * 64,
                ),
            }
            for name, tampered in cases.items():
                with self.subTest(name=name):
                    transport = _QueueTransport(
                        (_candidate_for_target(fixture, ordinal=0),)
                    )
                    driver = ParentProvisionObjectStageDriver(
                        transport=transport,
                        object_compiler=compiler,
                    )
                    if name in {"mode", "hash"}:
                        with self.assertRaises(LifecycleProbeError):
                            driver.run(
                                _ObjectSession(tampered),
                                task_binding_sha256=BASE_TASK_BINDING_SHA256,
                            )
                    else:
                        result = driver.run(
                            _ObjectSession(tampered),
                            task_binding_sha256=BASE_TASK_BINDING_SHA256,
                        )
                        self.assertEqual(
                            result.execution_receipt.status,
                            ParentProvisionRecognitionAggregateStatus.BLOCKED,
                        )
                        self.assertEqual(
                            result.execution_receipt.semantic_unresolved_scope_count,
                            0,
                        )
                    self.assertEqual(transport.calls, 0)


@dataclass
class _ObjectSession:
    activation: object

    def __post_init__(self) -> None:
        self.execution_modes: tuple[str | None, ...] = ()

    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ):  # type: ignore[no-untyped-def]
        if stage is not AgentExtractionStage.PARENT_PROVISION_RECOGNITION:
            raise AssertionError("unexpected stage")
        self.execution_modes = (*self.execution_modes, execution_mode)
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


def _object_activation(
    state: Path,
    *,
    windowed: bool = False,
):  # type: ignore[no-untyped-def]
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
    schema_model = (
        ParentProvisionObjectWindowCandidateV1
        if windowed
        else ParentProvisionObjectCandidateV1
    )
    schema = compile_strict_runner_output_schema(schema_model)
    schema_payload = json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    kit_root = next(iter(activation.skill_paths.values())).parents[3]
    execution_mode = (
        PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE
        if windowed
        else PARENT_PROVISION_OBJECT_EXECUTION_MODE
    )
    schema_id = (
        PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION
        if windowed
        else PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION
    )
    schema_relative = (
        "schemas/parent-provision-object-window-candidate.v1.schema.json"
        if windowed
        else "schemas/parent-provision-object-candidate.v1.schema.json"
    )
    schema_path = kit_root / schema_relative
    schema_path.write_bytes(schema_payload)
    schema_file = CanonicalStageFileDigestV1(
        path=schema_relative,
        sha256=hashlib.sha256(schema_payload).hexdigest(),
    )
    draft = CanonicalStageRunnerOutputSchemaVariantV1.model_construct(
        content_free=True,
        execution_mode=execution_mode,
        schema_id=schema_id,
        schema_file=schema_file,
        variant_sha256="0" * 64,
    )
    variant = CanonicalStageRunnerOutputSchemaVariantV1(
        content_free=True,
        execution_mode=execution_mode,
        schema_id=schema_id,
        schema_file=schema_file,
        variant_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"variant_sha256"})
        ),
    )
    variants_sha256 = canonical_json_sha256([variant.model_dump(mode="json")])
    old_stage = activation.session_attestation.stages[0]
    stage_payload = old_stage.model_dump(mode="json")
    stage_payload.update(
        {
            "runner_output_schema_variants": [variant.model_dump(mode="json")],
            "runner_output_schema_variants_sha256": variants_sha256,
        }
    )
    new_stage = type(old_stage).model_validate(stage_payload)
    attestation_payload = activation.session_attestation.model_dump(
        mode="json",
        exclude={"attestation_id"},
    )
    attestation_payload["stages"] = [new_stage.model_dump(mode="json")]
    attestation = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-"
            + canonical_json_sha256(attestation_payload)[:32]
        ),
        **attestation_payload,
    )
    return replace(
        activation,
        runner_output_schema_id=schema_id,
        runner_output_schema_path=schema_path.resolve(),
        runner_output_schema_sha256=schema_file.sha256,
        runner_output_execution_mode=execution_mode,
        runner_output_schema_variants_sha256=variants_sha256,
        session_attestation=attestation,
    )


def _compiler_for_activation(activation):  # type: ignore[no-untyped-def]
    fixture = _fixture()
    predecessor = activation.predecessor
    stage_input = activation.d3_stage_input_attestation
    evidence = activation.session_attestation.document_evidence
    assert predecessor is not None
    assert stage_input is not None
    assert evidence is not None
    identity = fixture.identity.model_copy(
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
        objects=fixture.objects,
        segments=fixture.segments,
    )
    compiler = CanonicalParentProvisionObjectBatchCompiler(
        d2_identity=fixture.identity,
        logical_objects=fixture.objects,
        source_segments=fixture.segments,
        stage_input_bundle_sha256=stage_input.bundle_sha256,
        kit_identity=fixture.kit,
    )
    return fixture, compiler


def _candidate_for_target(fixture: _Fixture, *, ordinal: int) -> dict[str, object]:
    return _closed_candidate(
        _entry_for(fixture.segments[ordinal], ordinal=ordinal)
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
