from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from reqpilotx_server.application.agent_workflow.d2_logical_structure import (
    D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV1,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    compile_validated_d2_identity,
    d3_source_scope_sha256,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentStageArtifactStatus,
)


D3_STAGE_INPUT_PREDECESSOR_BINDING_SCHEMA_VERSION = (
    "reqpilotx-d3-stage-input-predecessor-binding/v1"
)
D3_EXACT_SOURCE_SEGMENT_SET_SCHEMA_VERSION = "reqpilotx-d3-exact-source-segment-set/v2"
D3_STAGE_INPUT_BUNDLE_ATTESTATION_SCHEMA_VERSION = (
    "reqpilotx-d3-stage-input-bundle-attestation/v1"
)
D3_STAGE_INPUT_RELATIVE_DIRECTORY = Path("stage-input/d3-parent-provision")
D3_STAGE_INPUT_MANIFEST_RELATIVE_PATH = (
    D3_STAGE_INPUT_RELATIVE_DIRECTORY / "manifest.json"
)

_D2_PROPOSAL_FILE = "d2-logical-structure-proposal.json"
_D2_RECEIPT_FILE = "d2-logical-structure-validation-receipt.json"
_SOURCE_SEGMENTS_FILE = "d3-exact-source-segments.json"
_MANIFEST_FILE = "manifest.json"
_PAYLOAD_FILES = (
    _D2_PROPOSAL_FILE,
    _D2_RECEIPT_FILE,
    _SOURCE_SEGMENTS_FILE,
)
_WORKSPACE_FILES = (*_PAYLOAD_FILES, _MANIFEST_FILE)


class D3StageInputWorkspaceError(ValueError):
    """Fail-closed error raised by the immutable D3 input boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D3StageInputPredecessorBindingV1(_StrictFrozenModel):
    """Content-free D2 binding accepted by the D3 input materializer."""

    schema_version: Literal["reqpilotx-d3-stage-input-predecessor-binding/v1"] = (
        D3_STAGE_INPUT_PREDECESSOR_BINDING_SCHEMA_VERSION
    )
    stage: Literal[AgentExtractionStage.PARENT_PROVISION_RECOGNITION] = (
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    predecessor_stage: Literal[AgentExtractionStage.LOGICAL_STRUCTURE] = (
        AgentExtractionStage.LOGICAL_STRUCTURE
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_schema_id: Literal["logical-structure-proposal/v1"] = (
        D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION
    )
    status: Literal[
        AgentStageArtifactStatus.VALIDATED,
        AgentStageArtifactStatus.PARTIAL,
    ]


class D3SegmentOrdinalLedgerEntryV1(_StrictFrozenModel):
    """Host-derived ordinal binding for compact D3 source spans."""

    segment_ordinal: StrictInt = Field(ge=0)
    segment_id: str = Field(min_length=1, max_length=200)
    logical_object_id: str = Field(min_length=1, max_length=200)
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = Field(min_length=1)
    source_text_length: StrictInt = Field(ge=0)
    d2_segment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class D3ExactSourceSegmentSetV2(_StrictFrozenModel):
    """The only customer-content-bearing file in the D3 stage-input bundle."""

    schema_version: Literal["reqpilotx-d3-exact-source-segment-set/v2"] = (
        D3_EXACT_SOURCE_SEGMENT_SET_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_d2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_d2_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_ordinal_ledger: tuple[D3SegmentOrdinalLedgerEntryV1, ...] = Field(
        min_length=1
    )
    segments: tuple[D3ExactSourceSegmentV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> D3ExactSourceSegmentSetV2:
        segment_ids = tuple(item.segment_id for item in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("D3 stage-input source segments must be unique")
        expected_ledger = _segment_ordinal_ledger(self.segments)
        if self.segment_ordinal_ledger != expected_ledger:
            raise ValueError("D3 stage-input segment ordinal ledger drifted")
        if self.source_scope_sha256 != d3_source_scope_sha256(self.segments):
            raise ValueError("D3 stage-input source-scope SHA drifted")
        return self


class D3StageInputFileDigestV1(_StrictFrozenModel):
    path: Literal[
        "d2-logical-structure-proposal.json",
        "d2-logical-structure-validation-receipt.json",
        "d3-exact-source-segments.json",
    ]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class D3StageInputBundleAttestationV1(_StrictFrozenModel):
    """Path-free and source-text-free identity of one committed input bundle."""

    schema_version: Literal["reqpilotx-d3-stage-input-bundle-attestation/v1"] = (
        D3_STAGE_INPUT_BUNDLE_ATTESTATION_SCHEMA_VERSION
    )
    attestation_id: str = Field(pattern=r"^d3-stage-input-[0-9a-f]{32}$")
    content_free: Literal[True] = True
    stage: Literal[AgentExtractionStage.PARENT_PROVISION_RECOGNITION] = (
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor: D3StageInputPredecessorBindingV1
    d2_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[D3StageInputFileDigestV1, ...] = Field(
        min_length=3,
        max_length=3,
    )
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bundle(self) -> D3StageInputBundleAttestationV1:
        if tuple(item.path for item in self.files) != _PAYLOAD_FILES:
            raise ValueError("D3 stage-input files are not the canonical inventory")
        payload = self.model_dump(
            mode="json",
            exclude={"attestation_id", "bundle_sha256"},
        )
        expected_sha256 = _canonical_sha256(payload)
        if self.bundle_sha256 != expected_sha256:
            raise ValueError("D3 stage-input bundle SHA drifted")
        if self.attestation_id != f"d3-stage-input-{expected_sha256[:32]}":
            raise ValueError("D3 stage-input attestation ID drifted")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedD3StageInputWorkspace:
    workspace_path: Path
    manifest_path: Path
    attestation: D3StageInputBundleAttestationV1


def materialize_d3_stage_input_workspace(
    *,
    session_root: Path,
    d2_proposal: D2LogicalStructureProposalV1,
    d2_validation_receipt: D2LogicalStructureValidationReceiptV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    predecessor: D3StageInputPredecessorBindingV1,
) -> MaterializedD3StageInputWorkspace:
    """Commit one immutable D3 input bundle below a private session root.

    The manifest is written last and is the publication marker.  No caller may
    supply paths inside the bundle, and an existing target is never reused.
    """

    proposal, receipt, exact_segments, predecessor_binding = _validate_inputs(
        d2_proposal=d2_proposal,
        d2_validation_receipt=d2_validation_receipt,
        source_segments=source_segments,
        predecessor=predecessor,
    )
    d2_identity = compile_validated_d2_identity(
        d2_proposal=proposal,
        d2_receipt=receipt,
        source_segments=exact_segments,
    )
    _validate_predecessor(predecessor_binding, proposal, receipt)

    root = _private_session_root(session_root)
    stage_input_root = _confined_child(root, Path("stage-input"))
    stage_input_created = False
    workspace_created = False
    workspace: Path | None = None
    try:
        if _lexists(stage_input_root):
            _require_private_directory(stage_input_root, subject="stage-input root")
        else:
            stage_input_root.mkdir(mode=0o700)
            stage_input_root.chmod(0o700)
            stage_input_created = True
        workspace = _confined_child(root, D3_STAGE_INPUT_RELATIVE_DIRECTORY)
        if _lexists(workspace):
            raise D3StageInputWorkspaceError("D3_STAGE_INPUT_OVERWRITE_FORBIDDEN")
        workspace.mkdir(mode=0o700)
        workspace.chmod(0o700)
        workspace_created = True

        segment_set = D3ExactSourceSegmentSetV2(
            run_id=proposal.run_id,
            source_sha256=proposal.source_sha256,
            predecessor_d2_artifact_id=receipt.artifact_id,
            predecessor_d2_artifact_sha256=receipt.artifact_sha256,
            source_scope_sha256=d2_identity.source_scope_sha256,
            segment_ordinal_ledger=_segment_ordinal_ledger(exact_segments),
            segments=exact_segments,
        )
        payloads: tuple[tuple[str, object], ...] = (
            (_D2_PROPOSAL_FILE, proposal.model_dump(mode="json")),
            (_D2_RECEIPT_FILE, receipt.model_dump(mode="json")),
            (_SOURCE_SEGMENTS_FILE, segment_set.model_dump(mode="json")),
        )
        file_digests: list[D3StageInputFileDigestV1] = []
        for filename, payload in payloads:
            encoded = _canonical_json_bytes(payload)
            _atomic_write_new_file(workspace / filename, encoded)
            file_digests.append(
                D3StageInputFileDigestV1(
                    path=filename,
                    sha256=hashlib.sha256(encoded).hexdigest(),
                )
            )

        attestation_payload = {
            "content_free": True,
            "stage": AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            "run_id": proposal.run_id,
            "source_sha256": proposal.source_sha256,
            "kit_bundle_sha256": proposal.kit_identity.bundle_sha256,
            "predecessor": predecessor_binding,
            "d2_identity_sha256": _canonical_sha256(
                d2_identity.model_dump(mode="json")
            ),
            "source_scope_sha256": d2_identity.source_scope_sha256,
            "files": tuple(file_digests),
        }
        serializable = D3StageInputBundleAttestationV1.model_construct(
            attestation_id="d3-stage-input-" + "0" * 32,
            bundle_sha256="0" * 64,
            **attestation_payload,
        ).model_dump(
            mode="json",
            exclude={"attestation_id", "bundle_sha256"},
        )
        bundle_sha256 = _canonical_sha256(serializable)
        attestation = D3StageInputBundleAttestationV1(
            attestation_id=f"d3-stage-input-{bundle_sha256[:32]}",
            bundle_sha256=bundle_sha256,
            **attestation_payload,
        )
        _atomic_write_new_file(
            workspace / _MANIFEST_FILE,
            _canonical_json_bytes(attestation.model_dump(mode="json")),
        )
        _fsync_directory(workspace)
        _fsync_directory(stage_input_root)
        return verify_d3_stage_input_workspace(
            session_root=root,
            expected_attestation=attestation,
        )
    except BaseException:
        if workspace_created and workspace is not None:
            _remove_unpublished_workspace(workspace, session_root=root)
        if stage_input_created:
            try:
                stage_input_root.rmdir()
            except OSError:
                pass
        raise


def verify_d3_stage_input_workspace(
    *,
    session_root: Path,
    expected_attestation: D3StageInputBundleAttestationV1,
) -> MaterializedD3StageInputWorkspace:
    """Verify a materialized bundle against its out-of-band content-free pin."""

    expected = D3StageInputBundleAttestationV1.model_validate(
        expected_attestation.model_dump(mode="json")
    )
    root = _private_session_root(session_root)
    workspace = _confined_child(root, D3_STAGE_INPUT_RELATIVE_DIRECTORY)
    _require_private_directory(workspace, subject="D3 stage-input workspace")
    inventory = tuple(sorted(item.name for item in workspace.iterdir()))
    if inventory != tuple(sorted(_WORKSPACE_FILES)):
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_INVENTORY_DRIFT")
    for filename in _WORKSPACE_FILES:
        _require_private_regular_file(workspace / filename)

    manifest_path = workspace / _MANIFEST_FILE
    observed = D3StageInputBundleAttestationV1.model_validate(
        _load_json_file(manifest_path)
    )
    if observed != expected:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_ATTESTATION_TAMPERED")

    digest_by_path = {item.path: item.sha256 for item in observed.files}
    for filename in _PAYLOAD_FILES:
        if _sha256_file(workspace / filename) != digest_by_path[filename]:
            raise D3StageInputWorkspaceError("D3_STAGE_INPUT_FILE_TAMPERED")

    proposal = D2LogicalStructureProposalV1.model_validate(
        _load_json_file(workspace / _D2_PROPOSAL_FILE)
    )
    receipt = D2LogicalStructureValidationReceiptV1.model_validate(
        _load_json_file(workspace / _D2_RECEIPT_FILE)
    )
    segment_set = D3ExactSourceSegmentSetV2.model_validate(
        _load_json_file(workspace / _SOURCE_SEGMENTS_FILE)
    )
    d2_identity = compile_validated_d2_identity(
        d2_proposal=proposal,
        d2_receipt=receipt,
        source_segments=segment_set.segments,
    )
    _validate_predecessor(observed.predecessor, proposal, receipt)
    if (
        segment_set.run_id != proposal.run_id
        or segment_set.source_sha256 != proposal.source_sha256
        or segment_set.predecessor_d2_artifact_id != receipt.artifact_id
        or segment_set.predecessor_d2_artifact_sha256 != receipt.artifact_sha256
        or segment_set.source_scope_sha256 != d2_identity.source_scope_sha256
        or observed.run_id != proposal.run_id
        or observed.source_sha256 != proposal.source_sha256
        or observed.kit_bundle_sha256 != proposal.kit_identity.bundle_sha256
        or observed.source_scope_sha256 != d2_identity.source_scope_sha256
        or observed.d2_identity_sha256
        != _canonical_sha256(d2_identity.model_dump(mode="json"))
    ):
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_BUNDLE_LINEAGE_DRIFT")
    return MaterializedD3StageInputWorkspace(
        workspace_path=workspace,
        manifest_path=manifest_path,
        attestation=observed,
    )


def _validate_inputs(
    *,
    d2_proposal: D2LogicalStructureProposalV1,
    d2_validation_receipt: D2LogicalStructureValidationReceiptV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    predecessor: D3StageInputPredecessorBindingV1,
) -> tuple[
    D2LogicalStructureProposalV1,
    D2LogicalStructureValidationReceiptV1,
    tuple[D3ExactSourceSegmentV1, ...],
    D3StageInputPredecessorBindingV1,
]:
    try:
        proposal = D2LogicalStructureProposalV1.model_validate(
            d2_proposal.model_dump(mode="json")
        )
        receipt = D2LogicalStructureValidationReceiptV1.model_validate(
            d2_validation_receipt.model_dump(mode="json")
        )
        exact_segments = tuple(
            D3ExactSourceSegmentV1.model_validate(item.model_dump(mode="json"))
            for item in source_segments
        )
        predecessor_binding = D3StageInputPredecessorBindingV1.model_validate(
            predecessor.model_dump(mode="json")
        )
        compile_validated_d2_identity(
            d2_proposal=proposal,
            d2_receipt=receipt,
            source_segments=exact_segments,
        )
    except (TypeError, ValueError) as exc:
        raise D3StageInputWorkspaceError(
            "D3_STAGE_INPUT_PREDECESSOR_CLOSURE_INVALID"
        ) from exc
    return proposal, receipt, exact_segments, predecessor_binding


def _segment_ordinal_ledger(
    segments: tuple[D3ExactSourceSegmentV1, ...],
) -> tuple[D3SegmentOrdinalLedgerEntryV1, ...]:
    return tuple(
        D3SegmentOrdinalLedgerEntryV1(
            segment_ordinal=ordinal,
            segment_id=segment.segment_id,
            logical_object_id=segment.logical_object_id,
            source_order=segment.source_order,
            page_numbers=segment.page_numbers,
            source_text_length=len(segment.source_text),
            d2_segment_sha256=segment.d2_segment_sha256,
        )
        for ordinal, segment in enumerate(segments)
    )


def _validate_predecessor(
    predecessor: D3StageInputPredecessorBindingV1,
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
) -> None:
    expected_status = (
        AgentStageArtifactStatus.VALIDATED
        if proposal.status is D2LogicalStructureStatus.COMPLETE
        else AgentStageArtifactStatus.PARTIAL
    )
    if (
        predecessor.run_id != proposal.run_id
        or predecessor.source_sha256 != proposal.source_sha256
        or predecessor.artifact_id != receipt.artifact_id
        or predecessor.artifact_sha256 != receipt.artifact_sha256
        or predecessor.validator_receipt_sha256 != receipt.receipt_sha256
        or predecessor.status is not expected_status
    ):
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_BINDING_MISMATCH")


def _private_session_root(session_root: Path) -> Path:
    candidate = Path(session_root)
    if not _lexists(candidate):
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_SESSION_ROOT_MISSING")
    if candidate.is_symlink():
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_SYMLINK_FORBIDDEN")
    resolved = candidate.resolve(strict=True)
    _require_private_directory(resolved, subject="session root")
    return resolved


def _confined_child(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_PATH_ESCAPE")
    candidate = root.joinpath(relative)
    resolved_parent = candidate.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_PATH_ESCAPE") from exc
    return candidate


def _require_private_directory(path: Path, *, subject: str) -> None:
    if path.is_symlink():
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_SYMLINK_FORBIDDEN")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise D3StageInputWorkspaceError(
            f"D3_STAGE_INPUT_{subject.upper().replace(' ', '_')}_MISSING"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_DIRECTORY_REQUIRED")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_PRIVATE_MODE_REQUIRED")


def _require_private_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_SYMLINK_FORBIDDEN")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_FILE_MISSING") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_REGULAR_FILE_REQUIRED")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_FILE_MODE_DRIFT")


def _atomic_write_new_file(path: Path, payload: bytes) -> None:
    if _lexists(path):
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_OVERWRITE_FORBIDDEN")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    temporary.chmod(0o600)
    try:
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError as exc:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_OVERWRITE_FORBIDDEN") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_json_file(path: Path) -> object:
    raw = path.read_bytes()

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise D3StageInputWorkspaceError("D3_STAGE_INPUT_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_JSON_INVALID") from exc


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_unpublished_workspace(workspace: Path, *, session_root: Path) -> None:
    try:
        workspace.resolve(strict=False).relative_to(session_root)
    except ValueError as exc:  # pragma: no cover - fixed path plus earlier checks.
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_PATH_ESCAPE") from exc
    if workspace.is_symlink():
        raise D3StageInputWorkspaceError("D3_STAGE_INPUT_SYMLINK_FORBIDDEN")
    if workspace.exists():
        shutil.rmtree(workspace)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


__all__ = [
    "D3_STAGE_INPUT_BUNDLE_ATTESTATION_SCHEMA_VERSION",
    "D3_STAGE_INPUT_MANIFEST_RELATIVE_PATH",
    "D3_STAGE_INPUT_PREDECESSOR_BINDING_SCHEMA_VERSION",
    "D3_STAGE_INPUT_RELATIVE_DIRECTORY",
    "D3ExactSourceSegmentSetV2",
    "D3SegmentOrdinalLedgerEntryV1",
    "D3StageInputBundleAttestationV1",
    "D3StageInputFileDigestV1",
    "D3StageInputPredecessorBindingV1",
    "D3StageInputWorkspaceError",
    "MaterializedD3StageInputWorkspace",
    "materialize_d3_stage_input_workspace",
    "verify_d3_stage_input_workspace",
]
