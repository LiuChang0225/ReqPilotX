from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBytes,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


VISUAL_TABLE_SCHEMA_VERSION = "visual-table/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_NODE_ID_PATTERN = re.compile(r"^node-[0-9a-f]{32}$")
_TABLE_ID_PATTERN = re.compile(r"^table-[0-9a-f]{32}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class VisualTableCropInput(_StrictFrozenModel):
    """One annotation-free 2x crop of one native PDF table.

    The bytes are memory-only provider input.  Native topology is deliberately
    absent: the remote recognizer cannot be steered by the answer it will later
    be checked against.
    """

    source_bytes: StrictBytes = Field(min_length=1, repr=False, exclude=True)
    source_sha256: StrictStr
    page_number: StrictInt = Field(ge=1)
    page_bbox: tuple[float, float, float, float]
    table_source_node_id: str = Field(min_length=1, max_length=160)
    table_id: str = Field(min_length=1, max_length=160)
    table_index: StrictInt = Field(ge=0)
    table_bbox: tuple[float, float, float, float]
    crop_image_bytes: StrictBytes = Field(min_length=1, repr=False, exclude=True)
    crop_image_sha256: StrictStr
    crop_image_media_type: Literal["image/png"] = "image/png"
    crop_image_width_px: StrictInt = Field(ge=1)
    crop_image_height_px: StrictInt = Field(ge=1)
    render_scale: Literal[2.0] = 2.0
    annotations_included: Literal[False] = False

    @field_validator("source_sha256", "crop_image_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        _validate_sha256(value, "digest")
        return value

    @field_validator("page_bbox", "table_bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validate_bbox(value, "bbox")

    @field_validator("table_source_node_id")
    @classmethod
    def validate_source_node_id(cls, value: str) -> str:
        if _SOURCE_NODE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("table_source_node_id must use a stable SourceIR identity")
        return value

    @field_validator("table_id")
    @classmethod
    def validate_table_id(cls, value: str) -> str:
        if _TABLE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("table_id must use a stable SourceIR identity")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> VisualTableCropInput:
        if hashlib.sha256(self.source_bytes).hexdigest() != self.source_sha256:
            raise ValueError("source_bytes must match source_sha256")
        if hashlib.sha256(self.crop_image_bytes).hexdigest() != self.crop_image_sha256:
            raise ValueError("crop_image_bytes must match crop_image_sha256")
        if not _bbox_is_within(self.table_bbox, self.page_bbox):
            raise ValueError("table_bbox must stay within page_bbox")
        return self


class VisualTableCell(_StrictFrozenModel):
    """Provider-parsed cell text and topology; geometry is intentionally absent."""

    visual_table_cell_id: str = Field(min_length=1, max_length=160)
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(default=1, ge=1)
    column_span: StrictInt = Field(default=1, ge=1)
    raw_text: str = ""


class VisualTableSnapshot(_StrictFrozenModel):
    """Immutable provider facts for one exact native-table crop."""

    schema_version: Literal["visual-table/v1"] = VISUAL_TABLE_SCHEMA_VERSION
    visual_table_snapshot_id: str = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=320)
    page_number: StrictInt = Field(ge=1)
    page_bbox: tuple[float, float, float, float]
    table_source_node_id: str = Field(min_length=1, max_length=160)
    table_id: str = Field(min_length=1, max_length=160)
    table_index: StrictInt = Field(ge=0)
    table_bbox: tuple[float, float, float, float]
    crop_image_sha256: StrictStr
    crop_image_media_type: Literal["image/png"] = "image/png"
    crop_image_width_px: StrictInt = Field(ge=1)
    crop_image_height_px: StrictInt = Field(ge=1)
    render_scale: Literal[2.0] = 2.0
    annotations_included: Literal[False] = False
    row_count: StrictInt = Field(ge=1)
    column_count: StrictInt = Field(ge=1)
    cells: tuple[VisualTableCell, ...] = Field(min_length=1)

    @field_validator("source_sha256", "crop_image_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        _validate_sha256(value, "digest")
        return value

    @field_validator("page_bbox", "table_bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validate_bbox(value, "bbox")

    @field_validator("provider_id", "provider_version")
    @classmethod
    def reject_blank_provider_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider identity must not be blank")
        return value

    @field_validator("table_source_node_id")
    @classmethod
    def validate_source_node_id(cls, value: str) -> str:
        if _SOURCE_NODE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("table_source_node_id must use a stable SourceIR identity")
        return value

    @field_validator("table_id")
    @classmethod
    def validate_table_id(cls, value: str) -> str:
        if _TABLE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("table_id must use a stable SourceIR identity")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> VisualTableSnapshot:
        if not _bbox_is_within(self.table_bbox, self.page_bbox):
            raise ValueError("table_bbox must stay within page_bbox")
        expected_snapshot_id = stable_visual_table_snapshot_id(
            source_sha256=self.source_sha256,
            crop_image_sha256=self.crop_image_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=self.page_number,
            table_source_node_id=self.table_source_node_id,
            table_id=self.table_id,
            table_index=self.table_index,
            table_bbox=self.table_bbox,
            crop_image_width_px=self.crop_image_width_px,
            crop_image_height_px=self.crop_image_height_px,
        )
        if self.visual_table_snapshot_id != expected_snapshot_id:
            raise ValueError("visual_table_snapshot_id must bind provider and exact crop")

        anchors = [(cell.row, cell.column) for cell in self.cells]
        if anchors != sorted(anchors) or len(anchors) != len(set(anchors)):
            raise ValueError("visual table cells must have unique canonical anchors")
        occupied: set[tuple[int, int]] = set()
        for cell in self.cells:
            if (
                cell.row + cell.row_span > self.row_count
                or cell.column + cell.column_span > self.column_count
            ):
                raise ValueError("visual table cell span exceeds table dimensions")
            expected_cell_id = stable_visual_table_cell_id(
                visual_table_snapshot_id=self.visual_table_snapshot_id,
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
            )
            if cell.visual_table_cell_id != expected_cell_id:
                raise ValueError("visual_table_cell_id must bind its snapshot topology")
            coverage = {
                (row, column)
                for row in range(cell.row, cell.row + cell.row_span)
                for column in range(cell.column, cell.column + cell.column_span)
            }
            if occupied.intersection(coverage):
                raise ValueError("visual table cell spans must not overlap")
            occupied.update(coverage)
        expected_coverage = {
            (row, column)
            for row in range(self.row_count)
            for column in range(self.column_count)
        }
        if occupied != expected_coverage:
            raise ValueError("visual table topology must cover one complete rectangle")
        return self


def stable_visual_table_snapshot_id(
    *,
    source_sha256: str,
    crop_image_sha256: str,
    provider_id: str,
    provider_version: str,
    page_number: int,
    table_source_node_id: str,
    table_id: str,
    table_index: int,
    table_bbox: tuple[float, float, float, float],
    crop_image_width_px: int,
    crop_image_height_px: int,
) -> str:
    _validate_sha256(source_sha256, "source_sha256")
    _validate_sha256(crop_image_sha256, "crop_image_sha256")
    if not provider_id.strip() or not provider_version.strip():
        raise ValueError("provider identity must not be blank")
    if _SOURCE_NODE_ID_PATTERN.fullmatch(table_source_node_id) is None:
        raise ValueError("table_source_node_id must be stable")
    if _TABLE_ID_PATTERN.fullmatch(table_id) is None:
        raise ValueError("table_id must be stable")
    if page_number < 1 or table_index < 0:
        raise ValueError("page_number/table_index are outside their valid range")
    if crop_image_width_px < 1 or crop_image_height_px < 1:
        raise ValueError("crop dimensions must be positive")
    bbox = _validate_bbox(table_bbox, "table_bbox")
    return _stable_id(
        "vtable",
        source_sha256,
        crop_image_sha256,
        provider_id,
        provider_version,
        page_number,
        table_source_node_id,
        table_id,
        table_index,
        bbox,
        crop_image_width_px,
        crop_image_height_px,
        2.0,
        False,
    )


def stable_visual_table_cell_id(
    *,
    visual_table_snapshot_id: str,
    row: int,
    column: int,
    row_span: int,
    column_span: int,
) -> str:
    if not isinstance(visual_table_snapshot_id, str) or not visual_table_snapshot_id:
        raise ValueError("visual_table_snapshot_id must not be blank")
    if min(row, column) < 0 or min(row_span, column_span) < 1:
        raise ValueError("visual table cell topology is invalid")
    return _stable_id(
        "vtc",
        visual_table_snapshot_id,
        row,
        column,
        row_span,
        column_span,
    )


def _validate_bbox(
    value: tuple[float, float, float, float],
    field_name: str,
) -> tuple[float, float, float, float]:
    if not isinstance(value, tuple) or len(value) != 4:
        raise TypeError(f"{field_name} must be a four-number tuple")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise TypeError(f"{field_name} must contain finite numbers")
    bbox = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in bbox):
        raise ValueError(f"{field_name} must contain finite numbers")
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError(f"{field_name} must have positive ordered area")
    return bbox


def _bbox_is_within(
    child: tuple[float, float, float, float],
    parent: tuple[float, float, float, float],
) -> bool:
    tolerance = 1e-6
    return (
        child[0] >= parent[0] - tolerance
        and child[1] >= parent[1] - tolerance
        and child[2] <= parent[2] + tolerance
        and child[3] <= parent[3] + tolerance
    )


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


__all__ = [
    "VISUAL_TABLE_SCHEMA_VERSION",
    "VisualTableCell",
    "VisualTableCropInput",
    "VisualTableSnapshot",
    "stable_visual_table_cell_id",
    "stable_visual_table_snapshot_id",
]
