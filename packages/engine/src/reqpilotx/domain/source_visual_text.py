"""Provider-neutral, source-bound visual text facts.

This module is owned by neutral source capture.  The former extraction-domain
path is retained as a compatibility re-export.
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
    StrictBytes,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


VISUAL_TEXT_SCHEMA_VERSION = "visual-text/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VisualContentType: TypeAlias = Literal[
    "TEXT",
    "TABLE",
    "FIGURE",
    "HEADER",
    "FOOTER",
    "WATERMARK",
]
VISUAL_CONTENT_TYPES: tuple[VisualContentType, ...] = (
    "TEXT",
    "TABLE",
    "FIGURE",
    "HEADER",
    "FOOTER",
    "WATERMARK",
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class VisualTextPageInput(_StrictFrozenModel):
    """Ephemeral, memory-only page image passed to a visual text provider.

    The bytes are intentionally part of the provider input rather than an
    on-disk artifact.  A provider must independently recompute both digests
    before it performs recognition.
    """

    source_bytes: StrictBytes = Field(min_length=1, repr=False, exclude=True)
    source_sha256: StrictStr
    page_number: StrictInt = Field(ge=1)
    page_bbox: tuple[float, float, float, float]
    page_image_bytes: StrictBytes = Field(min_length=1, repr=False, exclude=True)
    page_image_sha256: StrictStr
    page_image_media_type: Literal["image/png"] = "image/png"
    page_image_width_px: StrictInt = Field(ge=1)
    page_image_height_px: StrictInt = Field(ge=1)

    @field_validator("source_sha256", "page_image_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        _validate_sha256(value, "digest")
        return value

    @field_validator("page_bbox")
    @classmethod
    def validate_page_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validate_bbox(value, "page_bbox")


class VisualTextGeometryReceipt(_StrictFrozenModel):
    """Provider polygon plus the deterministic render-to-page transform proof.

    ``GEOMETRY_TRANSFORM_VALIDATED`` means only that the four provider points
    satisfied the geometric contract and were deterministically mapped onto
    the source page.  It is deliberately not a claim that the OCR text or its
    box is semantically accurate.
    """

    schema_version: Literal["visual-text-geometry-receipt/v1"] = (
        "visual-text-geometry-receipt/v1"
    )
    provider_coordinate_space: Literal[
        "PROVIDER_PAGE_IMAGE_PIXELS_TOP_LEFT"
    ] = "PROVIDER_PAGE_IMAGE_PIXELS_TOP_LEFT"
    original_page_coordinate_space: Literal[
        "ORIGINAL_PAGE_IMAGE_PIXELS_TOP_LEFT"
    ] = "ORIGINAL_PAGE_IMAGE_PIXELS_TOP_LEFT"
    target_coordinate_space: Literal["PDF_VISIBLE_PAGE_POINTS_TOP_LEFT"] = (
        "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT"
    )
    provider_point_order: Literal["TOP_LEFT_CLOCKWISE"] = "TOP_LEFT_CLOCKWISE"
    provider_quadrilateral_px: tuple[tuple[float, float], ...]
    original_page_quadrilateral_px: tuple[tuple[float, float], ...]
    provider_image_size_px: tuple[StrictInt, StrictInt]
    original_page_image_size_px: tuple[StrictInt, StrictInt]
    input_rotation_degrees: Literal[0, 90, 180, 270]
    transform_version: Literal["pb2"] = "pb2"
    target_bbox: tuple[float, float, float, float]
    validation_status: Literal["GEOMETRY_TRANSFORM_VALIDATED"] = (
        "GEOMETRY_TRANSFORM_VALIDATED"
    )

    @field_validator(
        "provider_quadrilateral_px",
        "original_page_quadrilateral_px",
        mode="before",
    )
    @classmethod
    def validate_quadrilateral(
        cls,
        value: object,
    ) -> tuple[tuple[float, float], ...]:
        return _validate_receipt_quadrilateral(value)

    @field_validator("provider_image_size_px", "original_page_image_size_px")
    @classmethod
    def validate_image_size(
        cls,
        value: tuple[int, int],
    ) -> tuple[int, int]:
        if len(value) != 2 or value[0] < 1 or value[1] < 1:
            raise ValueError("geometry receipt image sizes must be positive")
        return value

    @field_validator("target_bbox")
    @classmethod
    def validate_target_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validate_bbox(value, "target_bbox")

    @model_validator(mode="after")
    def validate_transform_receipt(self) -> VisualTextGeometryReceipt:
        original_width, original_height = self.original_page_image_size_px
        expected_provider_size = (
            (original_width, original_height)
            if self.input_rotation_degrees in {0, 180}
            else (original_height, original_width)
        )
        if self.provider_image_size_px != expected_provider_size:
            raise ValueError(
                "provider image size must match the recorded orthogonal rotation"
            )
        _validate_clockwise_receipt_quadrilateral(
            self.provider_quadrilateral_px,
            image_size=self.provider_image_size_px,
        )
        _validate_clockwise_receipt_quadrilateral(
            self.original_page_quadrilateral_px,
            image_size=self.original_page_image_size_px,
        )
        expected_original = _inverse_receipt_quadrilateral_rotation(
            self.provider_quadrilateral_px,
            original_image_size=self.original_page_image_size_px,
            clockwise_rotation_degrees=self.input_rotation_degrees,
        )
        if not _quadrilaterals_match(
            expected_original,
            self.original_page_quadrilateral_px,
        ):
            raise ValueError(
                "original quadrilateral must match the recorded inverse transform"
            )
        return self


class VisualLayoutTableCell(_StrictFrozenModel):
    """One provider-observed cell in a complete rectangular page table."""

    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(default=1, ge=1)
    column_span: StrictInt = Field(default=1, ge=1)
    raw_text: str = ""
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    horizontal_alignment: Literal["LEFT", "CENTER", "RIGHT", "JUSTIFY"] | None = None
    vertical_alignment: Literal["TOP", "MIDDLE", "BOTTOM"] | None = None
    indent_level: StrictInt = Field(default=0, ge=0, le=20)

    @field_validator("bbox")
    @classmethod
    def validate_optional_bbox(cls, value):
        return _validate_bbox(value, "visual table cell bbox") if value is not None else None


class VisualLayoutTable(_StrictFrozenModel):
    """Provider-observed table topology carried by one page-layout block."""

    row_count: StrictInt = Field(ge=1, le=4_000)
    column_count: StrictInt = Field(ge=1, le=1_000)
    header_row_count: StrictInt = Field(default=0, ge=0)
    starts_mid_table: bool = False
    continues_next_page: bool = False
    cells: tuple[VisualLayoutTableCell, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_grid(self) -> VisualLayoutTable:
        if self.header_row_count > self.row_count:
            raise ValueError("header_row_count must not exceed row_count")
        if self.row_count * self.column_count > 500_000:
            raise ValueError("visual layout table exceeds the bounded grid limit")
        anchors = [(cell.row, cell.column) for cell in self.cells]
        if anchors != sorted(anchors) or len(anchors) != len(set(anchors)):
            raise ValueError("visual layout table cells need unique canonical anchors")
        occupied: set[tuple[int, int]] = set()
        for cell in self.cells:
            if (
                cell.row + cell.row_span > self.row_count
                or cell.column + cell.column_span > self.column_count
            ):
                raise ValueError("visual layout table cell span exceeds table dimensions")
            coverage = {
                (row, column)
                for row in range(cell.row, cell.row + cell.row_span)
                for column in range(cell.column, cell.column + cell.column_span)
            }
            if occupied.intersection(coverage):
                raise ValueError("visual layout table cell spans must not overlap")
            occupied.update(coverage)
        expected = {
            (row, column)
            for row in range(self.row_count)
            for column in range(self.column_count)
        }
        if occupied != expected:
            raise ValueError("visual layout table topology must cover one rectangle")
        return self

    def cell_covering(
        self,
        row: int,
        column: int,
    ) -> VisualLayoutTableCell | None:
        return next(
            (
                cell
                for cell in self.cells
                if cell.row <= row < cell.row + cell.row_span
                and cell.column <= column < cell.column + cell.column_span
            ),
            None,
        )


class VisualTextObservation(_StrictFrozenModel):
    """One OCR or page-layout observation grounded in an exact rendered page."""

    observation_id: str = Field(min_length=1, max_length=160)
    observation_index: StrictInt = Field(ge=0)
    source_sha256: StrictStr
    page_number: StrictInt = Field(ge=1)
    bbox: tuple[float, float, float, float]
    raw_text: str = ""
    content_type: VisualContentType = "TEXT"
    role: Literal["TOC"] | None = None
    layout_table: VisualLayoutTable | None = None
    parent_observation_index: StrictInt | None = Field(default=None, ge=0)
    parent_table_cell: tuple[StrictInt, StrictInt] | None = None
    # Some approved remote OCR APIs (notably Bailian advanced recognition)
    # return grounded line geometry without a calibrated confidence.  ``None``
    # preserves that distinction instead of inventing a perfect or zero score.
    confidence: float | None = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=160)
    page_image_sha256: StrictStr
    geometry_receipt: VisualTextGeometryReceipt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("source_sha256", "page_image_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        _validate_sha256(value, "digest")
        return value

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validate_bbox(value, "bbox")

    @field_validator("provider_id", "provider_version")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("parent_table_cell")
    @classmethod
    def validate_parent_table_cell(
        cls,
        value: tuple[int, int] | None,
    ) -> tuple[int, int] | None:
        if value is None:
            return value
        if (
            not isinstance(value, tuple)
            or len(value) != 2
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in value
            )
        ):
            raise TypeError("parent_table_cell must be a non-negative row/column tuple")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def require_numeric_confidence(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("confidence must be null or a finite number")
        return value

    @model_validator(mode="after")
    def validate_stable_identity(self) -> VisualTextObservation:
        if self.content_type in {"TEXT", "HEADER", "FOOTER", "WATERMARK"}:
            if not self.raw_text.strip():
                raise ValueError("text-bearing visual observations must not be blank")
        if self.content_type == "TABLE":
            if self.layout_table is None:
                raise ValueError("TABLE observations require a complete layout_table")
            if any(cell.bbox is not None and not _bbox_is_within(cell.bbox, self.bbox)
                   for cell in self.layout_table.cells):
                raise ValueError("visual table cell bbox must stay within its table observation")
        elif self.layout_table is not None:
            raise ValueError("layout_table is allowed only on TABLE observations")
        if self.role is not None and self.content_type != "TEXT":
            raise ValueError("TOC role is allowed only on TEXT observations")
        parent_fields = (
            self.parent_observation_index,
            self.parent_table_cell,
        )
        if (parent_fields[0] is None) != (parent_fields[1] is None):
            raise ValueError("table-child grounding requires both parent fields")
        if parent_fields[0] is not None:
            if self.content_type != "FIGURE":
                raise ValueError("only FIGURE observations can be placed in a table cell")
            if parent_fields[0] >= self.observation_index:
                raise ValueError("a table-child observation must follow its table")
        expected_id = stable_visual_text_observation_id(
            source_sha256=self.source_sha256,
            page_image_sha256=self.page_image_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=self.page_number,
            observation_index=self.observation_index,
            bbox=self.bbox,
        )
        if self.observation_id != expected_id:
            raise ValueError(
                "observation_id must be derived from provider and page-image grounding"
            )
        if self.geometry_receipt is not None and not _bboxes_match(
            self.geometry_receipt.target_bbox,
            self.bbox,
        ):
            raise ValueError(
                "geometry receipt target_bbox must match its visual observation"
            )
        return self


class VisualTextSnapshot(_StrictFrozenModel):
    """Immutable OCR/page-layout facts for one in-memory rendered source page."""

    schema_version: Literal["visual-text/v1"] = VISUAL_TEXT_SCHEMA_VERSION
    visual_text_snapshot_id: str = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=160)
    page_number: StrictInt = Field(ge=1)
    page_bbox: tuple[float, float, float, float]
    page_image_sha256: StrictStr
    page_image_media_type: Literal["image/png"] = "image/png"
    page_image_width_px: StrictInt = Field(ge=1)
    page_image_height_px: StrictInt = Field(ge=1)
    evaluated_content_types: tuple[VisualContentType, ...] = ("TEXT",)
    observations: tuple[VisualTextObservation, ...] = ()

    @field_validator("source_sha256", "page_image_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        _validate_sha256(value, "digest")
        return value

    @field_validator("page_bbox")
    @classmethod
    def validate_page_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validate_bbox(value, "page_bbox")

    @field_validator("provider_id", "provider_version")
    @classmethod
    def reject_blank_provider_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_snapshot_grounding(self) -> VisualTextSnapshot:
        if (
            not self.evaluated_content_types
            or len(self.evaluated_content_types)
            != len(set(self.evaluated_content_types))
            or self.evaluated_content_types
            != tuple(
                item
                for item in VISUAL_CONTENT_TYPES
                if item in self.evaluated_content_types
            )
        ):
            raise ValueError(
                "evaluated_content_types must be a non-empty canonical subset"
            )
        expected_id = stable_visual_text_snapshot_id(
            source_sha256=self.source_sha256,
            page_image_sha256=self.page_image_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=self.page_number,
            page_bbox=self.page_bbox,
            page_image_width_px=self.page_image_width_px,
            page_image_height_px=self.page_image_height_px,
        )
        if self.visual_text_snapshot_id != expected_id:
            raise ValueError(
                "visual_text_snapshot_id must be derived from provider and page-image grounding"
            )

        indexes = [observation.observation_index for observation in self.observations]
        if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
            raise ValueError(
                "visual text observations must have unique ascending observation_index values"
            )
        observation_ids = {item.observation_id for item in self.observations}
        if len(observation_ids) != len(self.observations):
            raise ValueError("visual text observation IDs must be unique")
        observations_by_index = {
            observation.observation_index: observation
            for observation in self.observations
        }
        for observation in self.observations:
            if (
                observation.source_sha256 != self.source_sha256
                or observation.page_image_sha256 != self.page_image_sha256
                or observation.provider_id != self.provider_id
                or observation.provider_version != self.provider_version
                or observation.page_number != self.page_number
            ):
                raise ValueError(
                    "visual text observation grounding must match its snapshot"
                )
            if not _bbox_is_within(observation.bbox, self.page_bbox):
                raise ValueError("visual text observation bbox must stay on its source page")
            if observation.content_type not in self.evaluated_content_types:
                raise ValueError(
                    "visual observation type must be declared as evaluated"
                )
            if (
                observation.geometry_receipt is not None
                and observation.geometry_receipt.original_page_image_size_px
                != (self.page_image_width_px, self.page_image_height_px)
            ):
                raise ValueError(
                    "geometry receipt render dimensions must match its snapshot"
                )
            if observation.geometry_receipt is not None and not _bboxes_match(
                _receipt_quadrilateral_to_page_bbox(
                    observation.geometry_receipt.original_page_quadrilateral_px,
                    image_size=(
                        self.page_image_width_px,
                        self.page_image_height_px,
                    ),
                    page_bbox=self.page_bbox,
                ),
                observation.bbox,
            ):
                raise ValueError(
                    "geometry receipt polygon must map to its observation bbox"
                )
            if observation.parent_observation_index is not None:
                parent = observations_by_index.get(
                    observation.parent_observation_index
                )
                if (
                    parent is None
                    or parent.content_type != "TABLE"
                    or parent.layout_table is None
                    or observation.parent_table_cell is None
                    or parent.layout_table.cell_covering(
                        observation.parent_table_cell[0],
                        observation.parent_table_cell[1],
                    )
                    is None
                ):
                    raise ValueError(
                        "table-child grounding must resolve to one observed table cell"
                    )
        return self


def stable_visual_text_snapshot_id(
    *,
    source_sha256: str,
    page_image_sha256: str,
    provider_id: str,
    provider_version: str,
    page_number: int,
    page_bbox: tuple[float, float, float, float],
    page_image_width_px: int,
    page_image_height_px: int,
) -> str:
    _validate_sha256(source_sha256, "source_sha256")
    _validate_sha256(page_image_sha256, "page_image_sha256")
    _require_non_blank(provider_id, "provider_id")
    _require_non_blank(provider_version, "provider_version")
    if isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if (
        isinstance(page_image_width_px, bool)
        or isinstance(page_image_height_px, bool)
        or page_image_width_px < 1
        or page_image_height_px < 1
    ):
        raise ValueError("page image dimensions must be positive integers")
    canonical_bbox = _validate_bbox(page_bbox, "page_bbox")
    return _stable_id(
        "vts",
        source_sha256,
        page_image_sha256,
        provider_id,
        provider_version,
        page_number,
        canonical_bbox,
        page_image_width_px,
        page_image_height_px,
    )


def stable_visual_text_observation_id(
    *,
    source_sha256: str,
    page_image_sha256: str,
    provider_id: str,
    provider_version: str,
    page_number: int,
    observation_index: int,
    bbox: tuple[float, float, float, float],
) -> str:
    _validate_sha256(source_sha256, "source_sha256")
    _validate_sha256(page_image_sha256, "page_image_sha256")
    _require_non_blank(provider_id, "provider_id")
    _require_non_blank(provider_version, "provider_version")
    if isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if isinstance(observation_index, bool) or observation_index < 0:
        raise ValueError("observation_index must be a non-negative integer")
    canonical_bbox = _validate_bbox(bbox, "bbox")
    return _stable_id(
        "vto",
        source_sha256,
        page_image_sha256,
        provider_id,
        provider_version,
        page_number,
        observation_index,
        canonical_bbox,
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
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0:
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


def _validate_receipt_quadrilateral(
    value: object,
) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise TypeError("geometry receipt quadrilateral must contain four points")
    points: list[tuple[float, float]] = []
    for point in value:
        if (
            not isinstance(point, (tuple, list))
            or len(point) != 2
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float))
                for item in point
            )
        ):
            raise TypeError("geometry receipt points must contain finite numbers")
        normalized = (float(point[0]), float(point[1]))
        if not all(math.isfinite(item) for item in normalized):
            raise ValueError("geometry receipt points must contain finite numbers")
        points.append(normalized)
    normalized_points = tuple(points)
    if len(set(normalized_points)) != 4:
        raise ValueError("geometry receipt quadrilateral needs four unique points")
    return normalized_points


def _validate_clockwise_receipt_quadrilateral(
    points: tuple[tuple[float, float], ...],
    *,
    image_size: tuple[int, int],
) -> None:
    width, height = image_size
    if any(
        x < 0.0 or y < 0.0 or x > float(width) or y > float(height)
        for x, y in points
    ):
        raise ValueError("geometry receipt quadrilateral must stay on its image")
    tolerance = max(1e-9, float(width * height) * 1e-12)
    corner_cross_products = tuple(
        _receipt_cross_product(
            points[index],
            points[(index + 1) % 4],
            points[(index + 2) % 4],
        )
        for index in range(4)
    )
    signed_double_area = sum(
        point[0] * points[(index + 1) % 4][1]
        - points[(index + 1) % 4][0] * point[1]
        for index, point in enumerate(points)
    )
    if signed_double_area <= tolerance or any(
        value <= tolerance for value in corner_cross_products
    ):
        raise ValueError(
            "geometry receipt quadrilateral must be simple, convex, and clockwise"
        )


def _receipt_cross_product(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (
        second[1] - first[1]
    ) * (third[0] - first[0])


def _inverse_receipt_quadrilateral_rotation(
    points: tuple[tuple[float, float], ...],
    *,
    original_image_size: tuple[int, int],
    clockwise_rotation_degrees: int,
) -> tuple[tuple[float, float], ...]:
    width, height = original_image_size
    transformed: list[tuple[float, float]] = []
    for rotated_x, rotated_y in points:
        if clockwise_rotation_degrees == 0:
            original = (rotated_x, rotated_y)
        elif clockwise_rotation_degrees == 90:
            original = (rotated_y, float(height) - rotated_x)
        elif clockwise_rotation_degrees == 180:
            original = (float(width) - rotated_x, float(height) - rotated_y)
        else:
            original = (float(width) - rotated_y, rotated_x)
        transformed.append(original)
    return tuple(transformed)


def _quadrilaterals_match(
    first: tuple[tuple[float, float], ...],
    second: tuple[tuple[float, float], ...],
) -> bool:
    return all(
        math.isclose(first_value, second_value, rel_tol=0.0, abs_tol=1e-9)
        for first_point, second_point in zip(first, second, strict=True)
        for first_value, second_value in zip(first_point, second_point, strict=True)
    )


def _bboxes_match(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return all(
        math.isclose(first_value, second_value, rel_tol=0.0, abs_tol=1e-9)
        for first_value, second_value in zip(first, second, strict=True)
    )


def _receipt_quadrilateral_to_page_bbox(
    points: tuple[tuple[float, float], ...],
    *,
    image_size: tuple[int, int],
    page_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    image_width, image_height = image_size
    page_x0, page_y0, page_x1, page_y1 = page_bbox
    page_width = page_x1 - page_x0
    page_height = page_y1 - page_y0
    xs = tuple(point[0] for point in points)
    ys = tuple(point[1] for point in points)
    return (
        page_x0 + min(xs) / image_width * page_width,
        page_y0 + min(ys) / image_height * page_height,
        page_x0 + max(xs) / image_width * page_width,
        page_y0 + max(ys) / image_height * page_height,
    )


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


__all__ = [
    "VISUAL_CONTENT_TYPES",
    "VISUAL_TEXT_SCHEMA_VERSION",
    "VisualContentType",
    "VisualLayoutTable",
    "VisualLayoutTableCell",
    "VisualTextGeometryReceipt",
    "VisualTextObservation",
    "VisualTextPageInput",
    "VisualTextSnapshot",
    "stable_visual_text_observation_id",
    "stable_visual_text_snapshot_id",
]
