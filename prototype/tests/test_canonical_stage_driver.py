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
import unittest

from pydantic import ValidationError

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAgentNativeReadCapabilityV1,
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalAppServerStageActivation,
    CanonicalAppServerStageAttestationV1,
    CanonicalDocumentEvidenceAttestationV1,
    CanonicalStagePredecessorBindingV1,
    CanonicalStageFileDigestV1,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    AgentNativeReadOnlyRuntimePolicyV1,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageExecutionReceiptV1,
    CanonicalStageTransportResult,
    D2CanonicalStageDriver,
    D3CanonicalStageDriver,
    D3ParentProvisionDecisionCandidateV1,
    build_canonical_stage_invocation,
)
from app.adapters.agents.codex_app_server.d3_stage_input_workspace import (
    D3StageInputBundleAttestationV1,
    D3StageInputFileDigestV1,
    D3StageInputPredecessorBindingV1,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
)
from app.adapters.agents.d3_parent_provision_attestation import (
    compile_d3_parent_provision_policy_closure,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.application.agent_workflow.d2_logical_structure import (
    D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION,
    D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
    D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
    D2LogicalObjectKind,
    D2LogicalObjectV1,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV1,
    D2SourceHandleKind,
    D2SourceSegmentV1,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_artifact_id,
    stable_d2_logical_object_id,
    stable_d2_logical_object_sha256,
    stable_d2_logical_structure_proposal_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
)
from app.application.agent_workflow.d2_decision_candidate import (
    D2LogicalStructureCompilationV1,
    D2LogicalStructureDecisionCandidateV2,
    D2LogicalObjectDecisionCandidateV2,
    D2CandidateSourceRangeV1,
)
from app.application.agent_workflow.d3_classification_attestation import (
    canonical_sha256,
)
from app.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
    ParentProvisionProposalV3,
    ParentProvisionStatus,
)
from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3HostCompiledParentProvisionDecisionV1,
    D3ParentBoundaryClassificationCandidateV2,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentStageArtifactStatus,
)
from app.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = REPOSITORY_ROOT / "agent-kit"
RUN_ID = "agent-run-" + "a" * 32
SOURCE_SHA256 = "b" * 64
TASK_BINDING_SHA256 = "c" * 64


class CanonicalStageDriverTests(unittest.TestCase):
    def test_real_planned_d2_and_d3_fail_before_transport_or_host(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            session = prepare_canonical_app_server_document_session(
                base_runtime=_runtime(state),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(
                    AgentExtractionStage.LOGICAL_STRUCTURE,
                    AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
                ),
            )
            transport = _RecordingTransport()
            d2_compiler = _RecordingD2CandidateCompiler()
            d3_compiler = _RecordingD3CandidateCompiler()
            attestor = _UnavailableD3Attestor()
            d3_host = _RecordingD3HostValidator()
            try:
                with self.assertRaises(LifecycleProbeError):
                    D2CanonicalStageDriver(
                        transport=transport,
                        candidate_compiler=d2_compiler,
                    ).run(session, task_binding_sha256=TASK_BINDING_SHA256)
                with self.assertRaises(LifecycleProbeError):
                    D3CanonicalStageDriver(
                        transport=transport,
                        candidate_compiler=d3_compiler,
                        decision_attestor=attestor,
                        host_validator=d3_host,
                    ).run(session, task_binding_sha256=TASK_BINDING_SHA256)
            finally:
                session.close()
        self.assertEqual(transport.calls, 0)
        self.assertEqual(d2_compiler.calls, 0)
        self.assertEqual(d3_compiler.calls, 0)
        self.assertEqual(attestor.calls, 0)
        self.assertEqual(d3_host.calls, 0)

    def test_invocation_attaches_domain_and_capability_skills_without_copying_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
            )
            try:
                activation = session.require_inference_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING
                )
                schema = json.loads(
                    activation.runner_output_schema_path.read_text(encoding="utf-8")
                )
                binding = activation.session_attestation.stages[0]
                invocation = build_canonical_stage_invocation(
                    activation,
                    task_binding_sha256=TASK_BINDING_SHA256,
                    turn_output_schema_id=binding.runner_output_schema_id,
                    turn_output_schema=schema,
                    expected_final_artifact_schema_id=binding.artifact_schema_id,
                    expected_validator_id=activation.validator_id,
                    expected_validator_version=activation.validator_version,
                )
                prompt = invocation.input_items[0]["text"]
                self.assertEqual(
                    tuple(item["type"] for item in invocation.input_items),
                    ("text", "skill", "skill"),
                )
                self.assertEqual(
                    tuple(
                        item["name"]
                        for item in invocation.input_items
                        if item["type"] == "skill"
                    ),
                    ("reqpilotx-document-understanding", "pdf"),
                )
                for name, skill_path in activation.skill_paths.items():
                    self.assertIn(f"${name}", prompt)
                    self.assertNotIn(
                        skill_path.read_text(encoding="utf-8"),
                        prompt,
                    )
                self.assertNotIn(invocation.developer_context, prompt)
                self.assertEqual(
                    invocation.input_items[1]["path"],
                    str(activation.skill_paths["reqpilotx-document-understanding"]),
                )
                self.assertEqual(
                    invocation.input_items[2]["path"],
                    str(activation.skill_paths["pdf"]),
                )
            finally:
                session.close()

    def test_d2_injected_transport_and_host_form_valid_execution_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            proposal = _d2_proposal()
            host_receipt = _d2_receipt(proposal)
            activation = _ready_activation(
                state,
                stage=AgentExtractionStage.LOGICAL_STRUCTURE,
                skill_id="reqpilotx-logical-structure-reconstruction",
                artifact_schema_id=D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION,
                artifact_schema=D2LogicalStructureProposalV1.model_json_schema(),
                validator_id=D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
                validator_version=D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
            )
            candidate = _d2_wire_candidate()
            transport = _RecordingTransport(candidate.model_dump(mode="json"))
            compiler = _RecordingD2CandidateCompiler(
                D2LogicalStructureCompilationV1(
                    proposal=proposal,
                    validation_receipt=host_receipt,
                )
            )

            result = D2CanonicalStageDriver(
                transport=transport,
                candidate_compiler=compiler,
            ).run(
                _ReadySession(activation),
                task_binding_sha256=TASK_BINDING_SHA256,
            )

        self.assertEqual(transport.calls, 1)
        self.assertEqual(compiler.calls, 1)
        self.assertEqual(result.decision_candidate, candidate)
        self.assertEqual(result.proposal, proposal)
        self.assertEqual(result.host_validation_receipt, host_receipt)
        self.assertEqual(result.stage_artifact.status.value, "VALIDATED")
        self.assertEqual(
            result.stage_artifact.stage,
            AgentExtractionStage.LOGICAL_STRUCTURE,
        )
        self.assertEqual(
            result.final_receipt.validation_receipt_sha256,
            host_receipt.receipt_sha256,
        )
        self.assertEqual(
            result.execution_receipt.artifact_id,
            host_receipt.artifact_id,
        )
        tampered = result.execution_receipt.model_dump(mode="json")
        tampered["response_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "receipt SHA drifted"):
            CanonicalStageExecutionReceiptV1.model_validate(tampered)

    def test_d2_ready_contract_still_rejects_unattested_native_read_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            proposal = _d2_proposal()
            activation = _ready_activation(
                Path(raw),
                stage=AgentExtractionStage.LOGICAL_STRUCTURE,
                skill_id="reqpilotx-logical-structure-reconstruction",
                artifact_schema_id=D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION,
                artifact_schema=D2LogicalStructureProposalV1.model_json_schema(),
                validator_id=D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
                validator_version=D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
                native_read_ready=False,
            )
            candidate = _d2_wire_candidate()
            transport = _RecordingTransport(candidate.model_dump(mode="json"))
            compiler = _RecordingD2CandidateCompiler(
                D2LogicalStructureCompilationV1(
                    proposal=proposal,
                    validation_receipt=_d2_receipt(proposal),
                )
            )

            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_NATIVE_READ_LIFECYCLE_FEATURES_DISABLED",
            ):
                D2CanonicalStageDriver(
                    transport=transport,
                    candidate_compiler=compiler,
                ).run(
                    _ReadySession(activation),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )

        self.assertEqual(transport.calls, 0)
        self.assertEqual(compiler.calls, 0)

    def test_d3_candidate_rejects_agent_supplied_policy_attestation(self) -> None:
        payload = _d3_model_candidate().model_dump(mode="json")
        payload["policy_attestation"] = {
            "attestation_id": "d3-policy-attestation-" + "0" * 32
        }

        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            D3ParentBoundaryClassificationCandidateV2.model_validate(payload)
        self.assertNotIn(
            "policy_attestation",
            D3ParentBoundaryClassificationCandidateV2.model_json_schema()["properties"],
        )

    def test_d3_direct_attestor_unavailable_fails_after_turn_before_host(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            model_candidate = _d3_model_candidate()
            candidate = _d3_candidate()
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
            transport = _RecordingTransport(model_candidate.model_dump(mode="json"))
            compiler = _RecordingD3CandidateCompiler(candidate)
            attestor = _UnavailableD3Attestor()
            host = _RecordingD3HostValidator()

            with self.assertRaisesRegex(
                LifecycleProbeError,
                "D3_DIRECT_DECISION_ATTESTOR_UNAVAILABLE",
            ):
                D3CanonicalStageDriver(
                    transport=transport,
                    candidate_compiler=compiler,
                    decision_attestor=attestor,
                    host_validator=host,
                ).run(
                    _ReadySession(activation),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )

        self.assertEqual(transport.calls, 1)
        self.assertEqual(compiler.calls, 1)
        self.assertEqual(attestor.calls, 1)
        self.assertEqual(host.calls, 0)
        self.assertEqual(
            attestor.kwargs["request"].parent_provision_decision_sha256,
            candidate.decision_sha256,
        )
        self.assertEqual(
            attestor.kwargs["request"].evidence_refs,
            candidate.unresolved_logical_object_ids,
        )


@dataclass
class _ReadySession:
    activation: CanonicalAppServerStageActivation

    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
    ) -> CanonicalAppServerStageActivation:
        if stage is not self.activation.stage:
            raise AssertionError("unexpected stage")
        return self.activation


class _RecordingTransport:
    def __init__(self, output=None) -> None:  # type: ignore[no-untyped-def]
        self.output = output
        self.calls = 0
        self.invocation = None

    def invoke(self, invocation):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.invocation = invocation
        if self.output is None:
            raise AssertionError("transport must not be called")
        return CanonicalStageTransportResult(
            stage=invocation.stage,
            structured_output=self.output,
            output_schema_sha256=invocation.turn_output_schema_sha256,
            response_sha256=canonical_sha256(dict(self.output)),
            tool_receipt_sha256="d" * 64,
            runtime_thread_id="thread-test",
            runtime_turn_id="turn-test",
        )


class _RecordingD2CandidateCompiler:
    def __init__(self, compilation=None) -> None:  # type: ignore[no-untyped-def]
        self.compilation = compilation
        self.calls = 0

    def compile(self, candidate):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.compilation is None:
            raise AssertionError("D2 candidate compiler must not be called")
        return self.compilation


class _RecordingD3CandidateCompiler:
    def __init__(self, candidate=None) -> None:  # type: ignore[no-untyped-def]
        self.candidate = candidate
        self.calls = 0

    def compile(self, model_candidate):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.candidate is None:
            raise AssertionError("D3 candidate compiler must not be called")
        return D3HostCompiledParentProvisionDecisionV1.model_validate(
            self.candidate.model_dump(mode="json")
        )


class _UnavailableD3Attestor:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs = None

    def attest_decision(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.kwargs = kwargs
        raise LifecycleProbeError("D3_DIRECT_DECISION_ATTESTOR_UNAVAILABLE")


class _RecordingD3HostValidator:
    def __init__(self) -> None:
        self.calls = 0

    def validate(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("D3 Host validator must not be called")


def _ready_activation(
    state: Path,
    *,
    stage: AgentExtractionStage,
    skill_id: str,
    artifact_schema_id: str,
    artifact_schema: dict[str, object],
    validator_id: str,
    validator_version: str,
    include_d3_closure: bool = False,
    native_read_ready: bool = True,
) -> CanonicalAppServerStageActivation:
    runtime = _runtime(state)
    kit_root = state / "ready-kit"
    skill_path = kit_root / f".agents/skills/{skill_id}/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: " + skill_id + "\n---\nUNIQUE_SKILL_BODY_MUST_NOT_REPEAT\n",
        encoding="utf-8",
    )
    schema_relative = f"schemas/{artifact_schema_id.replace('/', '.')}.schema.json"
    schema_path = kit_root / schema_relative
    schema_path.parent.mkdir(parents=True)
    schema_payload = json.dumps(
        artifact_schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    schema_path.write_bytes(schema_payload)
    if stage is AgentExtractionStage.LOGICAL_STRUCTURE:
        runner_output_schema_id = "d2-logical-structure-decision-candidate/v2"
        runner_output_schema = compile_strict_runner_output_schema(
            D2LogicalStructureDecisionCandidateV2
        )
    elif stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION:
        runner_output_schema_id = "d3-parent-boundary-classification-candidate/v2"
        runner_output_schema = compile_strict_runner_output_schema(
            D3ParentBoundaryClassificationCandidateV2
        )
    else:
        runner_output_schema_id = artifact_schema_id
        runner_output_schema = artifact_schema
    runner_relative = (
        "schemas/" + runner_output_schema_id.replace("/", ".") + ".schema.json"
    )
    runner_schema_path = kit_root / runner_relative
    runner_schema_payload = json.dumps(
        runner_output_schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if runner_schema_path != schema_path:
        runner_schema_path.write_bytes(runner_schema_payload)
    runtime = replace(runtime, sterile_cwd=kit_root.resolve())
    context_text = "REQPILOTX_TEST_POLICY_CONTEXT\n"
    context_sha256 = canonical_sha256({"context_text": context_text, "stage": stage})
    skill_sha256 = hashlib.sha256(skill_path.read_bytes()).hexdigest()
    schema_sha256 = hashlib.sha256(schema_payload).hexdigest()
    runner_schema_sha256 = hashlib.sha256(runner_schema_payload).hexdigest()
    stage_attestation = CanonicalAppServerStageAttestationV1(
        stage=stage,
        implementation_status="AVAILABLE",
        context_kind="EXECUTABLE_STAGE_CONTEXT",
        skill_id=skill_id,
        skill=CanonicalStageFileDigestV1(
            path=f".agents/skills/{skill_id}/SKILL.md",
            sha256=skill_sha256,
        ),
        policy=CanonicalStageFileDigestV1(
            path="policy/extraction-policy.v1.json",
            sha256="1" * 64,
        ),
        references=(),
        context_sha256=context_sha256,
        artifact_schema_id=artifact_schema_id,
        artifact_schema=CanonicalStageFileDigestV1(
            path=schema_relative,
            sha256=schema_sha256,
        ),
        runner_output_schema_id=runner_output_schema_id,
        runner_output_schema=CanonicalStageFileDigestV1(
            path=runner_relative,
            sha256=runner_schema_sha256,
        ),
        validator_id=validator_id,
        validator_version=validator_version,
        required_tools=(),
        inference_available=True,
        diagnostic_codes=(),
    )
    kit = load_portable_agent_kit(AGENT_KIT_ROOT)
    profile = kit.runner_profile("app-server-product")
    d3_closure = (
        compile_d3_parent_provision_policy_closure(kit) if include_d3_closure else None
    )
    predecessor = None
    if stage is AgentExtractionStage.LOGICAL_STRUCTURE:
        predecessor = CanonicalStagePredecessorBindingV1(
            stage=stage,
            predecessor_stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA256,
            artifact_id="agent-artifact-" + "4" * 32,
            artifact_sha256="5" * 64,
            validator_receipt_sha256="8" * 64,
            proposal_schema_id="document-understanding-proposal/v2",
            status=AgentStageArtifactStatus.VALIDATED,
        )
    elif stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION:
        predecessor = CanonicalStagePredecessorBindingV1(
            stage=stage,
            predecessor_stage=AgentExtractionStage.LOGICAL_STRUCTURE,
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA256,
            artifact_id="agent-artifact-" + "5" * 32,
            artifact_sha256="6" * 64,
            validator_receipt_sha256="9" * 64,
            proposal_schema_id="logical-structure-proposal/v1",
            status=AgentStageArtifactStatus.VALIDATED,
        )
    d3_stage_input = None
    d3_stage_input_manifest = None
    if stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION:
        assert predecessor is not None
        d3_predecessor = D3StageInputPredecessorBindingV1.model_validate(
            predecessor.model_dump(mode="json")
        )
        files = (
            D3StageInputFileDigestV1(
                path="d2-logical-structure-proposal.json",
                sha256="a" * 64,
            ),
            D3StageInputFileDigestV1(
                path="d2-logical-structure-validation-receipt.json",
                sha256="b" * 64,
            ),
            D3StageInputFileDigestV1(
                path="d3-exact-source-segments.json",
                sha256="c" * 64,
            ),
        )
        stage_input_values = {
            "content_free": True,
            "stage": AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            "run_id": RUN_ID,
            "source_sha256": SOURCE_SHA256,
            "kit_bundle_sha256": kit.identity.bundle_sha256,
            "predecessor": d3_predecessor,
            "d2_identity_sha256": "d" * 64,
            "source_scope_sha256": "e" * 64,
            "files": files,
        }
        stage_input_draft = D3StageInputBundleAttestationV1.model_construct(
            attestation_id="d3-stage-input-" + "0" * 32,
            bundle_sha256="0" * 64,
            **stage_input_values,
        )
        stage_input_sha = canonical_sha256(
            stage_input_draft.model_dump(
                mode="json",
                exclude={"attestation_id", "bundle_sha256"},
            )
        )
        d3_stage_input = D3StageInputBundleAttestationV1(
            attestation_id=f"d3-stage-input-{stage_input_sha[:32]}",
            bundle_sha256=stage_input_sha,
            **stage_input_values,
        )
        d3_stage_input_manifest = (
            runtime.sterile_cwd / "stage-input/d3-parent-provision/manifest.json"
        )
        d3_stage_input_manifest.parent.mkdir(parents=True)
        d3_stage_input_manifest.write_text(
            d3_stage_input.model_dump_json(),
            encoding="utf-8",
        )
    attestation_values = {
        "session_id": "canonical-document-session-" + "2" * 32,
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
        "agent_native_read_capability": (
            CanonicalAgentNativeReadCapabilityV1(
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
            )
            if native_read_ready
            else CanonicalAgentNativeReadCapabilityV1(
                status="BLOCKED",
                shell_tool_enabled=False,
                unified_exec_enabled=False,
                view_image_enabled=False,
                local_image_input_enabled=False,
                session_root_read_confinement_attested=False,
                network_access_enabled=False,
                filesystem_write_enabled=False,
                real_transport_eligible=False,
                diagnostic_codes=(
                    "CANONICAL_NATIVE_READ_LIFECYCLE_FEATURES_DISABLED",
                    "CANONICAL_NATIVE_READ_ROOT_CONFINEMENT_UNATTESTED",
                ),
            )
        ),
        "native_runtime_policy": (
            AgentNativeReadOnlyRuntimePolicyV1(
                readable_root_path_sha256=hashlib.sha256(
                    str(runtime.sterile_cwd.resolve()).encode("utf-8")
                ).hexdigest()
            )
            if native_read_ready
            else None
        ),
        "document_evidence": (
            CanonicalDocumentEvidenceAttestationV1(
                run_id=RUN_ID,
                opaque_source_handle="source-handle-" + "4" * 32,
                source_sha256=SOURCE_SHA256,
                document_role="SYNTHETIC_TEST",
                page_count=1,
                source_ir_id="source-ir-test",
                source_ir_canonical_sha256="5" * 64,
                workspace_bundle_sha256="6" * 64,
                evidence_inventory_sha256="7" * 64,
            )
            if native_read_ready
            else None
        ),
        "stage_predecessors": ((predecessor,) if predecessor is not None else ()),
        "d3_stage_input": d3_stage_input,
        "workspace_manifest_sha256": "3" * 64,
        "workspace_manifest_unchanged": True,
        "stages": (stage_attestation,),
        "d3_policy_closure_sha256": (
            canonical_sha256(d3_closure.model_dump(mode="json"))
            if d3_closure is not None
            else None
        ),
    }
    draft = CanonicalAppServerDocumentSessionAttestationV1.model_construct(
        attestation_id="canonical-session-attestation-" + "0" * 32,
        **attestation_values,
    )
    attestation_payload = draft.model_dump(mode="json", exclude={"attestation_id"})
    attestation = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-"
            + canonical_sha256(attestation_payload)[:32]
        ),
        **attestation_values,
    )
    evidence_workspace = runtime.sterile_cwd / "evidence-workspace"
    visual_inputs: tuple[Path, ...] = ()
    if native_read_ready:
        evidence_workspace.mkdir()
        contact_sheet = evidence_workspace / "contact-sheet.png"
        contact_sheet.write_bytes(b"synthetic-image")
        visual_inputs = (contact_sheet.resolve(),)
    return CanonicalAppServerStageActivation(
        runtime=runtime,
        stage=stage,
        skill_paths={skill_id: skill_path.resolve()},
        context_text=context_text,
        context_sha256=context_sha256,
        artifact_schema_path=schema_path.resolve(),
        artifact_schema_sha256=schema_sha256,
        runner_output_schema_id=runner_output_schema_id,
        runner_output_schema_path=runner_schema_path.resolve(),
        runner_output_schema_sha256=runner_schema_sha256,
        validator_id=validator_id,
        validator_version=validator_version,
        session_attestation=attestation,
        d3_policy_closure=d3_closure,
        evidence_workspace_path=(
            evidence_workspace.resolve() if native_read_ready else None
        ),
        visual_input_paths=visual_inputs,
        turn_sandbox_policy=(
            {
                "type": "readOnly",
                "access": {
                    "type": "restricted",
                    "includePlatformDefaults": True,
                    "readableRoots": [str(runtime.sterile_cwd.resolve())],
                },
            }
            if native_read_ready
            else None
        ),
        predecessor=predecessor,
        stage_input_manifest_path=d3_stage_input_manifest,
        d3_stage_input_attestation=d3_stage_input,
    )


def _d2_proposal() -> D2LogicalStructureProposalV1:
    kit = load_portable_agent_kit(AGENT_KIT_ROOT).identity
    d1 = D2ValidatedD1ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "4" * 32,
        artifact_sha256="5" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA256,
        status=AgentStageArtifactStatus.VALIDATED,
        proposal_sha256="6" * 64,
        evidence_sha256="7" * 64,
        validator_receipt_sha256="8" * 64,
        page_count=1,
        kit_bundle_sha256=kit.bundle_sha256,
    )
    table_map = AgentD1TableMapArtifactIdentityV1(
        source_sha256=SOURCE_SHA256,
        artifact_id="d1-table-map-test",
        artifact_sha256="9" * 64,
    )
    source_handle = "node-source-test"
    content_sha256 = hashlib.sha256(b"source text").hexdigest()
    segment_id = stable_d2_source_segment_id(
        source_sha256=SOURCE_SHA256,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle=source_handle,
        start_offset=0,
        end_offset=11,
    )
    segment = D2SourceSegmentV1(
        segment_id=segment_id,
        source_sha256=SOURCE_SHA256,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle=source_handle,
        owner_source_node_id=source_handle,
        source_order=0,
        page_numbers=(1,),
        start_offset=0,
        end_offset=11,
        source_content_sha256=content_sha256,
        segment_sha256=stable_d2_source_segment_sha256(
            segment_id=segment_id,
            source_sha256=SOURCE_SHA256,
            handle_kind=D2SourceHandleKind.SOURCE_NODE,
            source_handle=source_handle,
            owner_source_node_id=source_handle,
            source_order=0,
            page_numbers=(1,),
            start_offset=0,
            end_offset=11,
            source_content_sha256=content_sha256,
        ),
    )
    object_id = stable_d2_logical_object_id(
        source_sha256=SOURCE_SHA256,
        object_kind=D2LogicalObjectKind.PROVISION_SCOPE,
        source_segment_ids=(segment.segment_id,),
    )
    object_values = {
        "logical_object_id": object_id,
        "object_kind": D2LogicalObjectKind.PROVISION_SCOPE,
        "parent_logical_object_id": None,
        "source_order": 0,
        "page_numbers": (1,),
        "source_segments": (segment,),
        "logical_table_scope_ids": (),
        "reason_codes": ("source_boundary",),
    }
    logical_object = D2LogicalObjectV1(
        **object_values,
        object_sha256=stable_d2_logical_object_sha256(**object_values),
    )
    proposal_values = {
        "run_id": RUN_ID,
        "source_sha256": SOURCE_SHA256,
        "source_ir_id": "source-ir-test",
        "source_ir_canonical_sha256": "e" * 64,
        "kit_identity": kit,
        "predecessor_d1_artifact": d1,
        "d1_table_map_artifact": table_map,
        "status": D2LogicalStructureStatus.COMPLETE,
        "logical_objects": (logical_object,),
        "logical_table_scopes": (),
        "source_evidence_dispositions": (),
        "physical_table_dispositions": (),
        "unresolved_codes": (),
    }
    draft = D2LogicalStructureProposalV1.model_construct(
        **proposal_values,
        proposal_sha256="0" * 64,
    )
    return D2LogicalStructureProposalV1(
        **proposal_values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
    )


def _d2_receipt(
    proposal: D2LogicalStructureProposalV1,
) -> D2LogicalStructureValidationReceiptV1:
    artifact_id = stable_d2_artifact_id(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        proposal_sha256=proposal.proposal_sha256,
    )
    values = {
        "artifact_id": artifact_id,
        "artifact_sha256": proposal.proposal_sha256,
        "run_id": proposal.run_id,
        "source_sha256": proposal.source_sha256,
        "source_ir_id": proposal.source_ir_id,
        "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
        "kit_identity": proposal.kit_identity,
        "predecessor_d1_artifact_id": (proposal.predecessor_d1_artifact.artifact_id),
        "predecessor_d1_artifact_sha256": (
            proposal.predecessor_d1_artifact.artifact_sha256
        ),
        "d1_table_map_artifact_id": proposal.d1_table_map_artifact.artifact_id,
        "d1_table_map_artifact_sha256": (
            proposal.d1_table_map_artifact.artifact_sha256
        ),
        "status": proposal.status,
        "logical_object_ids": tuple(
            item.logical_object_id for item in proposal.logical_objects
        ),
        "source_segment_ids": tuple(
            segment.segment_id
            for item in proposal.logical_objects
            for segment in item.source_segments
        ),
        "logical_table_scope_ids": (),
        "source_evidence_disposition_ids": (),
        "unresolved_source_evidence_disposition_ids": (),
        "physical_table_ids": (),
        "unresolved_table_ids": (),
        "proposal_sha256": proposal.proposal_sha256,
        "evidence_sha256": "f" * 64,
        "validator_id": D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
        "validator_version": D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
    }
    draft = D2LogicalStructureValidationReceiptV1.model_construct(
        **values,
        receipt_sha256="0" * 64,
    )
    receipt_sha256 = canonical_sha256(
        draft.model_dump(mode="json", exclude={"receipt_sha256"})
    )
    return D2LogicalStructureValidationReceiptV1(
        **values,
        receipt_sha256=receipt_sha256,
    )


def _d2_wire_candidate() -> D2LogicalStructureDecisionCandidateV2:
    return D2LogicalStructureDecisionCandidateV2(
        logical_objects=(
            D2LogicalObjectDecisionCandidateV2(
                object_key="object:source",
                object_kind=D2LogicalObjectKind.PROVISION_SCOPE,
                source_ranges=(
                    D2CandidateSourceRangeV1(
                        handle_kind=D2SourceHandleKind.SOURCE_NODE,
                        source_handle="node-source-test",
                        start_offset=0,
                        end_offset=11,
                    ),
                ),
                reason_codes=("source_boundary",),
            ),
        ),
    )


def _d3_model_candidate() -> D3ParentBoundaryClassificationCandidateV2:
    return D3ParentBoundaryClassificationCandidateV2(
        entries=(),
        unresolved_logical_object_ids=("d2-logical-object-test",),
    )


def _d3_candidate() -> D3ParentProvisionDecisionCandidateV1:
    return D3ParentProvisionDecisionCandidateV1(
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA256,
        kit_identity=load_portable_agent_kit(AGENT_KIT_ROOT).identity,
        predecessor_d2_artifact_id="agent-artifact-" + "5" * 32,
        predecessor_d2_artifact_sha256="6" * 64,
        status=ParentProvisionStatus.PARTIAL,
        declared_logical_object_ids=(),
        unresolved_logical_object_ids=("d2-logical-object-test",),
        entries=(),
        diagnostic_codes=("UNRESOLVED_SCOPE",),
    )


def _runtime(state: Path) -> CanaryRuntimeState:
    workspace = state / "workspace"
    workspace.mkdir(mode=0o700)
    codex_home = state / "codex-home"
    codex_home.mkdir(mode=0o700)
    sqlite_home = state / "sqlite"
    sqlite_home.mkdir(mode=0o700)
    log_dir = state / "logs"
    log_dir.mkdir(mode=0o700)
    tmp_dir = state / "tmp"
    tmp_dir.mkdir(mode=0o700)
    return CanaryRuntimeState(
        codex_home=codex_home,
        command=("codex", "app-server"),
        environment={},
        log_dir=log_dir,
        protocol={"version": "test", "restricted_read_access": "true"},
        sqlite_home=sqlite_home,
        state_path=state,
        sterile_cwd=workspace,
        tmp_dir=tmp_dir,
    )


if __name__ == "__main__":
    unittest.main()
