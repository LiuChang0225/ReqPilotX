from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
from pathlib import Path
import tempfile
import unittest

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalProductEvidenceRequest,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
)
from app.adapters.agents.codex_app_server.parent_provision_object_driver import (
    PARENT_PROVISION_OBJECT_EXECUTION_MODE,
    PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
)
from app.adapters.agents.codex_app_server.parent_provision_reconciliation_driver import (
    PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3CandidateSourceSpanV2,
    D3ParentBoundaryClassificationEntryV2,
)
from app.application.agent_workflow.d3_parent_provisions import (
    ParentProvisionStatus,
    ProvisionContentRole,
    RequirementKind,
    RequirementTechnicality,
    Requirementness,
)
from app.application.agent_workflow.d3_region_stage_input import (
    materialize_d3_region_stage_input,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.parent_provision_nested_evidence import (
    ParentProvisionNestedEvidenceAssignmentStatus,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectScopeStatus,
    compile_parent_provision_object_candidate,
)
from app.application.agent_workflow.parent_provision_object_reconciliation import (
    build_parent_provision_object_reconciliation_plan,
)
from app.application.agent_workflow.region_parent_runtime_adapter import (
    adapt_region_parent_runtime,
)
from app.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)

if __package__:
    from .test_canonical_app_server_document_session import (
        AGENT_KIT_ROOT,
        REPOSITORY_ROOT,
        _runtime,
    )
    from .test_canonical_d3_region_stage_input_session import _region_request
    from .test_parent_provision_object_reconciliation import _resolved_candidate
    from .test_parent_provision_object_workflow import (
        OBJECT_TASK_SHA256,
        RECONCILIATION_TASK_SHA256,
        _DualSession,
        _QueueTransport,
        _WorkflowFixture,
        _orchestrator,
    )
else:
    from test_canonical_app_server_document_session import (
        AGENT_KIT_ROOT,
        REPOSITORY_ROOT,
        _runtime,
    )
    from test_canonical_d3_region_stage_input_session import _region_request
    from test_parent_provision_object_reconciliation import _resolved_candidate
    from test_parent_provision_object_workflow import (
        OBJECT_TASK_SHA256,
        RECONCILIATION_TASK_SHA256,
        _DualSession,
        _QueueTransport,
        _WorkflowFixture,
        _orchestrator,
    )


class ParentProvisionRegionWorkflowTests(unittest.TestCase):
    def test_direct_region_workflow_binds_nested_assignment_to_both_proofs(
        self,
    ) -> None:
        fixture, session = _region_workflow_fixture()
        self.addCleanup(fixture.temporary_directory.cleanup)
        self.addCleanup(session.close)

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(_region_object_outcomes(fixture)),
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
        closure = result.professional_result.nested_evidence_assignment_closure
        assert closure is not None
        self.assertEqual(
            closure.status,
            ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE,
        )
        self.assertEqual(closure.eligibilities, ())
        self.assertEqual(
            result.finalized_execution_receipt.nested_evidence_assignment_closure_sha256,
            closure.closure_sha256,
        )
        self.assertEqual(
            result.professional_result.professional_proof.nested_evidence_assignment_closure_sha256,
            closure.closure_sha256,
        )

    def test_reconciled_region_workflow_replays_same_nested_assignment_gate(
        self,
    ) -> None:
        fixture, session = _region_workflow_fixture()
        self.addCleanup(fixture.temporary_directory.cleanup)
        self.addCleanup(session.close)
        marked_index = 0

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(
                _region_object_outcomes(fixture, marked_index=marked_index)
            ),
            reconciliation_transport=_QueueTransport(
                _region_reconciliation_outcomes(
                    fixture,
                    marked_index=marked_index,
                )
            ),
        ).run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
            reconciliation_task_binding_sha256=RECONCILIATION_TASK_SHA256,
        )

        self.assertTrue(result.publication_ready)
        self.assertIsNotNone(result.coordination_result)
        assert result.professional_result is not None
        closure = result.professional_result.nested_evidence_assignment_closure
        assert closure is not None
        self.assertEqual(
            closure.status,
            ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE,
        )
        self.assertEqual(closure.eligibilities, ())

    def test_partial_region_predecessor_publishes_partial_with_explicit_closure(
        self,
    ) -> None:
        fixture, session = _region_workflow_fixture(predecessor_partial=True)
        self.addCleanup(fixture.temporary_directory.cleanup)
        self.addCleanup(session.close)

        result = _orchestrator(
            fixture,
            object_transport=_QueueTransport(_region_object_outcomes(fixture)),
            reconciliation_transport=_QueueTransport(()),
        ).run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
        )

        self.assertTrue(result.publication_ready)
        assert result.professional_result is not None
        self.assertEqual(
            result.professional_result.proposal.status,
            ParentProvisionStatus.PARTIAL,
        )
        closure = result.professional_result.nested_evidence_assignment_closure
        assert closure is not None
        self.assertEqual(
            closure.status,
            ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE,
        )


def _region_workflow_fixture(
    *,
    predecessor_partial: bool = False,
):  # type: ignore[no-untyped-def]
    kit = load_portable_agent_kit(AGENT_KIT_ROOT)
    temporary_directory = tempfile.TemporaryDirectory()
    state = Path(temporary_directory.name)
    corpus = state / "authorized-corpus"
    corpus.mkdir(mode=0o700)
    source_path = corpus / "source.pdf"
    build_synthetic_pdf(source_path, include_prompt_injection=False)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    request, predecessor = _region_request(source_sha256, kit.identity)
    session = prepare_canonical_app_server_document_session(
        base_runtime=_runtime(state),
        canonical_kit_root=AGENT_KIT_ROOT,
        stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
        evidence_request=CanonicalProductEvidenceRequest(
            source_path=source_path,
            expected_source_sha256=source_sha256,
            document_role="CUSTOMER_SOR",
            authorized_corpus_root=corpus,
            run_id=request.d2_proposal.run_id,
            opaque_source_handle="source-handle-" + "2" * 32,
        ),
        stage_predecessors=(predecessor,),
        repository_root=REPOSITORY_ROOT,
        d3_region_stage_input_request=request,
    )
    # This workflow fixture combines the canonical transport PDF with the
    # region-contract unit SourceIR.  Pin the out-of-band evidence identity to
    # that already validated region SourceIR before asking for an activation.
    assert session.document_evidence is not None
    session.document_evidence = session.document_evidence.model_copy(
        update={
            "source_ir_id": request.expected_lineage.source_ir_id,
            "source_ir_canonical_sha256": (
                request.expected_lineage.source_ir_canonical_sha256
            ),
        }
    )
    stage_input = materialize_d3_region_stage_input(
        request.source_ir,
        proposal=request.d2_proposal,
        validation_receipt=request.d2_validation_receipt,
        parent_input=request.parent_input,
        parent_input_receipt=request.parent_input_receipt,
        expected_lineage=request.expected_lineage,
    )
    runtime = adapt_region_parent_runtime(
        proposal=request.d2_proposal,
        validation_receipt=request.d2_validation_receipt,
        stage_input=stage_input,
        expected_kit_identity=kit.identity,
        expected_source_sha256=source_sha256,
        expected_source_ir_canonical_sha256=source_ir_canonical_sha256(
            request.source_ir
        ),
        expected_stage_input_manifest_sha256=stage_input.manifest.manifest_sha256,
        expected_workspace_bundle_sha256=(
            session.d3_region_stage_input.attestation.bundle_sha256
        ),
    )
    object_activation = session.require_inference_stage(
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        execution_mode=PARENT_PROVISION_OBJECT_EXECUTION_MODE,
    )
    window_activation = session.require_inference_stage(
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        execution_mode=PARENT_PROVISION_OBJECT_WINDOW_EXECUTION_MODE,
    )
    reconciliation_activation = session.require_inference_stage(
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        execution_mode=PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
    )
    region_attestation = object_activation.d3_region_stage_input_attestation
    assert region_attestation is not None
    if predecessor_partial:
        compiler = CanonicalParentProvisionObjectBatchCompiler(
            d2_identity=runtime.d2_identity.model_copy(
                update={"status": ParentProvisionStatus.PARTIAL}
            ),
            logical_objects=runtime.logical_objects,
            source_segments=runtime.source_segments,
            stage_input_bundle_sha256=region_attestation.bundle_sha256,
            kit_identity=kit.identity,
            stage_input_manifest_sha256=(
                region_attestation.stage_input_manifest.manifest_sha256
            ),
            runtime_adapter_sha256=runtime.adapter_sha256,
        )
    else:
        compiler = CanonicalParentProvisionObjectBatchCompiler.from_region_runtime(
            runtime=runtime,
            workspace_attestation=region_attestation,
            kit_identity=kit.identity,
        )
    fixture = _WorkflowFixture(
        temporary_directory=temporary_directory,
        kit=kit,
        object_activation=object_activation,
        window_activation=window_activation,
        reconciliation_activation=reconciliation_activation,
        compiler=compiler,
        objects=runtime.logical_objects,
        segments=runtime.source_segments,
    )
    return fixture, session


def _region_object_outcomes(
    fixture: _WorkflowFixture,
    *,
    marked_index: int | None = None,
) -> tuple[dict[str, object], ...]:
    outcomes: list[dict[str, object]] = []
    for index, target in enumerate(_model_targets(fixture)):
        if index == marked_index:
            candidate = ParentProvisionObjectCandidateV1(
                scope_status=(
                    ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ),
                diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
            )
        else:
            candidate = ParentProvisionObjectCandidateV1(
                scope_status=ParentProvisionObjectScopeStatus.CLOSED_WITHIN_OBJECT,
                entries=_entries_for_ordinals(fixture, target.segment_ordinals),
            )
        outcomes.append(candidate.model_dump(mode="json"))
    return tuple(outcomes)


def _region_reconciliation_outcomes(
    fixture: _WorkflowFixture,
    *,
    marked_index: int,
) -> tuple[dict[str, object], ...]:
    plan = fixture.compiler.build_plan()
    local_results = []
    for index, target in enumerate(_model_targets(fixture)):
        candidate = (
            ParentProvisionObjectCandidateV1(
                scope_status=(
                    ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ),
                diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
            )
            if index == marked_index
            else ParentProvisionObjectCandidateV1(
                scope_status=ParentProvisionObjectScopeStatus.CLOSED_WITHIN_OBJECT,
                entries=_entries_for_ordinals(fixture, target.segment_ordinals),
            )
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
            *_entries_for_ordinals(fixture, group.global_segment_ordinals)
        ).model_dump(mode="json")
        for group in reconciliation_plan.groups
    )


def _model_targets(fixture: _WorkflowFixture):  # type: ignore[no-untyped-def]
    return tuple(
        item
        for item in fixture.compiler.build_plan().targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )


def _entries_for_ordinals(
    fixture: _WorkflowFixture,
    ordinals: tuple[int, ...],
) -> tuple[D3ParentBoundaryClassificationEntryV2, ...]:
    runs: list[list[int]] = []
    for ordinal in ordinals:
        if not runs or ordinal != runs[-1][-1] + 1:
            runs.append([ordinal])
        else:
            runs[-1].append(ordinal)
    return tuple(
        D3ParentBoundaryClassificationEntryV2(
            source_spans=tuple(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=ordinal,
                    start_offset=0,
                    end_segment_ordinal=ordinal,
                    end_offset=len(fixture.segments[ordinal].source_text),
                )
                for ordinal in run
            ),
            requirementness=Requirementness.REQUIREMENT,
            requirement_kind=RequirementKind.FUNCTIONAL,
            technicality=RequirementTechnicality.TECHNICAL,
            content_role=ProvisionContentRole.PROVISION,
            confidence=0.93,
            reason_codes=("SOURCE_BOUND_CLASSIFICATION",),
        )
        for run in runs
    )


if __name__ == "__main__":
    unittest.main()
