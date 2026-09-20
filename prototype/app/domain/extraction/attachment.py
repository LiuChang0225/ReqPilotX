from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.extraction.preflight import DocumentFormat
from app.domain.extraction.semantics import SemanticClass


ATTACHMENT_DISPOSITION_SCHEMA_VERSION = "attachment-disposition/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_ARTIFACT_ID_PATTERN = re.compile(r"^attachment-artifact-[0-9a-f]{32}$")
_REFERENCE_ID_PATTERN = re.compile(r"^attachment-reference-[0-9a-f]{32}$")
_SNAPSHOT_ID_PATTERN = re.compile(r"^attachment-disposition-[0-9a-f]{32}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ExtractionDocumentRole(str, Enum):
    CUSTOMER_DRAWING = "customer_drawing"


class ExtractionIngressRoute(str, Enum):
    ATTACHMENT = "attachment"


class AttachmentReferenceRole(str, Enum):
    REFERENCE = "REFERENCE"


class AttachmentDecisionMode(str, Enum):
    NONE = "NONE"


class DeepRecognitionMode(str, Enum):
    BACKLOG = "BACKLOG"


class ExtractionDocumentContext(_StrictFrozenModel):
    logical_document_id: str = Field(min_length=1, max_length=160)
    upload_revision_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    source_size: int = Field(gt=0)
    document_role: ExtractionDocumentRole
    ingress_route: ExtractionIngressRoute

    @field_validator("logical_document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not _CONTEXT_ID_PATTERN.fullmatch(value):
            raise ValueError("logical_document_id must use a stable document key")
        return value

    @field_validator("upload_revision_id")
    @classmethod
    def validate_revision_id(cls, value: str) -> str:
        if not _CONTEXT_ID_PATTERN.fullmatch(value):
            raise ValueError("upload_revision_id must use a stable revision key")
        return value

    @field_validator("source_sha256")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        return value


class AttachmentArtifactReceipt(_StrictFrozenModel):
    artifact_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    source_size: int = Field(gt=0)
    media_type: str = Field(min_length=1, max_length=160)
    storage_key: str = Field(min_length=1, max_length=1000)
    store_verified: Literal[True]

    @field_validator("source_sha256")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("media_type")
    @classmethod
    def reject_blank_media_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("media_type must not be blank")
        return value

    @field_validator("storage_key")
    @classmethod
    def validate_storage_key(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("storage_key must use portable forward slashes")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("storage_key must be a contained relative key")
        if path.parts and path.parts[0].endswith(":"):
            raise ValueError("storage_key must not contain a drive prefix")
        return value

    @model_validator(mode="after")
    def validate_stable_identity(self) -> AttachmentArtifactReceipt:
        if not _ARTIFACT_ID_PATTERN.fullmatch(self.artifact_id):
            raise ValueError("artifact_id must use a stable attachment identity")
        expected = stable_attachment_artifact_id(
            source_sha256=self.source_sha256,
            source_size=self.source_size,
        )
        if self.artifact_id != expected:
            raise ValueError("artifact_id must be derived from source identity")
        return self


class AttachmentReference(_StrictFrozenModel):
    attachment_reference_id: str = Field(min_length=1, max_length=160)
    artifact_id: str = Field(min_length=1, max_length=160)
    semantic_class: Literal[SemanticClass.ATTACHMENT] = SemanticClass.ATTACHMENT
    role: Literal[AttachmentReferenceRole.REFERENCE] = AttachmentReferenceRole.REFERENCE
    decision_mode: Literal[AttachmentDecisionMode.NONE] = AttachmentDecisionMode.NONE

    @model_validator(mode="after")
    def validate_stable_identity(self) -> AttachmentReference:
        if not _ARTIFACT_ID_PATTERN.fullmatch(self.artifact_id):
            raise ValueError("artifact_id must use a stable attachment identity")
        if not _REFERENCE_ID_PATTERN.fullmatch(self.attachment_reference_id):
            raise ValueError("attachment_reference_id must use a stable identity")
        expected = stable_attachment_reference_id(artifact_id=self.artifact_id)
        if self.attachment_reference_id != expected:
            raise ValueError("attachment_reference_id must be derived from artifact")
        return self


class AttachmentDispositionSnapshot(_StrictFrozenModel):
    schema_version: Literal["attachment-disposition/v1"] = (
        ATTACHMENT_DISPOSITION_SCHEMA_VERSION
    )
    attachment_snapshot_id: str = Field(min_length=1, max_length=160)
    context: ExtractionDocumentContext
    detected_format: DocumentFormat
    preflight_schema_version: Literal["document-preflight/v2"]
    page_count: int | None = Field(default=None, ge=0)
    artifact: AttachmentArtifactReceipt
    reference: AttachmentReference
    semantic_class: Literal[SemanticClass.ATTACHMENT] = SemanticClass.ATTACHMENT
    deep_recognition_mode: Literal[DeepRecognitionMode.BACKLOG] = (
        DeepRecognitionMode.BACKLOG
    )
    blocks_requirement_source_extraction: Literal[False] = False
    ocr_provider_ids: tuple[str, ...] = ()
    visual_text_snapshot_count: Literal[0] = 0
    decision_requirement_count: Literal[0] = 0
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_validator("ocr_provider_ids")
    @classmethod
    def prohibit_ocr_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError("opaque attachments must not invoke OCR providers")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> AttachmentDispositionSnapshot:
        if self.detected_format not in {DocumentFormat.PDF, DocumentFormat.TIF}:
            raise ValueError("opaque attachment lane currently accepts PDF or TIF")
        if (
            self.artifact.source_sha256 != self.context.source_sha256
            or self.artifact.source_size != self.context.source_size
        ):
            raise ValueError("artifact receipt must match the trusted document context")
        if self.reference.artifact_id != self.artifact.artifact_id:
            raise ValueError("attachment reference must target the preserved artifact")
        if not _SNAPSHOT_ID_PATTERN.fullmatch(self.attachment_snapshot_id):
            raise ValueError("attachment_snapshot_id must use a stable identity")
        expected = stable_attachment_snapshot_id(
            context=self.context,
            artifact_id=self.artifact.artifact_id,
        )
        if self.attachment_snapshot_id != expected:
            raise ValueError(
                "attachment_snapshot_id must be derived from trusted identity"
            )
        return self

    @property
    def attachment_artifact_count(self) -> int:
        return 1

    @property
    def attachment_reference_count(self) -> int:
        return 1


def stable_attachment_artifact_id(*, source_sha256: str, source_size: int) -> str:
    return _stable_id("attachment-artifact", source_sha256, source_size)


def stable_attachment_reference_id(*, artifact_id: str) -> str:
    return _stable_id("attachment-reference", artifact_id)


def stable_attachment_snapshot_id(
    *,
    context: ExtractionDocumentContext,
    artifact_id: str,
) -> str:
    return _stable_id(
        "attachment-disposition",
        context.logical_document_id,
        context.upload_revision_id,
        context.source_sha256,
        context.source_size,
        context.document_role.value,
        context.ingress_route.value,
        artifact_id,
    )


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


__all__ = [
    "ATTACHMENT_DISPOSITION_SCHEMA_VERSION",
    "AttachmentArtifactReceipt",
    "AttachmentDecisionMode",
    "AttachmentDispositionSnapshot",
    "AttachmentReference",
    "AttachmentReferenceRole",
    "DeepRecognitionMode",
    "ExtractionDocumentContext",
    "ExtractionDocumentRole",
    "ExtractionIngressRoute",
    "stable_attachment_artifact_id",
    "stable_attachment_reference_id",
    "stable_attachment_snapshot_id",
]
