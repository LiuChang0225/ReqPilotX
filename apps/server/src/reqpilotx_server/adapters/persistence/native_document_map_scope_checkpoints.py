"""Private ObjectStore persistence for detailed Document Map scope checkpoints."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.application.agent_workflow.document_map_scope import (
    DocumentMapHeadingScopeCompilationV1,
    DocumentMapScopeRequestV1,
)
from reqpilotx_server.application.agent_workflow.document_map_replacement import (
    DocumentMapScopeReplacementCompilationV1,
    DocumentMapScopeReplacementRequestV1,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    canonical_json_sha256,
)
from reqpilotx_server.ports.object_store import ObjectStore


SCOPE_CHECKPOINT_RECEIPT_SCHEMA_VERSION = (
    "native-document-map-scope-checkpoint-receipt/v1"
)
SCOPE_CHECKPOINT_MEDIA_TYPE = (
    "application/vnd.reqpilotx.document-map-scope-checkpoint+json"
)


class NativeDocumentMapScopeCheckpointReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal[
        "native-document-map-scope-checkpoint-receipt/v1"
    ] = SCOPE_CHECKPOINT_RECEIPT_SCHEMA_VERSION
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    survey_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_node_count: int = Field(ge=1, le=4096)
    checkpoint_kind: Literal["HEADING_SCOPE", "COMPLETE_SCOPE_REPLACEMENT"] = Field(
        default="HEADING_SCOPE",
        exclude_if=lambda value: value == "HEADING_SCOPE",
    )
    logical_table_count: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    text_ownership_count: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    asset_placement_count: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    object_key: str = Field(min_length=1, max_length=2000)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_size: int = Field(ge=1)
    media_type: Literal[
        "application/vnd.reqpilotx.document-map-scope-checkpoint+json"
    ] = SCOPE_CHECKPOINT_MEDIA_TYPE
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> NativeDocumentMapScopeCheckpointReceiptV1:
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("document-map scope checkpoint receipt SHA drifted")
        return self


class NativeDocumentMapScopeCheckpointStore:
    """Persist one Host-compiled scope; never publish it as a complete Map."""

    def __init__(
        self,
        *,
        object_store: ObjectStore,
        project_id: str,
        document_id: str,
    ) -> None:
        self._object_store = object_store
        self._project_id = _safe_path_token(project_id, subject="project")
        self._document_id = _safe_path_token(document_id, subject="document")

    def save(
        self,
        *,
        request: DocumentMapScopeRequestV1 | DocumentMapScopeReplacementRequestV1,
        compilation: DocumentMapHeadingScopeCompilationV1
        | DocumentMapScopeReplacementCompilationV1,
        kit_bundle_sha256: str,
    ) -> NativeDocumentMapScopeCheckpointReceiptV1:
        replacement = isinstance(request, DocumentMapScopeReplacementRequestV1)
        request = type(request).model_validate(request.model_dump(mode="json"))
        compilation = type(compilation).model_validate(compilation.model_dump(mode="json"))
        scope = request.scope_request if replacement else request
        if replacement != isinstance(compilation, DocumentMapScopeReplacementCompilationV1):
            raise ValueError("document-map checkpoint request/compilation kind drifted")
        request_sha256 = request.request_sha256
        compiled_request_sha256 = (
            compilation.replacement_request_sha256
            if replacement
            else compilation.request_sha256
        )
        if (
            re.fullmatch(r"[0-9a-f]{64}", kit_bundle_sha256) is None
            or compilation.document_map_complete
            or not compilation.private_checkpoint_only
            or compilation.source_sha256 != scope.source_sha256
            or compilation.survey_compilation_sha256
            != scope.survey_compilation_sha256
            or compilation.scope_id != scope.scope_id
            or compiled_request_sha256 != request_sha256
            or (
                not replacement
                and compilation.accepted_source_span != scope.suggested_source_span
            )
            or (
                replacement
                and compilation.heading_compilation_sha256
                != request.heading_compilation.compilation_sha256
            )
        ):
            raise ValueError("document-map scope checkpoint lineage drifted")
        encoded = json.dumps(
            compilation.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object_sha256 = hashlib.sha256(encoded).hexdigest()
        object_key = (
            "runtime/agent-document-map-scope-checkpoints/"
            f"{self._project_id}/{self._document_id}/{scope.source_sha256}/"
            f"{scope.run_id}/{scope.scope_id}/{compilation.compilation_sha256}.json"
        )
        stored = self._object_store.put(
            key=object_key,
            source=BytesIO(encoded),
            expected_sha256=object_sha256,
            media_type=SCOPE_CHECKPOINT_MEDIA_TYPE,
        )
        if (
            stored.key != object_key
            or stored.sha256 != object_sha256
            or stored.size != len(encoded)
            or stored.media_type != SCOPE_CHECKPOINT_MEDIA_TYPE
        ):
            raise ValueError("document-map scope checkpoint ObjectStore receipt drifted")
        values = {
            "run_id": scope.run_id,
            "source_sha256": scope.source_sha256,
            "kit_bundle_sha256": kit_bundle_sha256,
            "survey_compilation_sha256": compilation.survey_compilation_sha256,
            "scope_id": compilation.scope_id,
            "request_sha256": compiled_request_sha256,
            "candidate_sha256": compilation.candidate_sha256,
            "compilation_sha256": compilation.compilation_sha256,
            "heading_node_count": len(compilation.heading_nodes),
            "checkpoint_kind": (
                "COMPLETE_SCOPE_REPLACEMENT" if replacement else "HEADING_SCOPE"
            ),
            "logical_table_count": len(compilation.logical_tables) if replacement else 0,
            "text_ownership_count": len(compilation.text_ownership) if replacement else 0,
            "asset_placement_count": len(compilation.asset_ownership) if replacement else 0,
            "object_key": stored.key,
            "object_sha256": stored.sha256,
            "object_size": stored.size,
            "media_type": stored.media_type,
        }
        draft = NativeDocumentMapScopeCheckpointReceiptV1.model_construct(
            **values,
            receipt_sha256="0" * 64,
        )
        return NativeDocumentMapScopeCheckpointReceiptV1(
            **values,
            receipt_sha256=canonical_json_sha256(
                draft.model_dump(mode="json", exclude={"receipt_sha256"})
            ),
        )

    def load(
        self,
        receipt: NativeDocumentMapScopeCheckpointReceiptV1,
    ) -> DocumentMapHeadingScopeCompilationV1 | DocumentMapScopeReplacementCompilationV1:
        receipt = NativeDocumentMapScopeCheckpointReceiptV1.model_validate(
            receipt.model_dump(mode="json")
        )
        expected_prefix = (
            "runtime/agent-document-map-scope-checkpoints/"
            f"{self._project_id}/{self._document_id}/{receipt.source_sha256}/"
            f"{receipt.run_id}/{receipt.scope_id}/"
        )
        if not receipt.object_key.startswith(expected_prefix):
            raise ValueError("document-map scope checkpoint object key drifted")
        try:
            stat = self._object_store.stat(receipt.object_key)
            with self._object_store.open(receipt.object_key) as stream:
                encoded = stream.read()
        except OSError as exc:
            raise ValueError("document-map scope checkpoint object missing") from exc
        if (
            stat is None
            or stat.sha256 != receipt.object_sha256
            or stat.size != receipt.object_size
            or len(encoded) != receipt.object_size
            or hashlib.sha256(encoded).hexdigest() != receipt.object_sha256
        ):
            raise ValueError("document-map scope checkpoint object SHA drifted")
        replacement = receipt.checkpoint_kind == "COMPLETE_SCOPE_REPLACEMENT"
        compilation = (
            DocumentMapScopeReplacementCompilationV1.model_validate_json(encoded)
            if replacement
            else DocumentMapHeadingScopeCompilationV1.model_validate_json(encoded)
        )
        compiled_request_sha256 = (
            compilation.replacement_request_sha256
            if replacement
            else compilation.request_sha256
        )
        if (
            compilation.source_sha256 != receipt.source_sha256
            or compilation.survey_compilation_sha256
            != receipt.survey_compilation_sha256
            or compilation.scope_id != receipt.scope_id
            or compiled_request_sha256 != receipt.request_sha256
            or compilation.candidate_sha256 != receipt.candidate_sha256
            or compilation.compilation_sha256 != receipt.compilation_sha256
            or len(compilation.heading_nodes) != receipt.heading_node_count
            or (
                replacement
                and (
                    len(compilation.logical_tables) != receipt.logical_table_count
                    or len(compilation.text_ownership) != receipt.text_ownership_count
                    or len(compilation.asset_ownership) != receipt.asset_placement_count
                )
            )
        ):
            raise ValueError("document-map scope checkpoint receipt lineage drifted")
        return compilation


def _safe_path_token(value: str, *, subject: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in normalized
        )
    ):
        raise ValueError(f"document-map scope checkpoint {subject} ID is invalid")
    return normalized


__all__ = [
    "NativeDocumentMapScopeCheckpointReceiptV1",
    "NativeDocumentMapScopeCheckpointStore",
    "SCOPE_CHECKPOINT_MEDIA_TYPE",
    "SCOPE_CHECKPOINT_RECEIPT_SCHEMA_VERSION",
]
