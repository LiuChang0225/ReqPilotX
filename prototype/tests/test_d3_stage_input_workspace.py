from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest

from app.adapters.agents.codex_app_server.d3_stage_input_workspace import (
    D3_STAGE_INPUT_MANIFEST_RELATIVE_PATH,
    D3_STAGE_INPUT_RELATIVE_DIRECTORY,
    D3ExactSourceSegmentSetV2,
    D3SegmentOrdinalLedgerEntryV1,
    D3StageInputPredecessorBindingV1,
    D3StageInputWorkspaceError,
    materialize_d3_stage_input_workspace,
    verify_d3_stage_input_workspace,
)
from app.application.agent_workflow.d2_logical_structure import (
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
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    canonical_json_sha256,
    d3_source_scope_sha256,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)


RUN_ID = "agent-run-" + "a" * 32
SOURCE_SHA256 = "b" * 64
SECRET_SOURCE_TEXT = "Synthetic customer requirement text."


@dataclass(frozen=True, slots=True)
class _Fixture:
    proposal: D2LogicalStructureProposalV1
    receipt: D2LogicalStructureValidationReceiptV1
    segments: tuple[D3ExactSourceSegmentV1, ...]
    predecessor: D3StageInputPredecessorBindingV1


class D3StageInputWorkspaceTests(unittest.TestCase):
    def test_materializes_committed_private_bundle_with_content_free_attestation(
        self,
    ) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as raw:
            session_root = _session_root(Path(raw))

            materialized = materialize_d3_stage_input_workspace(
                session_root=session_root,
                d2_proposal=fixture.proposal,
                d2_validation_receipt=fixture.receipt,
                source_segments=fixture.segments,
                predecessor=fixture.predecessor,
            )

            self.assertEqual(
                materialized.workspace_path,
                (session_root / D3_STAGE_INPUT_RELATIVE_DIRECTORY).resolve(),
            )
            self.assertEqual(
                materialized.manifest_path,
                (session_root / D3_STAGE_INPUT_MANIFEST_RELATIVE_PATH).resolve(),
            )
            self.assertEqual(
                stat.S_IMODE(materialized.workspace_path.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                tuple(
                    sorted(item.name for item in materialized.workspace_path.iterdir())
                ),
                (
                    "d2-logical-structure-proposal.json",
                    "d2-logical-structure-validation-receipt.json",
                    "d3-exact-source-segments.json",
                    "manifest.json",
                ),
            )
            for path in materialized.workspace_path.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            attestation_json = json.dumps(
                materialized.attestation.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn(SECRET_SOURCE_TEXT, attestation_json)
            self.assertNotIn(str(session_root), attestation_json)
            source_payload = json.loads(
                (
                    materialized.workspace_path / "d3-exact-source-segments.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                source_payload["segments"][0]["source_text"],
                SECRET_SOURCE_TEXT,
            )
            ordinal = source_payload["segment_ordinal_ledger"][0]
            self.assertEqual(ordinal["segment_ordinal"], 0)
            self.assertEqual(ordinal["source_order"], 17)
            self.assertEqual(
                ordinal["segment_id"],
                source_payload["segments"][0]["segment_id"],
            )
            verified = verify_d3_stage_input_workspace(
                session_root=session_root,
                expected_attestation=materialized.attestation,
            )
            self.assertEqual(verified.attestation, materialized.attestation)

    def test_segment_ordinal_ledger_is_not_source_order_and_cannot_drift(self) -> None:
        fixture = _fixture()
        segment = fixture.segments[0]
        valid = D3SegmentOrdinalLedgerEntryV1(
            segment_ordinal=0,
            segment_id=segment.segment_id,
            logical_object_id=segment.logical_object_id,
            source_order=segment.source_order,
            page_numbers=segment.page_numbers,
            source_text_length=len(segment.source_text),
            d2_segment_sha256=segment.d2_segment_sha256,
        )
        values = {
            "run_id": fixture.proposal.run_id,
            "source_sha256": fixture.proposal.source_sha256,
            "predecessor_d2_artifact_id": fixture.receipt.artifact_id,
            "predecessor_d2_artifact_sha256": fixture.receipt.artifact_sha256,
            "source_scope_sha256": d3_source_scope_sha256(fixture.segments),
            "segment_ordinal_ledger": (valid,),
            "segments": fixture.segments,
        }

        self.assertEqual(
            D3ExactSourceSegmentSetV2(**values).segment_ordinal_ledger[0].source_order,
            17,
        )
        with self.assertRaisesRegex(ValueError, "ordinal ledger drifted"):
            D3ExactSourceSegmentSetV2(
                **{
                    **values,
                    "segment_ordinal_ledger": (
                        valid.model_copy(update={"segment_ordinal": 1}),
                    ),
                }
            )

    def test_rejects_existing_target_without_touching_it(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as raw:
            session_root = _session_root(Path(raw))
            target = session_root / D3_STAGE_INPUT_RELATIVE_DIRECTORY
            target.mkdir(parents=True, mode=0o700)
            target.parent.chmod(0o700)
            target.chmod(0o700)
            sentinel = target / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(
                D3StageInputWorkspaceError,
                "OVERWRITE_FORBIDDEN",
            ):
                _materialize(session_root, fixture)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_rejects_session_and_stage_input_symlinks(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session_root = _session_root(root)
            session_link = root / "session-link"
            session_link.symlink_to(session_root, target_is_directory=True)
            with self.assertRaisesRegex(
                D3StageInputWorkspaceError,
                "SYMLINK_FORBIDDEN",
            ):
                _materialize(session_link, fixture)

            outside = root / "outside"
            outside.mkdir(mode=0o700)
            outside.chmod(0o700)
            (session_root / "stage-input").symlink_to(
                outside,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                D3StageInputWorkspaceError,
                "SYMLINK_FORBIDDEN",
            ):
                _materialize(session_root, fixture)
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_rejects_binding_source_receipt_and_status_drift(self) -> None:
        fixture = _fixture()
        drifts = (
            {"source_sha256": "c" * 64},
            {"validator_receipt_sha256": "d" * 64},
            {"status": AgentStageArtifactStatus.PARTIAL},
        )
        for update in drifts:
            with self.subTest(update=update), tempfile.TemporaryDirectory() as raw:
                session_root = _session_root(Path(raw))
                predecessor = fixture.predecessor.model_copy(update=update)
                with self.assertRaisesRegex(
                    D3StageInputWorkspaceError,
                    "BINDING_MISMATCH",
                ):
                    materialize_d3_stage_input_workspace(
                        session_root=session_root,
                        d2_proposal=fixture.proposal,
                        d2_validation_receipt=fixture.receipt,
                        source_segments=fixture.segments,
                        predecessor=predecessor,
                    )
                self.assertFalse(
                    (session_root / D3_STAGE_INPUT_RELATIVE_DIRECTORY).exists()
                )

    def test_revalidates_receipt_and_exact_segment_objects_before_writing(self) -> None:
        fixture = _fixture()
        stale_receipt = fixture.receipt.model_copy(update={"receipt_sha256": "0" * 64})
        stale_segment = fixture.segments[0].model_copy(
            update={"source_text": "tampered"}
        )

        for receipt, segments in (
            (stale_receipt, fixture.segments),
            (fixture.receipt, (stale_segment,)),
            (fixture.receipt, ()),
        ):
            with tempfile.TemporaryDirectory() as raw:
                session_root = _session_root(Path(raw))
                with self.assertRaisesRegex(
                    D3StageInputWorkspaceError,
                    "PREDECESSOR_CLOSURE_INVALID",
                ):
                    materialize_d3_stage_input_workspace(
                        session_root=session_root,
                        d2_proposal=fixture.proposal,
                        d2_validation_receipt=receipt,
                        source_segments=segments,
                        predecessor=fixture.predecessor,
                    )
                self.assertFalse(
                    (session_root / D3_STAGE_INPUT_RELATIVE_DIRECTORY).exists()
                )

    def test_pinned_verifier_rejects_file_and_symlink_tamper(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as raw:
            session_root = _session_root(Path(raw))
            materialized = _materialize(session_root, fixture)
            source_file = materialized.workspace_path / "d3-exact-source-segments.json"
            source_file.write_text("{}\n", encoding="utf-8")
            source_file.chmod(0o600)
            with self.assertRaisesRegex(
                D3StageInputWorkspaceError,
                "FILE_TAMPERED",
            ):
                verify_d3_stage_input_workspace(
                    session_root=session_root,
                    expected_attestation=materialized.attestation,
                )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session_root = _session_root(root)
            materialized = _materialize(session_root, fixture)
            source_file = materialized.workspace_path / "d3-exact-source-segments.json"
            source_file.unlink()
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            source_file.symlink_to(outside)
            with self.assertRaisesRegex(
                D3StageInputWorkspaceError,
                "SYMLINK_FORBIDDEN",
            ):
                verify_d3_stage_input_workspace(
                    session_root=session_root,
                    expected_attestation=materialized.attestation,
                )

    def test_manifest_path_escape_and_out_of_band_attestation_tamper_fail_closed(
        self,
    ) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as raw:
            session_root = _session_root(Path(raw))
            materialized = _materialize(session_root, fixture)
            manifest = json.loads(
                materialized.manifest_path.read_text(encoding="utf-8")
            )
            manifest["files"][0]["path"] = "../escape.json"
            materialized.manifest_path.write_text(
                json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            materialized.manifest_path.chmod(0o600)
            with self.assertRaises(ValueError):
                verify_d3_stage_input_workspace(
                    session_root=session_root,
                    expected_attestation=materialized.attestation,
                )

        with tempfile.TemporaryDirectory() as raw:
            session_root = _session_root(Path(raw))
            materialized = _materialize(session_root, fixture)
            stale = materialized.attestation.model_copy(
                update={"source_scope_sha256": "e" * 64}
            )
            with self.assertRaises(ValueError):
                verify_d3_stage_input_workspace(
                    session_root=session_root,
                    expected_attestation=stale,
                )

    def test_rejects_non_private_session_root(self) -> None:
        fixture = _fixture()
        with tempfile.TemporaryDirectory() as raw:
            session_root = Path(raw) / "session"
            session_root.mkdir(mode=0o755)
            session_root.chmod(0o755)
            with self.assertRaisesRegex(
                D3StageInputWorkspaceError,
                "PRIVATE_MODE_REQUIRED",
            ):
                _materialize(session_root, fixture)


def _materialize(session_root: Path, fixture: _Fixture):  # type: ignore[no-untyped-def]
    return materialize_d3_stage_input_workspace(
        session_root=session_root,
        d2_proposal=fixture.proposal,
        d2_validation_receipt=fixture.receipt,
        source_segments=fixture.segments,
        predecessor=fixture.predecessor,
    )


def _session_root(root: Path) -> Path:
    session_root = root / "session"
    session_root.mkdir(mode=0o700)
    session_root.chmod(0o700)
    return session_root


def _fixture() -> _Fixture:
    kit = AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.4.0",
        manifest_sha256="1" * 64,
        bundle_sha256="2" * 64,
        policy_id="reqpilotx-document-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.0.0",
    )
    d1 = D2ValidatedD1ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "3" * 32,
        artifact_sha256="4" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA256,
        status=AgentStageArtifactStatus.VALIDATED,
        proposal_sha256="5" * 64,
        evidence_sha256="6" * 64,
        validator_receipt_sha256="7" * 64,
        page_count=1,
        kit_bundle_sha256=kit.bundle_sha256,
    )
    table_map = AgentD1TableMapArtifactIdentityV1(
        source_sha256=SOURCE_SHA256,
        artifact_id="d1-table-map-synthetic",
        artifact_sha256="8" * 64,
    )
    source_content_sha256 = _sha(SECRET_SOURCE_TEXT)
    segment_id = stable_d2_source_segment_id(
        source_sha256=SOURCE_SHA256,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle="source-node-synthetic",
        start_offset=0,
        end_offset=len(SECRET_SOURCE_TEXT),
    )
    segment = D2SourceSegmentV1(
        segment_id=segment_id,
        source_sha256=SOURCE_SHA256,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle="source-node-synthetic",
        owner_source_node_id="source-node-synthetic",
        source_order=17,
        page_numbers=(1,),
        start_offset=0,
        end_offset=len(SECRET_SOURCE_TEXT),
        source_content_sha256=source_content_sha256,
        segment_sha256=stable_d2_source_segment_sha256(
            segment_id=segment_id,
            source_sha256=SOURCE_SHA256,
            handle_kind=D2SourceHandleKind.SOURCE_NODE,
            source_handle="source-node-synthetic",
            owner_source_node_id="source-node-synthetic",
            source_order=17,
            page_numbers=(1,),
            start_offset=0,
            end_offset=len(SECRET_SOURCE_TEXT),
            source_content_sha256=source_content_sha256,
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
        "source_order": 17,
        "page_numbers": (1,),
        "source_segments": (segment,),
        "logical_table_scope_ids": (),
        "reason_codes": ("synthetic_source_boundary",),
    }
    logical_object = D2LogicalObjectV1(
        **object_values,
        object_sha256=stable_d2_logical_object_sha256(**object_values),
    )
    proposal_values = {
        "run_id": RUN_ID,
        "source_sha256": SOURCE_SHA256,
        "source_ir_id": "source-ir-synthetic",
        "source_ir_canonical_sha256": "9" * 64,
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
    draft_proposal = D2LogicalStructureProposalV1.model_construct(
        **proposal_values,
        proposal_sha256="0" * 64,
    )
    proposal = D2LogicalStructureProposalV1(
        **proposal_values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft_proposal),
    )
    artifact_id = stable_d2_artifact_id(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        proposal_sha256=proposal.proposal_sha256,
    )
    receipt_values = {
        "artifact_id": artifact_id,
        "artifact_sha256": proposal.proposal_sha256,
        "run_id": proposal.run_id,
        "source_sha256": proposal.source_sha256,
        "source_ir_id": proposal.source_ir_id,
        "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
        "kit_identity": proposal.kit_identity,
        "predecessor_d1_artifact_id": proposal.predecessor_d1_artifact.artifact_id,
        "predecessor_d1_artifact_sha256": (
            proposal.predecessor_d1_artifact.artifact_sha256
        ),
        "d1_table_map_artifact_id": proposal.d1_table_map_artifact.artifact_id,
        "d1_table_map_artifact_sha256": (
            proposal.d1_table_map_artifact.artifact_sha256
        ),
        "status": proposal.status,
        "logical_object_ids": (logical_object.logical_object_id,),
        "source_segment_ids": (segment.segment_id,),
        "logical_table_scope_ids": (),
        "source_evidence_disposition_ids": (),
        "unresolved_source_evidence_disposition_ids": (),
        "physical_table_ids": (),
        "unresolved_table_ids": (),
        "proposal_sha256": proposal.proposal_sha256,
        "evidence_sha256": "a" * 64,
        "validator_id": D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
        "validator_version": D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
    }
    draft_receipt = D2LogicalStructureValidationReceiptV1.model_construct(
        **receipt_values,
        receipt_sha256="0" * 64,
    )
    receipt = D2LogicalStructureValidationReceiptV1(
        **receipt_values,
        receipt_sha256=canonical_json_sha256(
            draft_receipt.model_dump(mode="json", exclude={"receipt_sha256"})
        ),
    )
    exact_segment = D3ExactSourceSegmentV1(
        segment_id=segment.segment_id,
        logical_object_id=logical_object.logical_object_id,
        source_handle=segment.source_handle,
        handle_kind=segment.handle_kind.value,
        owner_source_node_id=segment.owner_source_node_id,
        source_order=segment.source_order,
        page_numbers=segment.page_numbers,
        handle_start_offset=segment.start_offset,
        handle_end_offset=segment.end_offset,
        source_text=SECRET_SOURCE_TEXT,
        source_text_sha256=_sha(SECRET_SOURCE_TEXT),
        source_content_sha256=segment.source_content_sha256,
        d2_segment_sha256=segment.segment_sha256,
        evidence_refs=("page-render-synthetic",),
    )
    predecessor = D3StageInputPredecessorBindingV1(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        artifact_id=receipt.artifact_id,
        artifact_sha256=receipt.artifact_sha256,
        validator_receipt_sha256=receipt.receipt_sha256,
        status=AgentStageArtifactStatus.VALIDATED,
    )
    return _Fixture(
        proposal=proposal,
        receipt=receipt,
        segments=(exact_segment,),
        predecessor=predecessor,
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
