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
from unittest.mock import patch

from pydantic import ValidationError

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalAppServerStageActivation,
    CanonicalAppServerStageAttestationV1,
    CanonicalDocumentEvidenceAttestationV1,
    CanonicalStageFileDigestV1,
    CanonicalStagePredecessorBindingV1,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageExecutionReceiptV1,
    CanonicalStageTransportResult,
)
from app.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalD2V3CandidateCompiler,
)
from app.adapters.agents.codex_app_server.canonical_structure_v3_driver import (
    CANONICAL_D2_V3_STAGE_VALIDATOR_ID,
    CANONICAL_D2_V3_STAGE_VALIDATOR_VERSION,
    D2CanonicalV3StageDriver,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.application.agent_workflow.d2_decision_candidate_v3 import (
    D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION,
    D2LogicalStructureDecisionCandidateV3,
    D2RegionLogicalScopePlacementDecisionCandidateV3,
    D2TableRegionDecisionCandidateV3,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalStructureStatus,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
    D2LogicalStructureProposalV2,
)
from app.application.agent_workflow.d2_table_regions import (
    D2NestedValueTableRegionCandidateV1,
    D2TableRegionDispositionKind,
    D2UnresolvedTableRegionCandidateV1,
    d2_table_region_source_ir_canonical_sha256,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunnerKind,
    AgentStageArtifactStatus,
)
from app.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from tests.test_canonical_stage_driver import _ready_activation
from tests.test_d2_decision_candidate_v3 import (
    _d1_identity,
    _false_region,
    _kit_identity,
    _scope,
    _scope_region,
    _section_object,
    _source_fixture,
    _span,
    _supplemental,
)


TASK_BINDING_SHA256 = "c" * 64


class CanonicalStructureV3DriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_fixture()
        self.kit_identity = _kit_identity()
        self.kit = _FakeKit(self.kit_identity)
        self.d1 = _d1_identity(self.kit_identity)
        self.table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=self.source.source_sha256,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        self.compiler = CanonicalD2V3CandidateCompiler(
            source_ir=self.source,
            expected_d1_artifact=self.d1,
            expected_d1_table_map_artifact=self.table_map,
            kit=self.kit,  # type: ignore[arg-type]
        )

    def test_canonical_host_compiler_calls_real_v3_pipeline(self) -> None:
        compiled = self.compiler.compile(_complete_candidate(self.source))

        self.assertEqual(
            compiled.final_compilation.proposal.schema_version,
            D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
        )
        self.assertEqual(
            compiled.final_compilation.proposal.status,
            D2LogicalStructureStatus.COMPLETE,
        )
        self.assertEqual(
            compiled.base_compilation.proposal.predecessor_d1_artifact,
            self.d1,
        )
        self.assertEqual(
            compiled.table_region_artifact.d1_table_map_artifact_sha256,
            self.table_map.artifact_sha256,
        )

    def test_complete_candidate_builds_validated_final_and_execution_receipts(
        self,
    ) -> None:
        candidate = _complete_candidate(self.source)
        with tempfile.TemporaryDirectory() as raw:
            activation = _v3_activation(
                Path(raw),
                source=self.source,
                d1=self.d1,
                kit=self.kit,
            )
            transport = _RecordingTransport(candidate.model_dump(mode="json"))

            result = D2CanonicalV3StageDriver(
                transport=transport,
                candidate_compiler=self.compiler,
            ).run(_ReadySession(activation), task_binding_sha256=TASK_BINDING_SHA256)

        self.assertEqual(transport.calls, 1)
        self.assertEqual(result.decision_candidate, candidate)
        self.assertEqual(result.proposal.status, D2LogicalStructureStatus.COMPLETE)
        self.assertEqual(
            result.stage_artifact.status, AgentStageArtifactStatus.VALIDATED
        )
        self.assertEqual(
            result.stage_artifact.proposal_schema_id,
            D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
        )
        self.assertEqual(
            result.final_receipt.validation_receipt_sha256,
            result.host_validation_receipt.receipt_sha256,
        )
        self.assertEqual(
            result.execution_receipt.host_validation_receipt_sha256,
            result.host_validation_receipt.receipt_sha256,
        )
        self.assertEqual(
            result.execution_receipt.artifact_sha256,
            result.proposal.proposal_sha256,
        )
        tampered = result.execution_receipt.model_dump(mode="json")
        tampered["final_receipt_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "receipt SHA drifted"):
            CanonicalStageExecutionReceiptV1.model_validate(tampered)

    def test_partial_candidate_remains_partial_with_explicit_unresolved_scope(
        self,
    ) -> None:
        candidate = _partial_candidate(self.source)
        with tempfile.TemporaryDirectory() as raw:
            activation = _v3_activation(
                Path(raw),
                source=self.source,
                d1=self.d1,
                kit=self.kit,
            )
            result = D2CanonicalV3StageDriver(
                transport=_RecordingTransport(candidate.model_dump(mode="json")),
                candidate_compiler=self.compiler,
            ).run(_ReadySession(activation), task_binding_sha256=TASK_BINDING_SHA256)

        self.assertEqual(result.proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertEqual(result.stage_artifact.status, AgentStageArtifactStatus.PARTIAL)
        self.assertGreater(result.stage_artifact.unresolved_scope_count, 0)
        self.assertEqual(
            result.final_receipt.artifact_status,
            AgentStageArtifactStatus.PARTIAL,
        )

    def test_invalid_candidate_and_host_rejection_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _v3_activation(
                Path(raw),
                source=self.source,
                d1=self.d1,
                kit=self.kit,
            )
            invalid_transport = _RecordingTransport(
                {
                    "schema_version": (
                        D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION
                    ),
                    "table_regions": {"regions": []},
                    "unexpected": True,
                }
            )
            compiler = _RecordingCompiler(self.compiler, reject_if_called=True)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_D2_V3_CANDIDATE_INVALID",
            ):
                D2CanonicalV3StageDriver(
                    transport=invalid_transport,
                    candidate_compiler=compiler,
                ).run(
                    _ReadySession(activation),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )
            self.assertEqual(compiler.calls, 0)

            rejecting = _RecordingCompiler(self.compiler, reject=True)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_D2_V3_HOST_REJECTED",
            ):
                D2CanonicalV3StageDriver(
                    transport=_RecordingTransport(
                        _complete_candidate(self.source).model_dump(mode="json")
                    ),
                    candidate_compiler=rejecting,
                ).run(
                    _ReadySession(activation),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )
            self.assertEqual(rejecting.calls, 1)

    def test_schema_kit_and_predecessor_tamper_fail_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            activation = _v3_activation(
                Path(raw),
                source=self.source,
                d1=self.d1,
                kit=self.kit,
            )
            candidate_payload = _complete_candidate(self.source).model_dump(mode="json")

            schema_transport = _RecordingTransport(candidate_payload)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_TURN_SCHEMA_INVALID",
            ):
                D2CanonicalV3StageDriver(
                    transport=schema_transport,
                    candidate_compiler=self.compiler,
                ).run(
                    _ReadySession(
                        replace(
                            activation,
                            runner_output_schema_sha256="f" * 64,
                        )
                    ),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )
            self.assertEqual(schema_transport.calls, 0)

            drifted_identity = self.kit_identity.model_copy(
                update={"bundle_sha256": "f" * 64}
            )
            kit_transport = _RecordingTransport(candidate_payload)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_D2_V3_KIT_IDENTITY_MISMATCH",
            ):
                D2CanonicalV3StageDriver(
                    transport=kit_transport,
                    candidate_compiler=_RecordingCompiler(
                        self.compiler,
                        kit=_FakeKit(drifted_identity),
                    ),
                ).run(
                    _ReadySession(activation),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )
            self.assertEqual(kit_transport.calls, 0)

            assert activation.predecessor is not None
            predecessor_transport = _RecordingTransport(candidate_payload)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_PREDECESSOR_INVALID",
            ):
                D2CanonicalV3StageDriver(
                    transport=predecessor_transport,
                    candidate_compiler=self.compiler,
                ).run(
                    _ReadySession(
                        replace(
                            activation,
                            predecessor=activation.predecessor.model_copy(
                                update={"artifact_sha256": "f" * 64}
                            ),
                        )
                    ),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )
            self.assertEqual(predecessor_transport.calls, 0)

    def test_host_receipt_replay_tamper_is_rejected(self) -> None:
        candidate = _complete_candidate(self.source)
        compilation = self.compiler.compile(candidate)
        tampered_receipt = compilation.final_compilation.validation_receipt.model_copy(
            update={"receipt_sha256": "f" * 64}
        )
        tampered_final = compilation.final_compilation.model_copy(
            update={"validation_receipt": tampered_receipt}
        )
        tampered = compilation.model_copy(update={"final_compilation": tampered_final})
        with tempfile.TemporaryDirectory() as raw:
            activation = _v3_activation(
                Path(raw),
                source=self.source,
                d1=self.d1,
                kit=self.kit,
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_D2_V3_HOST_CANDIDATE_INVALID|CANONICAL_D2_V3_HOST",
            ):
                D2CanonicalV3StageDriver(
                    transport=_RecordingTransport(candidate.model_dump(mode="json")),
                    candidate_compiler=_StaticCompiler(self.compiler, tampered),
                ).run(
                    _ReadySession(activation),
                    task_binding_sha256=TASK_BINDING_SHA256,
                )


@dataclass
class _ReadySession:
    activation: CanonicalAppServerStageActivation

    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ) -> CanonicalAppServerStageActivation:
        if stage is not self.activation.stage or execution_mode is not None:
            raise AssertionError("unexpected stage activation")
        return self.activation


class _RecordingTransport:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.calls = 0

    def invoke(self, invocation):  # type: ignore[no-untyped-def]
        self.calls += 1
        return CanonicalStageTransportResult(
            stage=invocation.stage,
            structured_output=self.output,
            output_schema_sha256=invocation.turn_output_schema_sha256,
            response_sha256=_canonical_sha256(self.output),
            tool_receipt_sha256="d" * 64,
            runtime_thread_id="thread-v3-test",
            runtime_turn_id="turn-v3-test",
        )


class _RecordingCompiler:
    def __init__(
        self,
        base: CanonicalD2V3CandidateCompiler,
        *,
        reject: bool = False,
        reject_if_called: bool = False,
        kit: _FakeKit | None = None,
    ) -> None:
        self.source_ir = base.source_ir
        self.expected_d1_artifact = base.expected_d1_artifact
        self.expected_d1_table_map_artifact = base.expected_d1_table_map_artifact
        self.kit = kit or base.kit
        self.base = base
        self.reject = reject
        self.reject_if_called = reject_if_called
        self.calls = 0

    def compile(self, candidate):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.reject_if_called:
            raise AssertionError("compiler must not be called")
        if self.reject:
            raise ValueError("synthetic Host rejection")
        return self.base.compile(candidate)


class _StaticCompiler(_RecordingCompiler):
    def __init__(self, base, compilation) -> None:  # type: ignore[no-untyped-def]
        super().__init__(base)
        self.compilation = compilation

    def compile(self, candidate):  # type: ignore[no-untyped-def]
        del candidate
        self.calls += 1
        return self.compilation


@dataclass(frozen=True)
class _FakeRunnerProfile:
    runner_kind: AgentRunnerKind = AgentRunnerKind.APP_SERVER_PRODUCT
    adapter_id: str = "codex-app-server"
    adapter_version: str = "0.4.0"
    model_id: str = "gpt-5.6-sol"
    reasoning_effort: str = "high"


class _FakeKit:
    def __init__(self, identity: AgentKitIdentityV1) -> None:
        self.identity = identity
        self.runner_profile_sha256 = {"app-server-product": "9" * 64}
        self._profile = _FakeRunnerProfile()

    def runner_profile(self, profile_id: str) -> _FakeRunnerProfile:
        if profile_id != "app-server-product":
            raise KeyError(profile_id)
        return self._profile


def _v3_activation(
    state: Path,
    *,
    source,
    d1,
    kit: _FakeKit,
) -> CanonicalAppServerStageActivation:
    with patch(
        "tests.test_canonical_stage_driver.load_portable_agent_kit",
        return_value=kit,
    ):
        activation = _ready_activation(
            state,
            stage=AgentExtractionStage.LOGICAL_STRUCTURE,
            skill_id="reqpilotx-logical-structure-reconstruction",
            artifact_schema_id=D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
            artifact_schema=D2LogicalStructureProposalV2.model_json_schema(),
            validator_id=CANONICAL_D2_V3_STAGE_VALIDATOR_ID,
            validator_version=CANONICAL_D2_V3_STAGE_VALIDATOR_VERSION,
        )
    runner_schema = compile_strict_runner_output_schema(
        D2LogicalStructureDecisionCandidateV3
    )
    runner_payload = json.dumps(
        runner_schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    runner_relative = (
        "schemas/"
        + D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION.replace("/", ".")
        + ".schema.json"
    )
    runner_path = activation.runtime.sterile_cwd / runner_relative
    runner_path.write_bytes(runner_payload)
    runner_sha256 = hashlib.sha256(runner_payload).hexdigest()

    old_stage = activation.session_attestation.stages[0]
    stage_values = old_stage.model_dump(mode="json")
    stage_values.update(
        {
            "runner_output_schema_id": (
                D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION
            ),
            "runner_output_schema": CanonicalStageFileDigestV1(
                path=runner_relative,
                sha256=runner_sha256,
            ),
        }
    )
    stage_attestation = CanonicalAppServerStageAttestationV1.model_validate(
        stage_values
    )
    predecessor = CanonicalStagePredecessorBindingV1(
        stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        predecessor_stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        run_id=d1.run_id,
        source_sha256=d1.source_sha256,
        artifact_id=d1.artifact_id,
        artifact_sha256=d1.artifact_sha256,
        validator_receipt_sha256=d1.validator_receipt_sha256,
        proposal_schema_id=d1.proposal_schema_id,
        status=d1.status,
    )
    old_evidence = activation.session_attestation.document_evidence
    assert old_evidence is not None
    evidence = CanonicalDocumentEvidenceAttestationV1(
        run_id=d1.run_id,
        opaque_source_handle=old_evidence.opaque_source_handle,
        source_sha256=source.source_sha256,
        document_role=old_evidence.document_role,
        page_count=d1.page_count,
        source_ir_id=source.source_ir_id,
        source_ir_canonical_sha256=(d2_table_region_source_ir_canonical_sha256(source)),
        workspace_bundle_sha256=old_evidence.workspace_bundle_sha256,
        evidence_inventory_sha256=old_evidence.evidence_inventory_sha256,
    )
    session_values = activation.session_attestation.model_dump(
        mode="json",
        exclude={"attestation_id"},
    )
    session_values.update(
        {
            "document_evidence": evidence.model_dump(mode="json"),
            "stage_predecessors": (predecessor.model_dump(mode="json"),),
            "stages": (stage_attestation.model_dump(mode="json"),),
        }
    )
    session = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-" + _canonical_sha256(session_values)[:32]
        ),
        **session_values,
    )
    return replace(
        activation,
        runner_output_schema_id=(
            D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION
        ),
        runner_output_schema_path=runner_path.resolve(),
        runner_output_schema_sha256=runner_sha256,
        session_attestation=session,
        predecessor=predecessor,
    )


def _complete_candidate(source) -> D2LogicalStructureDecisionCandidateV3:
    table_ids = tuple(
        node.table_payload.table_id
        for node in source.nodes
        if node.table_payload is not None
    )
    scope = _scope("scope-a", ("table-scope",))
    return D2LogicalStructureDecisionCandidateV3(
        logical_objects=(_section_object(),),
        table_regions=D2TableRegionDecisionCandidateV3(
            regions=(
                _scope_region("table-scope", table_ids[0], 0, 4, "scope-a"),
                _false_region("layout-table", table_ids[1], 0, 4),
            ),
            logical_scopes=(scope,),
        ),
        region_logical_scope_placements=(
            D2RegionLogicalScopePlacementDecisionCandidateV3(
                logical_scope_key=scope.logical_scope_key,
                parent_object_key="section",
                reason_codes=("place_scope_under_section",),
            ),
        ),
        supplemental_evidence_placements=_supplemental(source),
    )


def _partial_candidate(source) -> D2LogicalStructureDecisionCandidateV3:
    table_ids = tuple(
        node.table_payload.table_id
        for node in source.nodes
        if node.table_payload is not None
    )
    scope = _scope("scope-a", ("owner",))
    return D2LogicalStructureDecisionCandidateV3(
        logical_objects=(_section_object(),),
        table_regions=D2TableRegionDecisionCandidateV3(
            regions=(
                _scope_region("owner", table_ids[0], 0, 1, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested",
                    physical_table_id=table_ids[0],
                    cell_spans=(_span(1, 2),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                _false_region("layout", table_ids[0], 2, 3),
                D2UnresolvedTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.UNRESOLVED,
                    region_key="uncertain",
                    physical_table_id=table_ids[0],
                    cell_spans=(_span(3, 4),),
                    reason_codes=("ambiguous_region",),
                ),
                _false_region("layout-table", table_ids[1], 0, 4),
            ),
            logical_scopes=(scope,),
        ),
        region_logical_scope_placements=(
            D2RegionLogicalScopePlacementDecisionCandidateV3(
                logical_scope_key=scope.logical_scope_key,
                parent_object_key="section",
                reason_codes=("place_scope_under_section",),
            ),
        ),
        supplemental_evidence_placements=_supplemental(source),
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
