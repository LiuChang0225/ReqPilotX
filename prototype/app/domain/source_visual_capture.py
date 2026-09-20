"""Neutral, source-bound contracts for single-document visual capture.

The models in this module deliberately stop at page readability.  They contain
no StructureTree, semantic classification, requirement, or extraction state.
Native SourceIR remains immutable; visual generations are append-only sidecars.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.domain.source_visual_text import VISUAL_CONTENT_TYPES, VisualTextSnapshot


CAPTURE_QUALITY_MANIFEST_SCHEMA_VERSION = "capture-quality-manifest/v1"
VISUAL_CAPTURE_GENERATION_SCHEMA_VERSION = "visual-capture-generation/v1"
VISUAL_PAGE_CAPTURE_SCHEMA_VERSION = "visual-page-capture/v1"
VISUAL_CAPTURE_PROFILE_ID = "reading-visual-capture/v1"
PDF_VISUAL_RISK_POLICY_VERSION = "pdf-native-readability-risk/v1"
PDF_PAGE_RENDERER_ID = "pymupdf-annotation-free-page-render"
PDF_PAGE_RENDERER_VERSION = "2x/v1"
PDF_PAGE_RENDER_SCALE = 2.0
MAX_VISUAL_CAPTURE_PAGES = 10_000


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class VisualCaptureStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    STALE = "STALE"


class CaptureQualityStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class PageReadableMode(str, Enum):
    NATIVE = "NATIVE"
    VISUAL = "VISUAL"
    NONE = "NONE"


class PageCaptureStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class VisualPageContentState(str, Enum):
    SELECTABLE_TEXT = "SELECTABLE_TEXT"
    VERIFIED_EMPTY = "VERIFIED_EMPTY"


class RequiredVisualCapability(str, Enum):
    NONE = "NONE"
    OCR = "OCR"
    LAYOUT = "LAYOUT"


class VisualTextRunV1(_StrictFrozenModel):
    run_id: StrictStr = Field(min_length=1, max_length=160)
    observation_id: StrictStr = Field(min_length=1, max_length=160)
    text: StrictStr = Field(min_length=1, max_length=200_000)
    bbox: tuple[float, float, float, float]
    content_type: StrictStr = Field(min_length=1, max_length=32)
    reading_order: StrictInt = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        return _valid_bbox(value, "bbox")


class VisualCaptureGenerationV1(_StrictFrozenModel):
    schema_version: Literal["visual-capture-generation/v1"] = (
        VISUAL_CAPTURE_GENERATION_SCHEMA_VERSION
    )
    generation_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    base_capture_id: StrictStr = Field(min_length=1, max_length=160)
    base_source_ir_id: StrictStr = Field(min_length=1, max_length=160)
    capture_profile_id: Literal["reading-visual-capture/v1"] = (
        VISUAL_CAPTURE_PROFILE_ID
    )
    risk_policy_version: Literal["pdf-native-readability-risk/v1"] = (
        PDF_VISUAL_RISK_POLICY_VERSION
    )
    renderer_id: Literal["pymupdf-annotation-free-page-render"] = (
        PDF_PAGE_RENDERER_ID
    )
    renderer_version: Literal["2x/v1"] = PDF_PAGE_RENDERER_VERSION
    render_scale: float = Field(default=PDF_PAGE_RENDER_SCALE, gt=0.0)
    provider_id: StrictStr = Field(min_length=1, max_length=160)
    provider_version: StrictStr = Field(min_length=1, max_length=500)
    required_page_numbers: tuple[StrictInt, ...] = Field(
        max_length=MAX_VISUAL_CAPTURE_PAGES
    )
    created_at_utc: datetime

    @field_validator("required_page_numbers")
    @classmethod
    def validate_required_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if (
            any(page < 1 or page > MAX_VISUAL_CAPTURE_PAGES for page in value)
            or value != tuple(sorted(set(value)))
        ):
            raise ValueError("required page numbers must be unique, sorted, and positive")
        return value

    @field_validator("created_at_utc")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "created_at_utc")

    @model_validator(mode="after")
    def validate_stable_id(self) -> "VisualCaptureGenerationV1":
        expected = stable_visual_capture_generation_id(
            project_id=self.project_id,
            sor_document_id=self.sor_document_id,
            source_sha256=self.source_sha256,
            base_capture_id=self.base_capture_id,
            capture_profile_id=self.capture_profile_id,
            risk_policy_version=self.risk_policy_version,
            renderer_id=self.renderer_id,
            renderer_version=self.renderer_version,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            required_page_numbers=self.required_page_numbers,
        )
        if self.generation_id != expected:
            raise ValueError("generation_id must bind source, plan, and provider")
        return self


class VisualPageCaptureV1(_StrictFrozenModel):
    schema_version: Literal["visual-page-capture/v1"] = VISUAL_PAGE_CAPTURE_SCHEMA_VERSION
    generation_id: StrictStr = Field(min_length=1, max_length=160)
    visual_snapshot_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    base_capture_id: StrictStr = Field(min_length=1, max_length=160)
    page_number: StrictInt = Field(ge=1, le=MAX_VISUAL_CAPTURE_PAGES)
    page_bbox: tuple[float, float, float, float]
    page_image_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: StrictInt = Field(ge=1)
    height_px: StrictInt = Field(ge=1)
    render_scale: float = Field(gt=0.0)
    renderer_id: StrictStr = Field(min_length=1, max_length=160)
    renderer_version: StrictStr = Field(min_length=1, max_length=160)
    provider_id: StrictStr = Field(min_length=1, max_length=160)
    provider_version: StrictStr = Field(min_length=1, max_length=500)
    capture_status: Literal["READY"] = "READY"
    content_state: VisualPageContentState = VisualPageContentState.SELECTABLE_TEXT
    runs: tuple[VisualTextRunV1, ...] = ()
    visual_text_snapshot: VisualTextSnapshot
    created_at_utc: datetime

    @field_validator("page_bbox")
    @classmethod
    def validate_page_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        return _valid_bbox(value, "page_bbox")

    @field_validator("created_at_utc")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "created_at_utc")

    @model_validator(mode="after")
    def validate_visual_binding(self) -> "VisualPageCaptureV1":
        snapshot = self.visual_text_snapshot
        if (
            self.visual_snapshot_id != snapshot.visual_text_snapshot_id
            or self.source_sha256 != snapshot.source_sha256
            or self.page_number != snapshot.page_number
            or self.page_bbox != snapshot.page_bbox
            or self.page_image_sha256 != snapshot.page_image_sha256
            or self.width_px != snapshot.page_image_width_px
            or self.height_px != snapshot.page_image_height_px
            or self.provider_id != snapshot.provider_id
            or self.provider_version != snapshot.provider_version
        ):
            raise ValueError("visual page capture must exact-bind its provider snapshot")
        expected_runs = tuple(
            visual_text_run_from_observation(item)
            for item in snapshot.observations
            if visual_observation_text(item).strip()
        )
        if self.runs != expected_runs:
            raise ValueError("visual page runs must be the canonical snapshot projection")
        if self.content_state is VisualPageContentState.SELECTABLE_TEXT:
            if not expected_runs:
                raise ValueError("SELECTABLE_TEXT visual pages require selectable runs")
        elif (
            snapshot.observations
            or expected_runs
            or snapshot.evaluated_content_types != VISUAL_CONTENT_TYPES
        ):
            raise ValueError(
                "VERIFIED_EMPTY requires an empty full-layout provider evaluation"
            )
        return self


class VisualPageCapturePublicV1(_StrictFrozenModel):
    """Public page-layer DTO without the duplicate internal provider payload."""

    schema_version: Literal["visual-page-capture/v1"] = VISUAL_PAGE_CAPTURE_SCHEMA_VERSION
    generation_id: StrictStr = Field(min_length=1, max_length=160)
    visual_snapshot_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    base_capture_id: StrictStr = Field(min_length=1, max_length=160)
    page_number: StrictInt = Field(ge=1, le=MAX_VISUAL_CAPTURE_PAGES)
    page_bbox: tuple[float, float, float, float]
    page_image_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: StrictInt = Field(ge=1)
    height_px: StrictInt = Field(ge=1)
    render_scale: float = Field(gt=0.0)
    renderer_id: StrictStr = Field(min_length=1, max_length=160)
    renderer_version: StrictStr = Field(min_length=1, max_length=160)
    provider_id: StrictStr = Field(min_length=1, max_length=160)
    provider_version: StrictStr = Field(min_length=1, max_length=500)
    capture_status: Literal["READY"] = "READY"
    content_state: VisualPageContentState = VisualPageContentState.SELECTABLE_TEXT
    runs: tuple[VisualTextRunV1, ...] = ()
    created_at_utc: datetime

    @field_validator("page_bbox")
    @classmethod
    def validate_page_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        return _valid_bbox(value, "page_bbox")

    @field_validator("created_at_utc")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware(value, "created_at_utc")

    @model_validator(mode="after")
    def validate_public_content_state(self) -> "VisualPageCapturePublicV1":
        if (
            self.content_state is VisualPageContentState.SELECTABLE_TEXT
            and not self.runs
        ):
            raise ValueError("SELECTABLE_TEXT visual pages require selectable runs")
        if self.content_state is VisualPageContentState.VERIFIED_EMPTY and self.runs:
            raise ValueError("VERIFIED_EMPTY visual pages cannot expose text runs")
        return self

    @classmethod
    def from_internal(
        cls, value: VisualPageCaptureV1
    ) -> "VisualPageCapturePublicV1":
        return cls.model_validate(
            value.model_dump(mode="python", exclude={"visual_text_snapshot"})
        )


class CaptureQualityPageV1(_StrictFrozenModel):
    page_number: StrictInt = Field(ge=1, le=MAX_VISUAL_CAPTURE_PAGES)
    page_bbox: tuple[float, float, float, float] | None = None
    readable_mode: PageReadableMode
    capture_status: PageCaptureStatus
    native_character_count: StrictInt = Field(ge=0)
    native_cjk_character_count: StrictInt = Field(ge=0)
    native_selectable: bool
    risk_codes: tuple[StrictStr, ...] = ()
    required_capability: RequiredVisualCapability
    visual_snapshot_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    page_image_sha256: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    provider_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    provider_version: StrictStr | None = Field(default=None, min_length=1, max_length=500)
    failure_code: StrictStr | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("page_bbox")
    @classmethod
    def validate_optional_bbox(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        return None if value is None else _valid_bbox(value, "page_bbox")

    @model_validator(mode="after")
    def validate_state(self) -> "CaptureQualityPageV1":
        visual_fields = (
            self.visual_snapshot_id,
            self.page_image_sha256,
            self.provider_id,
            self.provider_version,
        )
        if self.capture_status is PageCaptureStatus.READY:
            if self.readable_mode is not PageReadableMode.VISUAL or any(
                value is None for value in visual_fields
            ):
                raise ValueError("READY visual pages require complete visual provenance")
        elif any(value is not None for value in visual_fields):
            raise ValueError("non-ready pages cannot claim a visual snapshot")
        if self.capture_status is PageCaptureStatus.FAILED:
            if not self.failure_code or self.readable_mode is not PageReadableMode.NONE:
                raise ValueError("failed visual pages require a failure code")
        elif self.failure_code is not None:
            raise ValueError("failure_code is valid only for failed pages")
        if self.required_capability is RequiredVisualCapability.NONE:
            if self.capture_status is not PageCaptureStatus.NOT_REQUIRED:
                raise ValueError("native-only pages must be NOT_REQUIRED")
            if (
                self.readable_mode is not PageReadableMode.NATIVE
                or not self.native_selectable
            ):
                raise ValueError(
                    "NOT_REQUIRED pages require trusted selectable native facts"
                )
        elif self.capture_status is PageCaptureStatus.NOT_REQUIRED:
            raise ValueError("visual-required pages cannot be NOT_REQUIRED")
        elif self.capture_status is not PageCaptureStatus.READY:
            if self.readable_mode is not PageReadableMode.NONE:
                raise ValueError(
                    "visual-required pages remain unreadable until visual capture is READY"
                )
        if (
            self.readable_mode is PageReadableMode.NATIVE
            and not self.native_selectable
        ):
            raise ValueError("NATIVE readable pages require selectable native facts")
        return self


class CaptureQualityManifestV1(_StrictFrozenModel):
    schema_version: Literal["capture-quality-manifest/v1"] = (
        CAPTURE_QUALITY_MANIFEST_SCHEMA_VERSION
    )
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    base_capture_id: StrictStr = Field(min_length=1, max_length=160)
    base_source_ir_id: StrictStr = Field(min_length=1, max_length=160)
    effective_bundle_id: StrictStr = Field(min_length=1, max_length=160)
    status: CaptureQualityStatus
    page_count: StrictInt = Field(ge=0, le=MAX_VISUAL_CAPTURE_PAGES)
    native_ready_page_count: StrictInt = Field(ge=0)
    visual_required_page_count: StrictInt = Field(ge=0)
    visual_ready_page_count: StrictInt = Field(ge=0)
    failed_page_count: StrictInt = Field(ge=0)
    active_visual_generation_id: StrictStr | None = Field(
        default=None, min_length=1, max_length=160
    )
    pages: tuple[CaptureQualityPageV1, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> "CaptureQualityManifestV1":
        if len(self.pages) != self.page_count:
            raise ValueError("quality manifest must contain exactly one row per page")
        if tuple(item.page_number for item in self.pages) != tuple(
            range(1, self.page_count + 1)
        ):
            raise ValueError("quality manifest pages must be complete and ordered")
        native = sum(item.readable_mode is PageReadableMode.NATIVE for item in self.pages)
        required = sum(
            item.required_capability is not RequiredVisualCapability.NONE
            for item in self.pages
        )
        visual_ready = sum(
            item.capture_status is PageCaptureStatus.READY for item in self.pages
        )
        failed = sum(
            item.capture_status is PageCaptureStatus.FAILED for item in self.pages
        )
        if (
            native != self.native_ready_page_count
            or required != self.visual_required_page_count
            or visual_ready != self.visual_ready_page_count
            or failed != self.failed_page_count
        ):
            raise ValueError("quality manifest counters do not match page states")
        if visual_ready > required:
            raise ValueError("visual ready page count cannot exceed required pages")
        readable = native + visual_ready
        if self.page_count == 0 or readable == self.page_count:
            expected_status = CaptureQualityStatus.READY
        elif readable == 0:
            expected_status = CaptureQualityStatus.BLOCKED
        else:
            expected_status = CaptureQualityStatus.PARTIAL
        if self.status is not expected_status:
            raise ValueError(
                "manifest status must be derived from its effective readable pages"
            )
        return self


def visual_text_run_from_observation(observation) -> VisualTextRunV1:
    return VisualTextRunV1(
        run_id=observation.observation_id,
        observation_id=observation.observation_id,
        text=visual_observation_text(observation),
        bbox=observation.bbox,
        content_type=observation.content_type,
        reading_order=observation.observation_index,
        confidence=observation.confidence,
    )


def visual_observation_text(observation) -> str:
    """Canonical selectable text for one grounded layout observation.

    Layout providers commonly leave TABLE.raw_text empty and carry the actual
    visible values only in the complete cell grid.  Project the grid as TSV,
    still bound to the TABLE observation/page bbox, so capture and reading do
    not silently discard table content.
    """

    raw_text = str(observation.raw_text or "")
    if raw_text.strip() or observation.content_type != "TABLE":
        return raw_text
    table = observation.layout_table
    if table is None:
        return ""
    values: dict[tuple[int, int], str] = {
        (cell.row, cell.column): cell.raw_text for cell in table.cells
    }
    rows = [
        "\t".join(values.get((row, column), "") for column in range(table.column_count))
        .rstrip("\t")
        for row in range(table.row_count)
    ]
    return "\n".join(rows).strip("\n")


def stable_visual_capture_generation_id(
    *,
    project_id: str,
    sor_document_id: str,
    source_sha256: str,
    base_capture_id: str,
    capture_profile_id: str,
    risk_policy_version: str,
    renderer_id: str,
    renderer_version: str,
    provider_id: str,
    provider_version: str,
    required_page_numbers: tuple[int, ...],
) -> str:
    return _stable_id(
        "visual-generation",
        project_id,
        sor_document_id,
        source_sha256,
        base_capture_id,
        capture_profile_id,
        risk_policy_version,
        renderer_id,
        renderer_version,
        provider_id,
        provider_version,
        required_page_numbers,
    )


def stable_effective_bundle_id(
    *,
    base_capture_id: str,
    visual_generation_id: str | None,
    visual_snapshot_ids: tuple[str, ...],
) -> str:
    return _stable_id(
        "effective-source",
        base_capture_id,
        visual_generation_id or "native-only",
        visual_snapshot_ids,
    )


def _valid_bbox(
    value: tuple[float, float, float, float], field_name: str
) -> tuple[float, float, float, float]:
    if not isinstance(value, tuple) or len(value) != 4:
        raise TypeError(f"{field_name} must be a four-number tuple")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise TypeError(f"{field_name} must contain numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{field_name} must contain finite numbers")
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError(f"{field_name} must have positive ordered area")
    return result


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


__all__ = [
    "CAPTURE_QUALITY_MANIFEST_SCHEMA_VERSION",
    "MAX_VISUAL_CAPTURE_PAGES",
    "PDF_PAGE_RENDERER_ID",
    "PDF_PAGE_RENDERER_VERSION",
    "PDF_PAGE_RENDER_SCALE",
    "PDF_VISUAL_RISK_POLICY_VERSION",
    "VISUAL_CAPTURE_GENERATION_SCHEMA_VERSION",
    "VISUAL_CAPTURE_PROFILE_ID",
    "VISUAL_PAGE_CAPTURE_SCHEMA_VERSION",
    "CaptureQualityManifestV1",
    "CaptureQualityPageV1",
    "CaptureQualityStatus",
    "PageCaptureStatus",
    "PageReadableMode",
    "RequiredVisualCapability",
    "VisualCaptureGenerationV1",
    "VisualCaptureStatus",
    "VisualPageContentState",
    "VisualPageCaptureV1",
    "VisualPageCapturePublicV1",
    "VisualTextRunV1",
    "stable_effective_bundle_id",
    "stable_visual_capture_generation_id",
    "visual_text_run_from_observation",
    "visual_observation_text",
]
