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
import tempfile
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    AgentNativeReadOnlyRuntimePolicyV1,
)
from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalStageFileDigestV1,
    CanonicalStageRunnerOutputSchemaVariantV1,
)
from app.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
)
from app.adapters.agents.codex_app_server.parent_provision_object_driver import (
    PARENT_PROVISION_OBJECT_EXECUTION_MODE,
    PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
)
from app.adapters.agents.codex_app_server.parent_provision_object_workflow import (
    CanonicalParentProvisionObjectWorkflowOrchestrator,
)
from app.adapters.agents.codex_app_server.parent_provision_professional_aggregate import (
    ParentProvisionProfessionalAggregateError,
    ParentProvisionRecognitionFinalizationMode,
)
from app.adapters.agents.codex_app_server.parent_provision_reconciliation_driver import (
    PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
)
from app.adapters.agents.portable_kit import PortableAgentKit, load_portable_agent_kit
from app.application.agent_workflow.d3_parent_provisions import (
    ParentProvisionStatus,
    Requirementness,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectScopeStatus,
    ParentProvisionObjectWindowCandidateV1,
    ParentProvisionObjectWindowItemV1,
    compile_parent_provision_object_candidate,
    require_parent_provision_object_evidence_recovery,
)
from app.application.agent_workflow.parent_provision_object_reconciliation import (
    build_parent_provision_object_reconciliation_plan,
)
from app.application.agent_workflow.parent_provision_reconciliation_receipts import (
    canonical_json_sha256,
)

if __package__:
    from .test_parent_provision_object_batches import _closed_candidate
    from .test_parent_provision_object_reconciliation import (
        _entry_for,
        _fixture,
        _resolved_candidate,
    )
    from .test_parent_provision_professional_aggregate import _activation
    from .test_parent_provision_reconciliation_driver import _QueueTransport
else:
    from test_parent_provision_object_batches import _closed_candidate
    from test_parent_provision_object_reconciliation import (
        _entry_for,
        _fixture,
        _resolved_candidate,
    )
    from test_parent_provision_professional_aggregate import _activation
    from test_parent_provision_reconciliation_driver import _QueueTransport


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = REPOSITORY_ROOT / "agent-kit"
OBJECT_TASK_SHA256 = "c" * 64
RECONCILIATION_TASK_SHA256 = "d" * 64


@dataclass(frozen=True, slots=True)
class _WorkflowFixture:
    temporary_directory: object
    kit: PortableAgentKit
    object_activation: object
    window_activation: object
    reconciliation_activation: object
    compiler: CanonicalParentProvisionObjectBatchCompiler
    objects: tuple[object, ...]
    segments: tuple[object, ...]


class CanonicalParentProvisionObjectWorkflowTests(unittest.TestCase):
    def _fixture(self) -> _WorkflowFixture:
        fixture = _workflow_fixture()
        self.addCleanup(fixture.temporary_directory.cleanup)
        return fixture

    def test_direct_workflow_publishes_professional_final_artifact(self) -> None:
        fixture = self._fixture()
        outcomes = _object_outcomes(fixture)
        object_transport = _QueueTransport(outcomes)
        reconciliation_transport = _QueueTransport(())
        session = _DualSession(
            fixture.object_activation,
            fixture.reconciliation_activation,
        )

        result = _orchestrator(
            fixture,
            object_transport=object_transport,
            reconciliation_transport=reconciliation_transport,
        ).run(
            session,
            object_task_binding_sha256=OBJECT_TASK_SHA256,
        )

        self.assertTrue(result.publication_ready)
        self.assertIsNone(result.coordination_result)
        self.assertEqual(result.reason_codes, ())
        assert result.finalized_execution_receipt is not None
        assert result.professional_result is not None
        self.assertEqual(
            result.finalized_execution_receipt.finalization_mode,
            ParentProvisionRecognitionFinalizationMode.DIRECT,
        )
        self.assertEqual(
            result.professional_result.final_receipt.proposal_sha256,
            result.professional_result.host_validation_receipt.artifact_sha256,
        )
        self.assertEqual(reconciliation_transport.calls, 0)
        self.assertEqual(
            session.execution_modes,
            (PARENT_PROVISION_OBJECT_EXECUTION_MODE,) * 2,
        )

    def test_source_window_turns_reuse_full_professional_finalization(self) -> None:
        fixture = self._fixture()
        targets = _model_targets(fixture)
        candidates = tuple(
            ParentProvisionObjectCandidateV1.model_validate(item)
            for item in _object_outcomes(fixture)
        )
        item_values = tuple(
            ParentProvisionObjectWindowItemV1(
                batch_id=target.batch_id,
                candidate=candidate,
            )
            for target, candidate in zip(targets, candidates, strict=True)
        )
        windows = (
            ParentProvisionObjectWindowCandidateV1(
                items=item_values[:1],
            ),
            ParentProvisionObjectWindowCandidateV1(
                items=item_values[1:],
            ),
        )
        session = _DualSession(
            fixture.object_activation,
            fixture.reconciliation_activation,
            fixture.window_activation,
        )

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(
                tuple(item.model_dump(mode="json") for item in windows)
            ),
            reconciliation_transport=_QueueTransport(()),
            max_targets_per_turn=8,
        ).run(
            session,
            object_task_binding_sha256=OBJECT_TASK_SHA256,
        )

        self.assertTrue(result.publication_ready)
        self.assertIsNotNone(result.professional_result)
        self.assertEqual(
            session.execution_modes,
            (PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,) * 2,
        )

    def test_reconciliation_workflow_replaces_group_then_publishes(self) -> None:
        fixture = self._fixture()
        marked_index = 1
        object_outcomes = _object_outcomes(fixture, marked_index=marked_index)
        reconciliation_outcomes = _reconciliation_outcomes(
            fixture,
            marked_index=marked_index,
        )
        session = _DualSession(
            fixture.object_activation,
            fixture.reconciliation_activation,
        )

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(object_outcomes),
            reconciliation_transport=_QueueTransport(reconciliation_outcomes),
        ).run(
            session,
            object_task_binding_sha256=OBJECT_TASK_SHA256,
            reconciliation_task_binding_sha256=RECONCILIATION_TASK_SHA256,
        )

        self.assertTrue(result.publication_ready)
        self.assertIsNotNone(result.coordination_result)
        assert result.coordination_result is not None
        assert result.finalized_execution_receipt is not None
        self.assertEqual(
            result.finalized_execution_receipt.finalization_mode,
            ParentProvisionRecognitionFinalizationMode.RECONCILED,
        )
        self.assertEqual(
            result.finalized_execution_receipt.reconciliation_execution_receipts_sha256,
            result.coordination_result.execution_receipt.receipt_sha256,
        )
        marked_batch_id = _model_targets(fixture)[marked_index].batch_id
        self.assertIn(
            marked_batch_id,
            result.finalized_execution_receipt.replaced_batch_ids,
        )
        self.assertEqual(
            session.execution_modes,
            (
                PARENT_PROVISION_OBJECT_EXECUTION_MODE,
                PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
                PARENT_PROVISION_OBJECT_EXECUTION_MODE,
            ),
        )

    def test_recovery_stays_working_and_does_not_publish(self) -> None:
        fixture = self._fixture()
        plan = fixture.compiler.build_plan()
        recovery_target = _model_targets(fixture)[0]
        recovery_plan = require_parent_provision_object_evidence_recovery(
            plan=plan,
            expected_plan_sha256=plan.plan_sha256,
            logical_object_id=recovery_target.logical_object_id,
        )
        remaining_outcomes = _object_outcomes(
            fixture,
            excluded_batch_ids={recovery_target.batch_id},
        )

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(remaining_outcomes),
            reconciliation_transport=_QueueTransport(()),
        ).run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
            plan=recovery_plan,
        )

        self.assertFalse(result.publication_ready)
        self.assertIsNone(result.finalized_execution_receipt)
        self.assertIn(
            "OBJECT_BATCH_EVIDENCE_RECOVERY_REQUIRED",
            result.reason_codes,
        )
        self.assertNotIn("HOST_EMPTY_CONTAINER", result.reason_codes)

    def test_output_failure_stays_working_and_does_not_publish(self) -> None:
        fixture = self._fixture()
        valid = list(_object_outcomes(fixture))
        valid[0] = {}

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(tuple(valid)),
            reconciliation_transport=_QueueTransport(()),
            max_attempts_per_object=1,
        ).run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
        )

        self.assertFalse(result.publication_ready)
        self.assertNotIn("HOST_EMPTY_CONTAINER", result.reason_codes)
        self.assertIsNone(result.professional_result)
        self.assertIn("RETRY_BUDGET_EXHAUSTED", result.reason_codes)

    def test_unresolved_entry_partial_publishes_with_separate_semantic_axis(
        self,
    ) -> None:
        fixture = self._fixture()
        outcomes = _object_outcomes(fixture, unresolved_entry_index=2)

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(outcomes),
            reconciliation_transport=_QueueTransport(()),
        ).run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
        )

        self.assertTrue(result.publication_ready)
        assert result.finalized_execution_receipt is not None
        assert result.professional_result is not None
        self.assertEqual(
            result.finalized_execution_receipt.semantic_unresolved_scope_count,
            0,
        )
        self.assertEqual(
            result.finalized_execution_receipt.semantic_unresolved_entry_count,
            1,
        )
        self.assertEqual(
            result.professional_result.proposal.status,
            ParentProvisionStatus.PARTIAL,
        )

    def test_tampered_coordination_receipt_root_preserves_results_without_publish(
        self,
    ) -> None:
        fixture = self._fixture()
        marked_index = 1
        orchestrator = _TamperingWorkflowOrchestrator(
            kit=fixture.kit,
            object_compiler=fixture.compiler,
            object_transport=_QueueTransport(
                _object_outcomes(fixture, marked_index=marked_index)
            ),
            reconciliation_transport=_QueueTransport(
                _reconciliation_outcomes(fixture, marked_index=marked_index)
            ),
        )

        result = orchestrator.run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
            reconciliation_task_binding_sha256=RECONCILIATION_TASK_SHA256,
        )

        self.assertFalse(result.publication_ready)
        self.assertIsNotNone(result.coordination_result)
        self.assertEqual(
            result.reason_codes,
            ("PARENT_PROVISION_RECONCILIATION_RECEIPT_INVALID",),
        )

    def test_any_professional_contract_error_preserves_direct_object_results(
        self,
    ) -> None:
        fixture = self._fixture()
        unsafe_error = "/Users/customer/secret.pdf failed"

        with patch(
            "app.adapters.agents.codex_app_server.parent_provision_object_workflow."
            "build_nested_evidence_assignment_closure_for_activation",
            side_effect=ParentProvisionProfessionalAggregateError(unsafe_error),
        ):
            result = _orchestrator(
                fixture,
                object_transport=_QueueTransport(_object_outcomes(fixture)),
                reconciliation_transport=_QueueTransport(()),
            ).run(
                _DualSession(
                    fixture.object_activation,
                    fixture.reconciliation_activation,
                ),
                object_task_binding_sha256=OBJECT_TASK_SHA256,
            )

        self.assertFalse(result.publication_ready)
        self.assertIsNotNone(result.object_result.object_batch_aggregate)
        self.assertIsNone(result.coordination_result)
        self.assertEqual(
            result.reason_codes,
            ("PARENT_PROVISION_PROFESSIONAL_FINALIZATION_FAILED",),
        )

    def test_any_professional_contract_error_preserves_reconciliation_results(
        self,
    ) -> None:
        fixture = self._fixture()
        marked_index = 1
        error_code = "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_REPLAY_DRIFT"

        with patch(
            "app.adapters.agents.codex_app_server.parent_provision_object_workflow."
            "build_nested_evidence_assignment_closure_for_activation",
            side_effect=ParentProvisionProfessionalAggregateError(error_code),
        ):
            result = _orchestrator(
                fixture,
                object_transport=_QueueTransport(
                    _object_outcomes(fixture, marked_index=marked_index)
                ),
                reconciliation_transport=_QueueTransport(
                    _reconciliation_outcomes(fixture, marked_index=marked_index)
                ),
            ).run(
                _DualSession(
                    fixture.object_activation,
                    fixture.reconciliation_activation,
                ),
                object_task_binding_sha256=OBJECT_TASK_SHA256,
                reconciliation_task_binding_sha256=RECONCILIATION_TASK_SHA256,
            )

        self.assertFalse(result.publication_ready)
        self.assertIsNotNone(result.coordination_result)
        assert result.coordination_result is not None
        self.assertIsNotNone(result.coordination_result.reconciliation_aggregate)
        self.assertEqual(result.reason_codes, (error_code,))

    def test_programming_error_during_finalization_is_not_hidden(self) -> None:
        fixture = self._fixture()

        with (
            patch(
                "app.adapters.agents.codex_app_server.parent_provision_object_workflow."
                "build_nested_evidence_assignment_closure_for_activation",
                side_effect=RuntimeError("programming defect"),
            ),
            self.assertRaisesRegex(RuntimeError, "programming defect"),
        ):
            _orchestrator(
                fixture,
                object_transport=_QueueTransport(_object_outcomes(fixture)),
                reconciliation_transport=_QueueTransport(()),
            ).run(
                _DualSession(
                    fixture.object_activation,
                    fixture.reconciliation_activation,
                ),
                object_task_binding_sha256=OBJECT_TASK_SHA256,
            )


class _TamperingWorkflowOrchestrator(
    CanonicalParentProvisionObjectWorkflowOrchestrator
):
    def _run_reconciliation(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = super()._run_reconciliation(*args, **kwargs)
        return replace(
            result,
            execution_receipt=result.execution_receipt.model_copy(
                update={"receipt_sha256": "0" * 64}
            ),
        )


class _DualSession:
    def __init__(
        self,
        object_activation: object,
        reconciliation_activation: object,
        window_activation: object | None = None,
    ):
        self._activations = {
            PARENT_PROVISION_OBJECT_EXECUTION_MODE: object_activation,
            PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE: (
                window_activation or object_activation
            ),
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE: (
                reconciliation_activation
            ),
        }
        self._calls: list[str | None] = []

    @property
    def execution_modes(self) -> tuple[str | None, ...]:
        return tuple(self._calls)

    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ):  # type: ignore[no-untyped-def]
        if stage is not AgentExtractionStage.PARENT_PROVISION_RECOGNITION:
            raise AssertionError("unexpected stage")
        self._calls.append(execution_mode)
        return self._activations[execution_mode]


def _orchestrator(
    fixture: _WorkflowFixture,
    *,
    object_transport,
    reconciliation_transport,
    max_attempts_per_object: int = 2,
    max_targets_per_turn: int = 1,
):  # type: ignore[no-untyped-def]
    return CanonicalParentProvisionObjectWorkflowOrchestrator(
        kit=fixture.kit,
        object_compiler=fixture.compiler,
        object_transport=object_transport,
        reconciliation_transport=reconciliation_transport,
        max_attempts_per_object=max_attempts_per_object,
        max_targets_per_turn=max_targets_per_turn,
    )


def _workflow_fixture() -> _WorkflowFixture:
    kit = load_portable_agent_kit(AGENT_KIT_ROOT)
    base = _fixture()
    identity = base.identity.model_copy(update={"kit_identity": kit.identity})
    activation = _activation(kit, identity)
    temporary_directory = tempfile.TemporaryDirectory()
    activation = _runtime_ready_activation(
        activation,
        Path(temporary_directory.name),
    )
    object_activation, window_activation, reconciliation_activation = (
        _variant_activations(
            kit,
            activation,
        )
    )
    stage_input = object_activation.d3_stage_input_attestation
    assert stage_input is not None
    compiler = CanonicalParentProvisionObjectBatchCompiler(
        d2_identity=identity,
        logical_objects=base.objects,
        source_segments=base.segments,
        stage_input_bundle_sha256=stage_input.bundle_sha256,
        kit_identity=kit.identity,
    )
    return _WorkflowFixture(
        temporary_directory=temporary_directory,
        kit=kit,
        object_activation=object_activation,
        window_activation=window_activation,
        reconciliation_activation=reconciliation_activation,
        compiler=compiler,
        objects=base.objects,
        segments=base.segments,
    )


def _runtime_ready_activation(activation, root: Path):  # type: ignore[no-untyped-def]
    evidence_workspace = root / "evidence-workspace"
    evidence_workspace.mkdir(parents=True)
    contact_sheet = evidence_workspace / "contact-sheet.png"
    contact_sheet.write_bytes(b"synthetic-image")
    stage_manifest = root / "stage-input/d3-parent-provision/manifest.json"
    stage_manifest.parent.mkdir(parents=True)
    assert activation.d3_stage_input_attestation is not None
    stage_manifest.write_text(
        activation.d3_stage_input_attestation.model_dump_json(),
        encoding="utf-8",
    )
    runtime = replace(activation.runtime, sterile_cwd=root.resolve())
    session_payload = activation.session_attestation.model_dump(
        mode="json", exclude={"attestation_id"}
    )
    session_payload["native_runtime_policy"] = AgentNativeReadOnlyRuntimePolicyV1(
        readable_root_path_sha256=hashlib.sha256(
            str(root.resolve()).encode("utf-8")
        ).hexdigest()
    ).model_dump(mode="json")
    session = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-"
            + canonical_json_sha256(session_payload)[:32]
        ),
        **session_payload,
    )
    return replace(
        activation,
        runtime=runtime,
        session_attestation=session,
        evidence_workspace_path=evidence_workspace.resolve(),
        visual_input_paths=(contact_sheet.resolve(),),
        turn_sandbox_policy={
            "type": "readOnly",
            "access": {
                "type": "restricted",
                "includePlatformDefaults": True,
                "readableRoots": [str(root.resolve())],
            },
        },
        stage_input_manifest_path=stage_manifest.resolve(),
    )


def _variant_activations(kit: PortableAgentKit, activation):  # type: ignore[no-untyped-def]
    stage_spec = next(
        item
        for item in kit.manifest.stages
        if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    variants = []
    variant_paths = {}
    for item in stage_spec.runner_output_schema_variants:
        schema_file = CanonicalStageFileDigestV1(
            path=item.schema_path,
            sha256=_sha256(kit.root / item.schema_path),
        )
        draft = CanonicalStageRunnerOutputSchemaVariantV1.model_construct(
            content_free=True,
            execution_mode=item.execution_mode,
            schema_id=item.schema_id,
            schema_file=schema_file,
            variant_sha256="0" * 64,
        )
        variants.append(
            CanonicalStageRunnerOutputSchemaVariantV1(
                content_free=True,
                execution_mode=item.execution_mode,
                schema_id=item.schema_id,
                schema_file=schema_file,
                variant_sha256=canonical_json_sha256(
                    draft.model_dump(mode="json", exclude={"variant_sha256"})
                ),
            )
        )
        variant_paths[item.execution_mode] = kit.root / item.schema_path
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
    session_payload = activation.session_attestation.model_dump(
        mode="json", exclude={"attestation_id"}
    )
    session_payload["stages"] = [new_stage.model_dump(mode="json")]
    session = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-"
            + canonical_json_sha256(session_payload)[:32]
        ),
        **session_payload,
    )
    by_mode = {item.execution_mode: item for item in variants_tuple}
    object_variant = by_mode[PARENT_PROVISION_OBJECT_EXECUTION_MODE]
    window_variant = by_mode[PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE]
    reconciliation_variant = by_mode[
        PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
    ]
    object_activation = replace(
        activation,
        session_attestation=session,
        runner_output_schema_id=object_variant.schema_id,
        runner_output_schema_path=variant_paths[PARENT_PROVISION_OBJECT_EXECUTION_MODE],
        runner_output_schema_sha256=object_variant.schema_file.sha256,
        runner_output_execution_mode=PARENT_PROVISION_OBJECT_EXECUTION_MODE,
        runner_output_schema_variants_sha256=variants_sha256,
    )
    window_activation = replace(
        activation,
        session_attestation=session,
        runner_output_schema_id=window_variant.schema_id,
        runner_output_schema_path=variant_paths[
            PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE
        ],
        runner_output_schema_sha256=window_variant.schema_file.sha256,
        runner_output_execution_mode=PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
        runner_output_schema_variants_sha256=variants_sha256,
    )
    reconciliation_activation = replace(
        activation,
        session_attestation=session,
        runner_output_schema_id=reconciliation_variant.schema_id,
        runner_output_schema_path=variant_paths[
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
        ],
        runner_output_schema_sha256=reconciliation_variant.schema_file.sha256,
        runner_output_execution_mode=(
            PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE
        ),
        runner_output_schema_variants_sha256=variants_sha256,
    )
    return object_activation, window_activation, reconciliation_activation


def _model_targets(fixture: _WorkflowFixture):  # type: ignore[no-untyped-def]
    return tuple(
        item
        for item in fixture.compiler.build_plan().targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )


def _object_outcomes(
    fixture: _WorkflowFixture,
    *,
    marked_index: int | None = None,
    unresolved_entry_index: int | None = None,
    excluded_batch_ids: set[str] | None = None,
) -> tuple[dict[str, object], ...]:
    excluded = excluded_batch_ids or set()
    outcomes = []
    for index, target in enumerate(_model_targets(fixture)):
        if target.batch_id in excluded:
            continue
        if index == marked_index:
            candidate = ParentProvisionObjectCandidateV1(
                scope_status=(
                    ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ),
                diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
            )
        else:
            ordinal = target.segment_ordinals[0]
            entry = _entry_for(fixture.segments[ordinal], ordinal=ordinal)
            if index == unresolved_entry_index:
                entry = entry.model_copy(
                    update={
                        "requirementness": Requirementness.UNRESOLVED,
                        "requirement_kind": None,
                        "technicality": None,
                    }
                )
            candidate = _closed_candidate(entry)
        outcomes.append(candidate.model_dump(mode="json"))
    return tuple(outcomes)


def _reconciliation_outcomes(
    fixture: _WorkflowFixture,
    *,
    marked_index: int,
) -> tuple[dict[str, object], ...]:
    plan = fixture.compiler.build_plan()
    local_results = []
    for index, target in enumerate(_model_targets(fixture)):
        if index == marked_index:
            candidate = ParentProvisionObjectCandidateV1(
                scope_status=(
                    ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ),
                diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
            )
        else:
            ordinal = target.segment_ordinals[0]
            candidate = _closed_candidate(
                _entry_for(fixture.segments[ordinal], ordinal=ordinal)
            )
        local_results.append(
            compile_parent_provision_object_candidate(
                plan=plan,
                expected_plan_sha256=plan.plan_sha256,
                batch_id=target.batch_id,
                d2_identity=fixture.compiler.d2_identity,
                source_segments=fixture.compiler.source_segments,
                kit_identity=fixture.compiler.kit_identity,
                candidate=candidate,
            )
        )
    reconciliation_plan = build_parent_provision_object_reconciliation_plan(
        object_batch_plan=plan,
        expected_object_batch_plan_sha256=plan.plan_sha256,
        local_results=tuple(local_results),
        d2_identity=fixture.compiler.d2_identity,
        source_segments=fixture.compiler.source_segments,
        kit_identity=fixture.compiler.kit_identity,
    )
    return tuple(
        _resolved_candidate(
            *(
                _entry_for(fixture.segments[ordinal], ordinal=ordinal)
                for ordinal in group.global_segment_ordinals
            )
        ).model_dump(mode="json")
        for group in reconciliation_plan.groups
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
