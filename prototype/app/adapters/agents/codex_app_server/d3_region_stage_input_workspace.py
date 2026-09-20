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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
    D2LogicalStructureProposalV2,
    D2LogicalStructureValidationReceiptV2,
)
from app.application.agent_workflow.d2_table_region_parent_input import (
    D2TableRegionParentInputCompilationV1,
    D2TableRegionParentInputReceiptV1,
    D2TableRegionParentInputV1,
)
from app.application.agent_workflow.d3_region_stage_input import (
    D3RegionStageInputCompilationV1,
    D3RegionStageInputLineageV1,
    D3RegionStageInputManifestV1,
    D3UnifiedExactSourceSegmentSetV3,
    build_d3_region_stage_input_lineage,
    materialize_d3_region_stage_input,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.domain.extraction.source_ir import SourceIRSnapshot


D3_REGION_STAGE_INPUT_WORKSPACE_MANIFEST_SCHEMA_VERSION = (
    "reqpilotx-d3-region-stage-input-workspace-manifest/v1"
)
D3_REGION_STAGE_INPUT_RELATIVE_DIRECTORY = Path(
    "stage-input/d3-region-parent-provision"
)
D3_REGION_STAGE_INPUT_MANIFEST_RELATIVE_PATH = (
    D3_REGION_STAGE_INPUT_RELATIVE_DIRECTORY / "manifest.json"
)

_D2_PROPOSAL_FILE = "d2-logical-structure-proposal-v2.json"
_D2_RECEIPT_FILE = "d2-logical-structure-validation-receipt-v2.json"
_REGION_PARENT_INPUT_FILE = "d2-table-region-parent-input.json"
_REGION_PARENT_INPUT_RECEIPT_FILE = "d2-table-region-parent-input-receipt.json"
_SOURCE_SEGMENTS_FILE = "d3-unified-exact-source-segments-v3.json"
_MANIFEST_FILE = "manifest.json"
_PAYLOAD_FILES = (
    _D2_PROPOSAL_FILE,
    _D2_RECEIPT_FILE,
    _REGION_PARENT_INPUT_FILE,
    _REGION_PARENT_INPUT_RECEIPT_FILE,
    _SOURCE_SEGMENTS_FILE,
)
_WORKSPACE_FILES = (*_PAYLOAD_FILES, _MANIFEST_FILE)


class D3RegionStageInputWorkspaceError(ValueError):
    """Fail-closed error raised by the private region stage-input boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D3RegionStageInputFileDigestV1(_StrictFrozenModel):
    path: Literal[
        "d2-logical-structure-proposal-v2.json",
        "d2-logical-structure-validation-receipt-v2.json",
        "d2-table-region-parent-input.json",
        "d2-table-region-parent-input-receipt.json",
        "d3-unified-exact-source-segments-v3.json",
    ]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class D3RegionStageInputWorkspaceManifestV1(_StrictFrozenModel):
    """Path-free and source-text-free identity of the committed workspace."""

    schema_version: Literal["reqpilotx-d3-region-stage-input-workspace-manifest/v1"] = (
        D3_REGION_STAGE_INPUT_WORKSPACE_MANIFEST_SCHEMA_VERSION
    )
    attestation_id: str = Field(
        pattern=r"^d3-region-stage-input-workspace-[0-9a-f]{32}$"
    )
    content_free: Literal[True] = True
    stage: Literal[AgentExtractionStage.PARENT_PROVISION_RECOGNITION] = (
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    proposal_schema_id: Literal["logical-structure-proposal/v2"] = (
        D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    region_artifact_id: str = Field(pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$")
    region_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_input_id: str = Field(pattern=r"^d2-table-region-parent-input-[0-9a-f]{32}$")
    parent_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_input_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_input_manifest: D3RegionStageInputManifestV1
    files: tuple[D3RegionStageInputFileDigestV1, ...] = Field(
        min_length=5,
        max_length=5,
    )
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> D3RegionStageInputWorkspaceManifestV1:
        if tuple(item.path for item in self.files) != _PAYLOAD_FILES:
            raise ValueError("region stage-input files are not the canonical inventory")
        lineage = self.stage_input_manifest.lineage
        if (
            self.run_id != lineage.run_id
            or self.source_sha256 != lineage.source_sha256
            or self.kit_bundle_sha256 != lineage.kit_bundle_sha256
            or self.predecessor_artifact_id != lineage.v2_artifact_id
            or self.predecessor_artifact_sha256 != lineage.v2_artifact_sha256
            or self.predecessor_validation_receipt_sha256
            != lineage.v2_validation_receipt_sha256
            or self.region_artifact_id != lineage.region_artifact_id
            or self.region_artifact_sha256 != lineage.region_artifact_sha256
            or self.parent_input_id != lineage.parent_input_id
            or self.parent_input_sha256 != lineage.parent_input_sha256
            or self.parent_input_receipt_sha256 != lineage.parent_input_receipt_sha256
        ):
            raise ValueError("region workspace manifest lineage drifted")
        payload = self.model_dump(
            mode="json",
            exclude={"attestation_id", "bundle_sha256"},
        )
        expected = _canonical_sha256(payload)
        if self.bundle_sha256 != expected:
            raise ValueError("region stage-input workspace bundle SHA drifted")
        if self.attestation_id != (f"d3-region-stage-input-workspace-{expected[:32]}"):
            raise ValueError("region stage-input workspace attestation ID drifted")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedD3RegionStageInputWorkspace:
    workspace_path: Path
    manifest_path: Path
    attestation: D3RegionStageInputWorkspaceManifestV1


def materialize_d3_region_stage_input_workspace(
    *,
    session_root: Path,
    source_ir: SourceIRSnapshot,
    d2_proposal: D2LogicalStructureProposalV2,
    d2_validation_receipt: D2LogicalStructureValidationReceiptV2,
    parent_input: D2TableRegionParentInputV1,
    parent_input_receipt: D2TableRegionParentInputReceiptV1,
    expected_lineage: D3RegionStageInputLineageV1,
) -> MaterializedD3RegionStageInputWorkspace:
    """Atomically publish one immutable, region-capable parent-stage input."""

    try:
        compilation = materialize_d3_region_stage_input(
            source_ir,
            proposal=d2_proposal,
            validation_receipt=d2_validation_receipt,
            parent_input=parent_input,
            parent_input_receipt=parent_input_receipt,
            expected_lineage=expected_lineage,
        )
        proposal = D2LogicalStructureProposalV2.model_validate(
            d2_proposal.model_dump(mode="json")
        )
        receipt = D2LogicalStructureValidationReceiptV2.model_validate(
            d2_validation_receipt.model_dump(mode="json")
        )
        region_input = D2TableRegionParentInputV1.model_validate(
            parent_input.model_dump(mode="json")
        )
        region_receipt = D2TableRegionParentInputReceiptV1.model_validate(
            parent_input_receipt.model_dump(mode="json")
        )
    except (TypeError, ValueError) as exc:
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_WORKSPACE_PREDECESSOR_INVALID"
        ) from exc

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
        workspace = _confined_child(root, D3_REGION_STAGE_INPUT_RELATIVE_DIRECTORY)
        if _lexists(workspace):
            raise D3RegionStageInputWorkspaceError(
                "D3_REGION_STAGE_INPUT_WORKSPACE_OVERWRITE_FORBIDDEN"
            )
        workspace.mkdir(mode=0o700)
        workspace.chmod(0o700)
        workspace_created = True

        payloads: tuple[tuple[str, object], ...] = (
            (_D2_PROPOSAL_FILE, proposal.model_dump(mode="json")),
            (_D2_RECEIPT_FILE, receipt.model_dump(mode="json")),
            (_REGION_PARENT_INPUT_FILE, region_input.model_dump(mode="json")),
            (
                _REGION_PARENT_INPUT_RECEIPT_FILE,
                region_receipt.model_dump(mode="json"),
            ),
            (
                _SOURCE_SEGMENTS_FILE,
                compilation.segment_set.model_dump(mode="json"),
            ),
        )
        file_digests: list[D3RegionStageInputFileDigestV1] = []
        for filename, payload in payloads:
            encoded = _canonical_json_bytes(payload)
            _atomic_write_new_file(workspace / filename, encoded)
            file_digests.append(
                D3RegionStageInputFileDigestV1(
                    path=filename,
                    sha256=hashlib.sha256(encoded).hexdigest(),
                )
            )

        manifest_values = {
            "content_free": True,
            "stage": AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            "proposal_schema_id": D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
            "run_id": proposal.run_id,
            "source_sha256": proposal.source_sha256,
            "kit_bundle_sha256": proposal.kit_identity.bundle_sha256,
            "predecessor_artifact_id": receipt.artifact_id,
            "predecessor_artifact_sha256": receipt.artifact_sha256,
            "predecessor_validation_receipt_sha256": receipt.receipt_sha256,
            "region_artifact_id": proposal.table_region_artifact.artifact_id,
            "region_artifact_sha256": proposal.table_region_artifact.artifact_sha256,
            "parent_input_id": region_input.stage_input_id,
            "parent_input_sha256": region_input.stage_input_sha256,
            "parent_input_receipt_sha256": region_receipt.receipt_sha256,
            "stage_input_manifest": compilation.manifest,
            "files": tuple(file_digests),
        }
        draft = D3RegionStageInputWorkspaceManifestV1.model_construct(
            **manifest_values,
            attestation_id="d3-region-stage-input-workspace-" + "0" * 32,
            bundle_sha256="0" * 64,
        )
        bundle_sha256 = _canonical_sha256(
            draft.model_dump(
                mode="json",
                exclude={"attestation_id", "bundle_sha256"},
            )
        )
        attestation = D3RegionStageInputWorkspaceManifestV1(
            **manifest_values,
            attestation_id=(f"d3-region-stage-input-workspace-{bundle_sha256[:32]}"),
            bundle_sha256=bundle_sha256,
        )
        _atomic_write_new_file(
            workspace / _MANIFEST_FILE,
            _canonical_json_bytes(attestation.model_dump(mode="json")),
        )
        _fsync_directory(workspace)
        _fsync_directory(stage_input_root)
        return verify_d3_region_stage_input_workspace(
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


def verify_d3_region_stage_input_workspace(
    *,
    session_root: Path,
    expected_attestation: D3RegionStageInputWorkspaceManifestV1,
) -> MaterializedD3RegionStageInputWorkspace:
    """Reopen and verify one workspace against its out-of-band attestation."""

    expected = D3RegionStageInputWorkspaceManifestV1.model_validate(
        expected_attestation.model_dump(mode="json")
    )
    root = _private_session_root(session_root)
    workspace = _confined_child(root, D3_REGION_STAGE_INPUT_RELATIVE_DIRECTORY)
    _require_private_directory(workspace, subject="region stage-input workspace")
    inventory = tuple(sorted(item.name for item in workspace.iterdir()))
    if inventory != tuple(sorted(_WORKSPACE_FILES)):
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_WORKSPACE_INVENTORY_DRIFT"
        )
    for filename in _WORKSPACE_FILES:
        _require_private_regular_file(workspace / filename)

    manifest_path = workspace / _MANIFEST_FILE
    observed = D3RegionStageInputWorkspaceManifestV1.model_validate(
        _load_json_file(manifest_path)
    )
    if observed != expected:
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_WORKSPACE_ATTESTATION_TAMPERED"
        )
    digest_by_path = {item.path: item.sha256 for item in observed.files}
    for filename in _PAYLOAD_FILES:
        if _sha256_file(workspace / filename) != digest_by_path[filename]:
            raise D3RegionStageInputWorkspaceError(
                "D3_REGION_STAGE_INPUT_WORKSPACE_FILE_TAMPERED"
            )

    try:
        proposal = D2LogicalStructureProposalV2.model_validate(
            _load_json_file(workspace / _D2_PROPOSAL_FILE)
        )
        receipt = D2LogicalStructureValidationReceiptV2.model_validate(
            _load_json_file(workspace / _D2_RECEIPT_FILE)
        )
        parent_input = D2TableRegionParentInputV1.model_validate(
            _load_json_file(workspace / _REGION_PARENT_INPUT_FILE)
        )
        parent_receipt = D2TableRegionParentInputReceiptV1.model_validate(
            _load_json_file(workspace / _REGION_PARENT_INPUT_RECEIPT_FILE)
        )
        segment_set = D3UnifiedExactSourceSegmentSetV3.model_validate(
            _load_json_file(workspace / _SOURCE_SEGMENTS_FILE)
        )
        D2TableRegionParentInputCompilationV1(
            stage_input=parent_input,
            receipt=parent_receipt,
        )
        D3RegionStageInputCompilationV1(
            segment_set=segment_set,
            manifest=observed.stage_input_manifest,
        )
    except (TypeError, ValueError) as exc:
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_WORKSPACE_PAYLOAD_INVALID"
        ) from exc

    lineage = build_d3_region_stage_input_lineage(
        proposal=proposal,
        validation_receipt=receipt,
        parent_input=parent_input,
        parent_input_receipt=parent_receipt,
    )
    if (
        lineage != observed.stage_input_manifest.lineage
        or observed.predecessor_artifact_id != receipt.artifact_id
        or observed.predecessor_artifact_sha256 != receipt.artifact_sha256
        or observed.predecessor_validation_receipt_sha256 != receipt.receipt_sha256
        or segment_set.run_id != proposal.run_id
        or segment_set.source_sha256 != proposal.source_sha256
        or segment_set.kit_bundle_sha256 != proposal.kit_identity.bundle_sha256
        or segment_set.predecessor_v2_artifact_id != receipt.artifact_id
        or segment_set.predecessor_v2_artifact_sha256 != receipt.artifact_sha256
        or segment_set.parent_input_id != parent_input.stage_input_id
        or segment_set.parent_input_sha256 != parent_input.stage_input_sha256
    ):
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_WORKSPACE_LINEAGE_DRIFT"
        )
    return MaterializedD3RegionStageInputWorkspace(
        workspace_path=workspace,
        manifest_path=manifest_path,
        attestation=observed,
    )


def _private_session_root(session_root: Path) -> Path:
    candidate = Path(session_root)
    if not _lexists(candidate):
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_SESSION_ROOT_MISSING"
        )
    if candidate.is_symlink():
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_SYMLINK_FORBIDDEN"
        )
    resolved = candidate.resolve(strict=True)
    _require_private_directory(resolved, subject="session root")
    return resolved


def _confined_child(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise D3RegionStageInputWorkspaceError("D3_REGION_STAGE_INPUT_PATH_ESCAPE")
    candidate = root.joinpath(relative)
    resolved_parent = candidate.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_PATH_ESCAPE"
        ) from exc
    return candidate


def _require_private_directory(path: Path, *, subject: str) -> None:
    if path.is_symlink():
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_SYMLINK_FORBIDDEN"
        )
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        label = subject.upper().replace(" ", "_")
        raise D3RegionStageInputWorkspaceError(
            f"D3_REGION_STAGE_INPUT_{label}_MISSING"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_DIRECTORY_REQUIRED"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_PRIVATE_MODE_REQUIRED"
        )


def _require_private_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_SYMLINK_FORBIDDEN"
        )
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_FILE_MISSING"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_REGULAR_FILE_REQUIRED"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise D3RegionStageInputWorkspaceError("D3_REGION_STAGE_INPUT_FILE_MODE_DRIFT")


def _atomic_write_new_file(path: Path, payload: bytes) -> None:
    if _lexists(path):
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_WORKSPACE_OVERWRITE_FORBIDDEN"
        )
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
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_WORKSPACE_OVERWRITE_FORBIDDEN"
        ) from exc
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
                raise D3RegionStageInputWorkspaceError(
                    "D3_REGION_STAGE_INPUT_DUPLICATE_JSON_KEY"
                )
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_JSON_INVALID"
        ) from exc


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
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_PATH_ESCAPE"
        ) from exc
    if workspace.is_symlink():
        raise D3RegionStageInputWorkspaceError(
            "D3_REGION_STAGE_INPUT_SYMLINK_FORBIDDEN"
        )
    if workspace.exists():
        shutil.rmtree(workspace)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


__all__ = [
    "D3_REGION_STAGE_INPUT_MANIFEST_RELATIVE_PATH",
    "D3_REGION_STAGE_INPUT_RELATIVE_DIRECTORY",
    "D3_REGION_STAGE_INPUT_WORKSPACE_MANIFEST_SCHEMA_VERSION",
    "D3RegionStageInputFileDigestV1",
    "D3RegionStageInputWorkspaceError",
    "D3RegionStageInputWorkspaceManifestV1",
    "MaterializedD3RegionStageInputWorkspace",
    "materialize_d3_region_stage_input_workspace",
    "verify_d3_region_stage_input_workspace",
]
