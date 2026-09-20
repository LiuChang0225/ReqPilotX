from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalD3RegionStageInputRequest,
    CanonicalD3StageInputRequest,
    CanonicalProductEvidenceRequest,
    CanonicalStagePredecessorBindingV1,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.document_evidence_host import prepare_document_evidence
from app.adapters.agents.portable_kit import (
    AgentKitValidationError,
    load_portable_agent_kit,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalStructureProposalV1,
    D2LogicalStructureValidationReceiptV1,
    stable_d2_artifact_id,
    stable_d2_logical_structure_proposal_sha256,
)
from app.application.agent_workflow.d3_parent_provisions import canonical_json_sha256
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)

if __package__:
    from . import test_d2_logical_structure_v2 as d2_v2_helpers
    from . import test_d3_region_stage_input as region_helpers
    from .test_canonical_app_server_document_session import (
        AGENT_KIT_ROOT,
        REPOSITORY_ROOT,
        _runtime,
    )
    from .test_d3_stage_input_workspace import _fixture as _v1_fixture
else:
    import test_d2_logical_structure_v2 as d2_v2_helpers
    import test_d3_region_stage_input as region_helpers
    from test_canonical_app_server_document_session import (
        AGENT_KIT_ROOT,
        REPOSITORY_ROOT,
        _runtime,
    )
    from test_d3_stage_input_workspace import _fixture as _v1_fixture


class CanonicalD3RegionStageInputSessionTests(unittest.TestCase):
    def test_region_request_attests_and_activates_object_mode(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
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
            # This contract fixture uses the region unit SourceIR rather than
            # the PDF adapter output. Align the out-of-band evidence identity
            # explicitly; the deterministic real-adapter case is covered below.
            assert session.document_evidence is not None
            session.document_evidence = session.document_evidence.model_copy(
                update={
                    "source_ir_id": request.expected_lineage.source_ir_id,
                    "source_ir_canonical_sha256": (
                        request.expected_lineage.source_ir_canonical_sha256
                    ),
                }
            )
            private_root = session.session_root
            try:
                attestation = session.attest()
                activation = session.require_inference_stage(
                    AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
                    execution_mode="OBJECT_BATCH",
                )

                self.assertIsNone(attestation.d3_stage_input)
                self.assertIsNotNone(attestation.d3_region_stage_input)
                self.assertIsNone(activation.d3_stage_input_attestation)
                self.assertIsNotNone(activation.d3_region_stage_input_attestation)
                self.assertEqual(
                    activation.stage_input_manifest_path,
                    activation.d3_region_stage_input_manifest_path,
                )
                self.assertEqual(
                    activation.d3_region_stage_input_manifest_path.resolve(),
                    (
                        private_root
                        / "stage-input/d3-region-parent-provision/manifest.json"
                    ).resolve(),
                )
                self.assertTrue(activation.stage_input_manifest_path.is_file())
                self.assertEqual(
                    activation.predecessor.proposal_schema_id,
                    "logical-structure-proposal/v2",
                )
                self.assertEqual(
                    activation.d3_region_stage_input_attestation.predecessor_artifact_id,
                    predecessor.artifact_id,
                )
                self.assertEqual(
                    activation.turn_sandbox_policy["access"]["readableRoots"],
                    [str(private_root.resolve())],
                )
                self.assertTrue(activation.visual_input_paths)
                serialized = attestation.model_dump_json()
                self.assertNotIn(str(private_root), serialized)
                self.assertNotIn(str(source_path), serialized)
                self.assertNotIn("A source-faithful provision", serialized)
                self.assertNotIn("table-1-cell", serialized)

                tampered_payload = attestation.model_dump(
                    mode="json",
                    exclude={"attestation_id"},
                )
                tampered_payload["document_evidence"][
                    "source_ir_canonical_sha256"
                ] = "f" * 64
                tampered_id = (
                    "canonical-session-attestation-"
                    + canonical_json_sha256(tampered_payload)[:32]
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "region D3 stage input drifted",
                ):
                    CanonicalAppServerDocumentSessionAttestationV1(
                        attestation_id=tampered_id,
                        **tampered_payload,
                    )
            finally:
                session.close()
            self.assertFalse(private_root.exists())

    def test_real_source_ir_dual_materialization_has_stable_identity(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        with tempfile.TemporaryDirectory() as raw:
            source_path = Path(raw) / "source.pdf"
            build_synthetic_pdf(source_path, include_prompt_injection=False)
            source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            common = {
                "expected_source_sha256": source_sha256,
                "document_role": "CUSTOMER_SOR",
                "kit_identity": kit.identity,
                "stage_context_sha256": "c" * 64,
                "run_id": "agent-run-" + "1" * 32,
                "opaque_source_handle": "source-handle-" + "2" * 32,
            }
            first = prepare_document_evidence(
                source_path,
                **common,
                clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            second = prepare_document_evidence(
                source_path,
                **common,
                clock=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

            self.assertEqual(first.source_ir.source_ir_id, second.source_ir.source_ir_id)
            self.assertEqual(
                source_ir_canonical_sha256(first.source_ir),
                source_ir_canonical_sha256(second.source_ir),
            )

    def test_region_predecessor_schema_and_receipt_lineage_fail_closed(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        request, predecessor = _region_request("a" * 64, kit.identity)
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(
                AgentKitValidationError,
                "lineage drifted",
            ):
                prepare_canonical_app_server_document_session(
                    base_runtime=_runtime(Path(raw)),
                    canonical_kit_root=AGENT_KIT_ROOT,
                    stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
                    stage_predecessors=(
                        predecessor.model_copy(
                            update={
                                "proposal_schema_id": "logical-structure-proposal/v1"
                            }
                        ),
                    ),
                    d3_region_stage_input_request=request,
                )

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(
                AgentKitValidationError,
                "lineage drifted",
            ):
                prepare_canonical_app_server_document_session(
                    base_runtime=_runtime(Path(raw)),
                    canonical_kit_root=AGENT_KIT_ROOT,
                    stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
                    stage_predecessors=(
                        predecessor.model_copy(
                            update={"validator_receipt_sha256": "e" * 64}
                        ),
                    ),
                    d3_region_stage_input_request=request,
                )

    def test_legacy_v1_request_still_materializes_unchanged_contract(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        request, predecessor = _legacy_request(kit.identity)
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
                stage_predecessors=(predecessor,),
                d3_stage_input_request=request,
            )
            private_root = session.session_root
            try:
                self.assertIsNotNone(session.d3_stage_input)
                self.assertIsNone(session.d3_region_stage_input)
                self.assertTrue(session.d3_stage_input.manifest_path.is_file())
                self.assertEqual(
                    session.d3_stage_input.attestation.predecessor.proposal_schema_id,
                    "logical-structure-proposal/v1",
                )
            finally:
                session.close()
            self.assertFalse(private_root.exists())

    def test_legacy_and_region_requests_are_mutually_exclusive_before_writes(
        self,
    ) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        region_request, region_predecessor = _region_request("a" * 64, kit.identity)
        legacy_request, _ = _legacy_request(kit.identity)
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            with self.assertRaisesRegex(
                AgentKitValidationError,
                "mutually exclusive",
            ):
                prepare_canonical_app_server_document_session(
                    base_runtime=_runtime(state),
                    canonical_kit_root=AGENT_KIT_ROOT,
                    stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
                    stage_predecessors=(region_predecessor,),
                    d3_stage_input_request=legacy_request,
                    d3_region_stage_input_request=region_request,
                )
            self.assertFalse(
                (state / "canonical-app-server-document-sessions").exists()
            )


def _region_request(source_sha256: str, kit_identity):
    with (
        patch.object(d2_v2_helpers, "SOURCE_SHA", source_sha256),
        patch.object(
            d2_v2_helpers,
            "KIT_SHA",
            kit_identity.bundle_sha256,
        ),
        patch.object(region_helpers, "_kit_identity", lambda: kit_identity),
    ):
        fixture = region_helpers._Fixture()
        prepared = fixture.prepare(fixture.complete_scope_artifact())
    request = CanonicalD3RegionStageInputRequest(
        source_ir=prepared.source,
        d2_proposal=prepared.proposal,
        d2_validation_receipt=prepared.receipt,
        parent_input=prepared.parent_input,
        parent_input_receipt=prepared.parent_receipt,
        expected_lineage=prepared.lineage,
    )
    predecessor = CanonicalStagePredecessorBindingV1(
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        predecessor_stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        run_id=prepared.proposal.run_id,
        source_sha256=prepared.proposal.source_sha256,
        artifact_id=prepared.receipt.artifact_id,
        artifact_sha256=prepared.receipt.artifact_sha256,
        validator_receipt_sha256=prepared.receipt.receipt_sha256,
        proposal_schema_id="logical-structure-proposal/v2",
        status=AgentStageArtifactStatus.VALIDATED,
    )
    return request, predecessor


def _legacy_request(kit_identity):
    fixture = _v1_fixture()
    old = fixture.proposal
    d1 = old.predecessor_d1_artifact.model_copy(
        update={"kit_bundle_sha256": kit_identity.bundle_sha256}
    )
    proposal_values = {
        field_name: getattr(old, field_name)
        for field_name in D2LogicalStructureProposalV1.model_fields
        if field_name
        not in {"kit_identity", "predecessor_d1_artifact", "proposal_sha256"}
    }
    proposal_values.update(
        kit_identity=kit_identity,
        predecessor_d1_artifact=d1,
    )
    proposal_draft = D2LogicalStructureProposalV1.model_construct(
        **proposal_values,
        proposal_sha256="0" * 64,
    )
    proposal = D2LogicalStructureProposalV1(
        **proposal_values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(proposal_draft),
    )
    old_receipt = fixture.receipt
    receipt_values = {
        field_name: getattr(old_receipt, field_name)
        for field_name in D2LogicalStructureValidationReceiptV1.model_fields
        if field_name
        not in {
            "artifact_id",
            "artifact_sha256",
            "kit_identity",
            "predecessor_d1_artifact_sha256",
            "proposal_sha256",
            "receipt_sha256",
        }
    }
    artifact_id = stable_d2_artifact_id(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        proposal_sha256=proposal.proposal_sha256,
    )
    receipt_values.update(
        artifact_id=artifact_id,
        artifact_sha256=proposal.proposal_sha256,
        kit_identity=kit_identity,
        predecessor_d1_artifact_sha256=d1.artifact_sha256,
        proposal_sha256=proposal.proposal_sha256,
    )
    receipt_draft = D2LogicalStructureValidationReceiptV1.model_construct(
        **receipt_values,
        receipt_sha256="0" * 64,
    )
    receipt = D2LogicalStructureValidationReceiptV1(
        **receipt_values,
        receipt_sha256=canonical_json_sha256(
            receipt_draft.model_dump(mode="json", exclude={"receipt_sha256"})
        ),
    )
    request = CanonicalD3StageInputRequest(
        d2_proposal=proposal,
        d2_validation_receipt=receipt,
        source_segments=fixture.segments,
    )
    predecessor = CanonicalStagePredecessorBindingV1(
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        predecessor_stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        artifact_id=receipt.artifact_id,
        artifact_sha256=receipt.artifact_sha256,
        validator_receipt_sha256=receipt.receipt_sha256,
        proposal_schema_id="logical-structure-proposal/v1",
        status=AgentStageArtifactStatus.VALIDATED,
    )
    return request, predecessor


if __name__ == "__main__":
    unittest.main()
