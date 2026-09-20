from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from app.domain.extraction.page_geometry import (
    PdfPageGeometryIndexV1,
    PdfPageGeometryLineV1,
)


@dataclass(frozen=True, slots=True)
class GeometryFocusRegion:
    id: str
    page_number: int
    bbox: tuple[float, float, float, float]
    source_id: str
    start_offset: int | None
    end_offset: int | None
    role: str
    precision: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page_number": self.page_number,
            "bbox": list(self.bbox),
            "source_id": self.source_id,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "role": self.role,
            "precision": self.precision,
        }


@runtime_checkable
class GeometryEvidencePort(Protocol):
    """Read-only access to immutable, source-bound page geometry."""

    @property
    def geometry_index_id(self) -> str: ...

    @property
    def geometry_index_sha256(self) -> str: ...

    def load_page(self, page_number: int) -> PdfPageGeometryIndexV1: ...

    def load_pages(
        self,
        page_numbers: Sequence[int],
    ) -> tuple[PdfPageGeometryIndexV1, ...]: ...

    def lines_intersecting(
        self,
        *,
        page_number: int,
        source_node_id: str,
        start_offset: int,
        end_offset: int,
    ) -> tuple[PdfPageGeometryLineV1, ...]: ...

    def regions_for_content(
        self,
        *,
        content_blocks: Sequence[Mapping[str, Any]],
        source_refs: Sequence[Mapping[str, Any]],
    ) -> tuple[GeometryFocusRegion, ...]: ...

    def reading_evidence_between(
        self,
        *,
        page_number: int,
        start_source_order: int,
        end_source_order: int,
    ) -> tuple[dict[str, Any], ...]: ...


__all__ = ["GeometryEvidencePort", "GeometryFocusRegion"]
