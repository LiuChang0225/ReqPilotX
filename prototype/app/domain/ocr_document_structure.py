"""Source-bound, geometry-free structure facts from OCR document parsing.

``qwen3.5-ocr`` ``document_parsing`` returns LaTeX rather than grounded page
regions.  This contract deliberately keeps those facts separate from
``VisualTextSnapshot``: a heading, paragraph, table, or formula may assist
structure recovery, but it must never acquire an invented bounding box.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


OCR_DOCUMENT_STRUCTURE_SCHEMA_VERSION = "ocr-document-structure/v1"
OCR_DOCUMENT_STRUCTURE_TASK = "document_parsing"
OCR_DOCUMENT_STRUCTURE_GEOMETRY_STATUS = "NOT_PROVIDED_BY_TASK"

OcrDocumentContentType: TypeAlias = Literal[
    "HEADING",
    "TEXT",
    "TABLE",
    "FORMULA",
    "HEADER",
    "FOOTER",
    "FIGURE",
    "IMAGE",
]
OCR_DOCUMENT_CONTENT_TYPES: tuple[OcrDocumentContentType, ...] = (
    "HEADING",
    "TEXT",
    "TABLE",
    "FORMULA",
    "HEADER",
    "FOOTER",
    "FIGURE",
    "IMAGE",
)
OcrDocumentAdoptableContentType: TypeAlias = Literal[
    "HEADING",
    "TEXT",
    "TABLE",
    "FORMULA",
]
OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES: tuple[OcrDocumentAdoptableContentType, ...] = (
    "HEADING",
    "TEXT",
    "TABLE",
    "FORMULA",
)
OcrDocumentCoverageStatus: TypeAlias = Literal[
    "CAPTURED",
    "NOT_DETECTED",
    "NOT_EVALUATED",
]
OcrDocumentNextAction: TypeAlias = Literal[
    "NONE",
    "GROUNDED_LAYOUT",
    "COMPLEX_VISION",
]
OcrDocumentDiagnosticCode: TypeAlias = Literal[
    "IMAGE_PLACEHOLDER_IGNORED_REQUIRES_COMPLEX_VISION",
]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class OcrDocumentStructureCoverage(_StrictFrozenModel):
    """Truthful coverage for one content class on one parsed page."""

    content_type: OcrDocumentContentType
    status: OcrDocumentCoverageStatus
    block_count: StrictInt = Field(ge=0)
    next_action: OcrDocumentNextAction = "NONE"

    @model_validator(mode="after")
    def validate_status(self) -> OcrDocumentStructureCoverage:
        if self.content_type in OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES:
            expected = "CAPTURED" if self.block_count else "NOT_DETECTED"
            if self.status != expected or self.next_action != "NONE":
                raise ValueError(
                    "adoptable document content must report captured/not-detected"
                )
            return self

        if self.status != "NOT_EVALUATED" or self.block_count != 0:
            raise ValueError("ungrounded document content must remain not evaluated")
        expected_action = (
            "COMPLEX_VISION"
            if self.content_type in {"FIGURE", "IMAGE"}
            else "GROUNDED_LAYOUT"
        )
        if self.next_action != expected_action:
            raise ValueError("unsupported content has an invalid next action")
        return self


class OcrDocumentStructureBlock(_StrictFrozenModel):
    """One ordered LaTeX-derived auxiliary fact without page geometry."""

    block_id: StrictStr = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    content_type: OcrDocumentAdoptableContentType
    heading_level: StrictInt | None = Field(default=None, ge=1, le=6)
    latex: StrictStr = Field(min_length=1, max_length=2_000_000)
    text: str = Field(default="", max_length=2_000_000)
    adoption_status: Literal["AUXILIARY_ONLY"] = "AUXILIARY_ONLY"
    geometry_status: Literal["NOT_PROVIDED_BY_TASK"] = (
        OCR_DOCUMENT_STRUCTURE_GEOMETRY_STATUS
    )
    source_sha256: StrictStr
    page_number: StrictInt = Field(ge=1)
    page_image_sha256: StrictStr
    provider_id: StrictStr = Field(min_length=1, max_length=160)
    provider_version: StrictStr = Field(min_length=1, max_length=320)

    @field_validator("source_sha256", "page_image_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        _require_sha256(value)
        return value

    @field_validator("provider_id", "provider_version")
    @classmethod
    def reject_blank_provider_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider identity must not be blank")
        return value

    @model_validator(mode="after")
    def validate_block(self) -> OcrDocumentStructureBlock:
        if self.content_type == "HEADING":
            if self.heading_level is None or not self.text.strip():
                raise ValueError("headings require a level and text")
        elif self.heading_level is not None:
            raise ValueError("heading_level is allowed only on headings")
        if self.content_type == "TEXT" and not self.text.strip():
            raise ValueError("text blocks must have visible text")
        expected_id = stable_ocr_document_structure_block_id(
            source_sha256=self.source_sha256,
            page_image_sha256=self.page_image_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=self.page_number,
            source_order=self.source_order,
            content_type=self.content_type,
            latex=self.latex,
        )
        if self.block_id != expected_id:
            raise ValueError("block_id must be derived from source-bound content")
        return self


class OcrDocumentStructureSnapshot(_StrictFrozenModel):
    """Immutable page receipt for geometry-free OCR document structure."""

    schema_version: Literal["ocr-document-structure/v1"] = (
        OCR_DOCUMENT_STRUCTURE_SCHEMA_VERSION
    )
    snapshot_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    page_number: StrictInt = Field(ge=1)
    page_bbox: tuple[float, float, float, float]
    page_image_sha256: StrictStr
    page_image_media_type: Literal["image/png"] = "image/png"
    page_image_width_px: StrictInt = Field(ge=1)
    page_image_height_px: StrictInt = Field(ge=1)
    provider_id: StrictStr = Field(min_length=1, max_length=160)
    provider_version: StrictStr = Field(min_length=1, max_length=320)
    task: Literal["document_parsing"] = OCR_DOCUMENT_STRUCTURE_TASK
    geometry_status: Literal["NOT_PROVIDED_BY_TASK"] = (
        OCR_DOCUMENT_STRUCTURE_GEOMETRY_STATUS
    )
    document_latex_sha256: StrictStr
    coverage: tuple[OcrDocumentStructureCoverage, ...]
    blocks: tuple[OcrDocumentStructureBlock, ...] = ()
    diagnostics: tuple[OcrDocumentDiagnosticCode, ...] = ()

    @field_validator(
        "source_sha256",
        "page_image_sha256",
        "document_latex_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        _require_sha256(value)
        return value

    @field_validator("page_bbox")
    @classmethod
    def validate_page_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validate_bbox(value)

    @field_validator("provider_id", "provider_version")
    @classmethod
    def reject_blank_provider_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider identity must not be blank")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> OcrDocumentStructureSnapshot:
        if tuple(item.content_type for item in self.coverage) != (
            OCR_DOCUMENT_CONTENT_TYPES
        ):
            raise ValueError("coverage must use the canonical content-type order")
        if self.diagnostics != tuple(sorted(set(self.diagnostics))):
            raise ValueError("diagnostics must be unique and canonical")
        if tuple(item.source_order for item in self.blocks) != tuple(
            range(len(self.blocks))
        ):
            raise ValueError("blocks must use contiguous source order")

        counts = {
            content_type: sum(
                block.content_type == content_type for block in self.blocks
            )
            for content_type in OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES
        }
        for item in self.coverage:
            if (
                item.content_type in counts
                and item.block_count != counts[item.content_type]
            ):
                raise ValueError("coverage block count does not match blocks")
        for block in self.blocks:
            if (
                block.source_sha256 != self.source_sha256
                or block.page_number != self.page_number
                or block.page_image_sha256 != self.page_image_sha256
                or block.provider_id != self.provider_id
                or block.provider_version != self.provider_version
            ):
                raise ValueError("block grounding must match its snapshot")

        expected_id = stable_ocr_document_structure_snapshot_id(
            source_sha256=self.source_sha256,
            page_image_sha256=self.page_image_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=self.page_number,
            page_bbox=self.page_bbox,
            document_latex_sha256=self.document_latex_sha256,
        )
        if self.snapshot_id != expected_id:
            raise ValueError("snapshot_id must be derived from source-bound content")
        return self


def stable_ocr_document_structure_snapshot_id(
    *,
    source_sha256: str,
    page_image_sha256: str,
    provider_id: str,
    provider_version: str,
    page_number: int,
    page_bbox: tuple[float, float, float, float],
    document_latex_sha256: str,
) -> str:
    _require_sha256(source_sha256)
    _require_sha256(page_image_sha256)
    _require_sha256(document_latex_sha256)
    _require_identity(provider_id, "provider_id")
    _require_identity(provider_version, "provider_version")
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ValueError("page_number must be a positive integer")
    normalized_bbox = _validate_bbox(page_bbox)
    return _stable_id(
        "ods",
        source_sha256,
        page_image_sha256,
        provider_id,
        provider_version,
        page_number,
        normalized_bbox,
        document_latex_sha256,
    )


def stable_ocr_document_structure_block_id(
    *,
    source_sha256: str,
    page_image_sha256: str,
    provider_id: str,
    provider_version: str,
    page_number: int,
    source_order: int,
    content_type: OcrDocumentAdoptableContentType,
    latex: str,
) -> str:
    _require_sha256(source_sha256)
    _require_sha256(page_image_sha256)
    _require_identity(provider_id, "provider_id")
    _require_identity(provider_version, "provider_version")
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ValueError("page_number must be a positive integer")
    if (
        isinstance(source_order, bool)
        or not isinstance(source_order, int)
        or source_order < 0
    ):
        raise ValueError("source_order must be a non-negative integer")
    if content_type not in OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES:
        raise ValueError("content_type must be an adoptable document content type")
    if not isinstance(latex, str) or not latex.strip():
        raise ValueError("latex must not be blank")
    return _stable_id(
        "odb",
        source_sha256,
        page_image_sha256,
        provider_id,
        provider_version,
        page_number,
        source_order,
        content_type,
        hashlib.sha256(latex.encode("utf-8")).hexdigest(),
    )


def _require_sha256(value: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("value must be a lowercase SHA-256 digest")


def _require_identity(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _validate_bbox(
    value: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if not isinstance(value, tuple) or len(value) != 4:
        raise TypeError("page_bbox must be a four-number tuple")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in value
    ):
        raise TypeError("page_bbox must contain finite numbers")
    normalized = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in normalized):
        raise ValueError("page_bbox must contain finite numbers")
    if normalized[2] <= normalized[0] or normalized[3] <= normalized[1]:
        raise ValueError("page_bbox must have positive ordered area")
    return normalized


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


__all__ = [
    "OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES",
    "OCR_DOCUMENT_CONTENT_TYPES",
    "OCR_DOCUMENT_STRUCTURE_GEOMETRY_STATUS",
    "OCR_DOCUMENT_STRUCTURE_SCHEMA_VERSION",
    "OCR_DOCUMENT_STRUCTURE_TASK",
    "OcrDocumentAdoptableContentType",
    "OcrDocumentContentType",
    "OcrDocumentCoverageStatus",
    "OcrDocumentDiagnosticCode",
    "OcrDocumentNextAction",
    "OcrDocumentStructureBlock",
    "OcrDocumentStructureCoverage",
    "OcrDocumentStructureSnapshot",
    "stable_ocr_document_structure_block_id",
    "stable_ocr_document_structure_snapshot_id",
]
