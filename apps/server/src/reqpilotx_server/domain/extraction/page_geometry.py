from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PDF_PAGE_GEOMETRY_SCHEMA_VERSION = "pdf-page-geometry-index/v1"
PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION = "pdf-page-geometry-manifest/v1"
PDF_VISIBLE_COORDINATE_SPACE = "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT"


class _FrozenGeometryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class PdfPageGeometrySourceIdentityV1(_FrozenGeometryModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=200)
    source_ir_schema_version: str = Field(min_length=1, max_length=80)
    adapter_id: str = Field(min_length=1, max_length=160)
    adapter_version: str = Field(min_length=1, max_length=200)


class PdfPageGeometryLineV1(_FrozenGeometryModel):
    source_node_id: str = Field(min_length=1, max_length=200)
    source_text_length: int = Field(ge=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    bbox: tuple[float, float, float, float]
    source_order: int = Field(ge=0)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validated_bbox(value, label="PDF line")

    @model_validator(mode="after")
    def validate_offsets(self) -> PdfPageGeometryLineV1:
        if (
            self.end_offset <= self.start_offset
            or self.end_offset > self.source_text_length
        ):
            raise ValueError(
                "PDF line offsets must be non-empty and within source text"
            )
        return self


class PdfPageGeometryObjectV1(_FrozenGeometryModel):
    source_node_id: str = Field(min_length=1, max_length=200)
    kind: Literal["TABLE", "FIGURE"]
    bbox: tuple[float, float, float, float]
    source_order: int = Field(ge=0)
    table_id: str = Field(default="", max_length=200)
    asset_ref: str = Field(default="", max_length=200)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return _validated_bbox(value, label="PDF object")


class PdfPageGeometryIndexV1(_FrozenGeometryModel):
    """Immutable, source-bound geometry for one visible PDF page."""

    schema_version: Literal["pdf-page-geometry-index/v1"] = (
        PDF_PAGE_GEOMETRY_SCHEMA_VERSION
    )
    source_identity: PdfPageGeometrySourceIdentityV1
    coordinate_space: Literal["PDF_VISIBLE_PAGE_POINTS_TOP_LEFT"] = (
        PDF_VISIBLE_COORDINATE_SPACE
    )
    page_number: int = Field(ge=1)
    page_width: float = Field(gt=0)
    page_height: float = Field(gt=0)
    rotation_cw_degrees: Literal[0, 90, 180, 270] = 0
    text_lines: tuple[PdfPageGeometryLineV1, ...] = ()
    tables: tuple[PdfPageGeometryObjectV1, ...] = ()
    figures: tuple[PdfPageGeometryObjectV1, ...] = ()
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("page_width", "page_height")
    @classmethod
    def validate_page_dimension(cls, value: float) -> float:
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError("PDF page dimensions must be finite")
        return normalized

    @model_validator(mode="after")
    def validate_canonical_identity(self) -> PdfPageGeometryIndexV1:
        if tuple(
            (line.source_order, line.source_node_id, line.start_offset)
            for line in self.text_lines
        ) != tuple(
            sorted(
                (
                    line.source_order,
                    line.source_node_id,
                    line.start_offset,
                )
                for line in self.text_lines
            )
        ):
            raise ValueError("PDF geometry lines must be in canonical source order")
        source_lengths: dict[str, int] = {}
        source_previous_end: dict[str, int] = {}
        line_orders: set[int] = set()
        for line in self.text_lines:
            if line.source_order in line_orders:
                raise ValueError("PDF geometry line source_order must be unique")
            line_orders.add(line.source_order)
            previous_length = source_lengths.setdefault(
                line.source_node_id,
                line.source_text_length,
            )
            if previous_length != line.source_text_length:
                raise ValueError(
                    "one PDF source node must have one immutable text length"
                )
            previous_end = source_previous_end.get(line.source_node_id, -1)
            if line.start_offset < previous_end:
                raise ValueError(
                    "PDF geometry line ranges must be ordered and non-overlapping"
                )
            source_previous_end[line.source_node_id] = line.end_offset
            self._require_bbox_in_visible_page(line.bbox)
        for collection, expected_kind in (
            (self.tables, "TABLE"),
            (self.figures, "FIGURE"),
        ):
            if any(item.kind != expected_kind for item in collection):
                raise ValueError("PDF geometry object collection has the wrong kind")
            if tuple(
                (item.source_order, item.source_node_id) for item in collection
            ) != tuple(
                sorted(
                    (item.source_order, item.source_node_id)
                    for item in collection
                )
            ):
                raise ValueError(
                    "PDF geometry objects must be in canonical source order"
                )
            for item in collection:
                self._require_bbox_in_visible_page(item.bbox)
        expected_sha256 = pdf_page_geometry_sha256(self)
        if self.index_sha256 != expected_sha256:
            raise ValueError(
                "index_sha256 must bind the canonical page geometry payload"
            )
        return self

    def _require_bbox_in_visible_page(
        self,
        bbox: tuple[float, float, float, float],
    ) -> None:
        tolerance = 2.0
        if (
            bbox[0] < -tolerance
            or bbox[1] < -tolerance
            or bbox[2] > self.page_width + tolerance
            or bbox[3] > self.page_height + tolerance
        ):
            raise ValueError(
                "PDF geometry bbox lies outside the visible page coordinate space"
            )


def create_pdf_page_geometry_index(
    *,
    source_identity: PdfPageGeometrySourceIdentityV1,
    page_number: int,
    page_width: float,
    page_height: float,
    rotation_cw_degrees: int,
    text_lines: tuple[PdfPageGeometryLineV1, ...] = (),
    tables: tuple[PdfPageGeometryObjectV1, ...] = (),
    figures: tuple[PdfPageGeometryObjectV1, ...] = (),
) -> PdfPageGeometryIndexV1:
    payload: dict[str, Any] = {
        "schema_version": PDF_PAGE_GEOMETRY_SCHEMA_VERSION,
        "source_identity": source_identity.model_dump(mode="json"),
        "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
        "page_number": page_number,
        "page_width": float(page_width),
        "page_height": float(page_height),
        "rotation_cw_degrees": rotation_cw_degrees,
        "text_lines": [line.model_dump(mode="json") for line in text_lines],
        "tables": [item.model_dump(mode="json") for item in tables],
        "figures": [item.model_dump(mode="json") for item in figures],
    }
    payload["index_sha256"] = hashlib.sha256(
        canonical_pdf_page_geometry_bytes(payload)
    ).hexdigest()
    return PdfPageGeometryIndexV1.model_validate(payload)


def canonical_pdf_page_geometry_bytes(
    value: PdfPageGeometryIndexV1 | dict[str, Any],
) -> bytes:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, PdfPageGeometryIndexV1)
        else dict(value)
    )
    payload.pop("index_sha256", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def pdf_page_geometry_sha256(
    value: PdfPageGeometryIndexV1 | dict[str, Any],
) -> str:
    return hashlib.sha256(canonical_pdf_page_geometry_bytes(value)).hexdigest()


def _validated_bbox(
    value: tuple[float, float, float, float],
    *,
    label: str,
) -> tuple[float, float, float, float]:
    normalized = tuple(float(item) for item in value)
    x0, y0, x1, y1 = normalized
    if (
        any(not math.isfinite(item) for item in normalized)
        or x1 <= x0
        or y1 <= y0
    ):
        raise ValueError(f"{label} bbox must be finite and non-empty")
    return normalized


__all__ = [
    "PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION",
    "PDF_PAGE_GEOMETRY_SCHEMA_VERSION",
    "PDF_VISIBLE_COORDINATE_SPACE",
    "PdfPageGeometryIndexV1",
    "PdfPageGeometryLineV1",
    "PdfPageGeometryObjectV1",
    "PdfPageGeometrySourceIdentityV1",
    "canonical_pdf_page_geometry_bytes",
    "create_pdf_page_geometry_index",
    "pdf_page_geometry_sha256",
]
