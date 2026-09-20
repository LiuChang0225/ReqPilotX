"""Private ObjectStore persistence for Agent-owned document survey checkpoints."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCompilationV1,
)
from app.application.agent_workflow.document_understanding import (
    canonical_json_sha256,
)
from app.ports.object_store import ObjectStore


CHECKPOINT_RECEIPT_SCHEMA_VERSION = (
    "native-document-map-survey-checkpoint-receipt/v1"
)
CHECKPOINT_MEDIA_TYPE = (
    "application/vnd.reqpilotx.document-map-survey-checkpoint+json"
)


class NativeDocumentMapSurveyCheckpointReceiptV1(BaseModel):
    """Content-free pointer safe for durable progress and resume lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal[
        "native-document-map-survey-checkpoint-receipt/v1"
    ] = CHECKPOINT_RECEIPT_SCHEMA_VERSION
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_coverage_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_ids: tuple[str, ...] = Field(min_length=1, max_length=250)
    object_key: str = Field(min_length=1, max_length=2000)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_size: int = Field(ge=1)
    media_type: Literal[
        "application/vnd.reqpilotx.document-map-survey-checkpoint+json"
    ] = CHECKPOINT_MEDIA_TYPE
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> NativeDocumentMapSurveyCheckpointReceiptV1:
        if len(self.scope_ids) != len(set(self.scope_ids)):
            raise ValueError("survey checkpoint scope IDs must be unique")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("survey checkpoint receipt SHA drifted")
        return self


class NativeDocumentMapSurveyCheckpointStore:
    """Persist one validated survey without publishing customer content."""

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
        compilation: DocumentMapSurveyCompilationV1,
    ) -> NativeDocumentMapSurveyCheckpointReceiptV1:
        compilation = DocumentMapSurveyCompilationV1.model_validate(
            compilation.model_dump(mode="json")
        )
        encoded = json.dumps(
            compilation.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object_sha256 = hashlib.sha256(encoded).hexdigest()
        object_key = self._object_prefix(compilation) + (
            f"{compilation.compilation_sha256}.json"
        )
        stored = self._object_store.put(
            key=object_key,
            source=BytesIO(encoded),
            expected_sha256=object_sha256,
            media_type=CHECKPOINT_MEDIA_TYPE,
        )
        if (
            stored.key != object_key
            or stored.sha256 != object_sha256
            or stored.size != len(encoded)
            or stored.media_type != CHECKPOINT_MEDIA_TYPE
        ):
            raise ValueError("survey checkpoint ObjectStore receipt drifted")
        values = {
            "run_id": compilation.run_id,
            "source_sha256": compilation.source_sha256,
            "kit_bundle_sha256": compilation.kit_identity.bundle_sha256,
            "candidate_sha256": compilation.candidate_sha256,
            "visual_coverage_receipt_sha256": (
                compilation.visual_coverage_receipt_sha256
            ),
            "compilation_sha256": compilation.compilation_sha256,
            "scope_ids": tuple(item.scope_id for item in compilation.scopes),
            "object_key": stored.key,
            "object_sha256": stored.sha256,
            "object_size": stored.size,
            "media_type": stored.media_type,
        }
        draft = NativeDocumentMapSurveyCheckpointReceiptV1.model_construct(
            **values,
            receipt_sha256="0" * 64,
        )
        return NativeDocumentMapSurveyCheckpointReceiptV1(
            **values,
            receipt_sha256=canonical_json_sha256(
                draft.model_dump(mode="json", exclude={"receipt_sha256"})
            ),
        )

    def load(
        self,
        receipt: NativeDocumentMapSurveyCheckpointReceiptV1,
    ) -> DocumentMapSurveyCompilationV1:
        receipt = NativeDocumentMapSurveyCheckpointReceiptV1.model_validate(
            receipt.model_dump(mode="json")
        )
        expected_prefix = (
            "runtime/agent-document-map-survey-checkpoints/"
            f"{self._project_id}/{self._document_id}/{receipt.source_sha256}/"
            f"{receipt.run_id}/"
        )
        if not receipt.object_key.startswith(expected_prefix):
            raise ValueError("survey checkpoint object key drifted")
        try:
            stat = self._object_store.stat(receipt.object_key)
            with self._object_store.open(receipt.object_key) as stream:
                encoded = stream.read()
        except OSError as exc:
            raise ValueError("survey checkpoint object missing") from exc
        if (
            stat is None
            or stat.sha256 != receipt.object_sha256
            or stat.size != receipt.object_size
            or len(encoded) != receipt.object_size
            or hashlib.sha256(encoded).hexdigest() != receipt.object_sha256
        ):
            raise ValueError("survey checkpoint object SHA drifted")
        compilation = DocumentMapSurveyCompilationV1.model_validate_json(encoded)
        if (
            compilation.run_id != receipt.run_id
            or compilation.source_sha256 != receipt.source_sha256
            or compilation.kit_identity.bundle_sha256
            != receipt.kit_bundle_sha256
            or compilation.candidate_sha256 != receipt.candidate_sha256
            or compilation.visual_coverage_receipt_sha256
            != receipt.visual_coverage_receipt_sha256
            or compilation.compilation_sha256 != receipt.compilation_sha256
            or tuple(item.scope_id for item in compilation.scopes)
            != receipt.scope_ids
        ):
            raise ValueError("survey checkpoint receipt lineage drifted")
        return compilation

    def _object_prefix(self, compilation: DocumentMapSurveyCompilationV1) -> str:
        return (
            "runtime/agent-document-map-survey-checkpoints/"
            f"{self._project_id}/{self._document_id}/{compilation.source_sha256}/"
            f"{compilation.run_id}/"
        )


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
        raise ValueError(f"survey checkpoint {subject} ID is invalid")
    return normalized


__all__ = [
    "CHECKPOINT_MEDIA_TYPE",
    "CHECKPOINT_RECEIPT_SCHEMA_VERSION",
    "NativeDocumentMapSurveyCheckpointReceiptV1",
    "NativeDocumentMapSurveyCheckpointStore",
]
