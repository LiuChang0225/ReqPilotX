"""Private stage input for one complete Document Map scope replacement."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.application.agent_workflow.document_map_replacement import (
    DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE,
    DocumentMapScopeReplacementRequestV1,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    canonical_json_sha256,
)


DOCUMENT_MAP_REPLACEMENT_STAGE_INPUT_SCHEMA_VERSION = (
    "canonical-document-map-replacement-stage-input/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CanonicalDocumentMapReplacementInputFileV1(_StrictFrozenModel):
    path: Literal["scope-replacement-request.json"] = (
        "scope-replacement-request.json"
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CanonicalDocumentMapReplacementStageInputAttestationV1(_StrictFrozenModel):
    schema_version: Literal[
        "canonical-document-map-replacement-stage-input/v1"
    ] = DOCUMENT_MAP_REPLACEMENT_STAGE_INPUT_SCHEMA_VERSION
    content_bearing: Literal[True] = True
    private_workspace_only: Literal[True] = True
    execution_mode: Literal["DOCUMENT_MAP_SCOPE_REPLACEMENT"] = (
        DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_workspace_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    survey_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    scope_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file: CanonicalDocumentMapReplacementInputFileV1
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_manifest(
        self,
    ) -> CanonicalDocumentMapReplacementStageInputAttestationV1:
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if self.manifest_sha256 != expected:
            raise ValueError("document-map replacement stage-input SHA drifted")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedCanonicalDocumentMapReplacementStageInput:
    root: Path
    manifest_path: Path
    request_path: Path
    attestation: CanonicalDocumentMapReplacementStageInputAttestationV1


def materialize_document_map_replacement_stage_input(
    *,
    session_root: Path,
    request: DocumentMapScopeReplacementRequestV1,
) -> MaterializedCanonicalDocumentMapReplacementStageInput:
    request = DocumentMapScopeReplacementRequestV1.model_validate(
        request.model_dump(mode="json")
    )
    scope = request.scope_request
    headings = request.heading_compilation
    root = session_root / "document-map-replacement-stage-input"
    root.mkdir(mode=0o700)
    request_path = root / "scope-replacement-request.json"
    request_bytes = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _write_private_read_only(request_path, request_bytes)
    values = {
        "run_id": scope.run_id,
        "source_sha256": scope.source_sha256,
        "source_ir_id": scope.source_ir_id,
        "source_ir_canonical_sha256": scope.source_ir_canonical_sha256,
        "evidence_workspace_bundle_sha256": (
            scope.evidence_workspace_bundle_sha256
        ),
        "survey_compilation_sha256": scope.survey_compilation_sha256,
        "scope_id": scope.scope_id,
        "scope_request_sha256": scope.request_sha256,
        "heading_compilation_sha256": headings.compilation_sha256,
        "replacement_request_sha256": request.request_sha256,
        "file": CanonicalDocumentMapReplacementInputFileV1(
            sha256=hashlib.sha256(request_bytes).hexdigest()
        ),
    }
    draft = CanonicalDocumentMapReplacementStageInputAttestationV1.model_construct(
        **values,
        manifest_sha256="0" * 64,
    )
    attestation = CanonicalDocumentMapReplacementStageInputAttestationV1(
        **values,
        manifest_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"manifest_sha256"})
        ),
    )
    manifest_path = root / "manifest.json"
    _write_private_read_only(
        manifest_path,
        json.dumps(
            attestation.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    return MaterializedCanonicalDocumentMapReplacementStageInput(
        root=root,
        manifest_path=manifest_path,
        request_path=request_path,
        attestation=attestation,
    )


def _write_private_read_only(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o400)


__all__ = [
    "CanonicalDocumentMapReplacementStageInputAttestationV1",
    "DOCUMENT_MAP_REPLACEMENT_STAGE_INPUT_SCHEMA_VERSION",
    "MaterializedCanonicalDocumentMapReplacementStageInput",
    "materialize_document_map_replacement_stage_input",
]
