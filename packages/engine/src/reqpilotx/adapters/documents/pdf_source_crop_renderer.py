from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from enum import Enum
from os import PathLike
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover - the production dependency is pinned
    pymupdf = None  # type: ignore[assignment]


_DEFAULT_SCALE = 2.0
_MAX_OUTPUT_PIXELS = 4_000_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PdfVisiblePageGeometry:
    """Visible page geometry in the same coordinates used by SourceIR."""

    page_number: int
    bounds: tuple[float, float, float, float]
    rotation_cw_degrees: int


class PdfSourceCropRejectionCode(str, Enum):
    PARSER_UNAVAILABLE = "pdf_source_crop_parser_unavailable"
    SOURCE_UNAVAILABLE = "pdf_source_crop_source_unavailable"
    INVALID_PAGE = "pdf_source_crop_invalid_page"
    INVALID_BBOX = "pdf_source_crop_invalid_bbox"
    RESOURCE_BUDGET_EXCEEDED = "pdf_source_crop_resource_budget_exceeded"
    RENDER_FAILED = "pdf_source_crop_render_failed"


class PdfSourceCropRejected(RuntimeError):
    def __init__(self, code: PdfSourceCropRejectionCode, message: str) -> None:
        self.code = code
        super().__init__(message)


def inspect_pdf_page_bounds(
    source_path: str | PathLike[str],
    *,
    expected_source_sha256: str,
) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        page.bounds
        for page in inspect_pdf_page_geometry(
            source_path,
            expected_source_sha256=expected_source_sha256,
        )
    )


def inspect_pdf_page_geometry(
    source_path: str | PathLike[str],
    *,
    expected_source_sha256: str,
) -> tuple[PdfVisiblePageGeometry, ...]:
    """Return visible page bounds and rotation without extracting content.

    Page-comparison overlays use the same rotated ``page.rect`` coordinate
    space as the native PDF adapter.  The source hash is checked again so a
    page box can never be paired with facts from different bytes.
    """

    if pymupdf is None:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.PARSER_UNAVAILABLE,
            "The pinned PDF renderer is unavailable.",
        )
    path = Path(source_path)
    if not path.exists() or not path.is_file():
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.SOURCE_UNAVAILABLE,
            "The source PDF is unavailable.",
        )
    expected_hash = str(expected_source_sha256 or "").strip().lower()
    if not _SHA256_PATTERN.fullmatch(expected_hash):
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.SOURCE_UNAVAILABLE,
            "A valid preview source identity is required.",
        )
    try:
        source_bytes = path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != expected_hash:
            raise PdfSourceCropRejected(
                PdfSourceCropRejectionCode.SOURCE_UNAVAILABLE,
                "The source PDF changed after extraction.",
            )
        document = pymupdf.open(stream=source_bytes, filetype="pdf")
    except PdfSourceCropRejected:
        raise
    except Exception as exc:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.RENDER_FAILED,
            "The source PDF could not be opened for page geometry inspection.",
        ) from exc

    try:
        pages: list[PdfVisiblePageGeometry] = []
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            page_rect = page.rect
            bbox = tuple(
                float(value)
                for value in (page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)
            )
            if (
                any(not math.isfinite(value) for value in bbox)
                or bbox[2] <= bbox[0]
                or bbox[3] <= bbox[1]
            ):
                raise PdfSourceCropRejected(
                    PdfSourceCropRejectionCode.RENDER_FAILED,
                    "The source PDF exposed invalid visible page geometry.",
                )
            rotation = int(getattr(page, "rotation", 0)) % 360
            if rotation not in {0, 90, 180, 270}:
                raise PdfSourceCropRejected(
                    PdfSourceCropRejectionCode.RENDER_FAILED,
                    "The source PDF exposed an unsupported page rotation.",
                )
            pages.append(
                PdfVisiblePageGeometry(
                    page_number=page_index + 1,
                    bounds=bbox,
                    rotation_cw_degrees=rotation,
                )
            )
        return tuple(pages)
    except PdfSourceCropRejected:
        raise
    except Exception as exc:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.RENDER_FAILED,
            "The source PDF page geometry could not be inspected.",
        ) from exc
    finally:
        document.close()


def render_pdf_source_crop(
    source_path: str | PathLike[str],
    *,
    page_number: int,
    bbox: tuple[float, float, float, float],
    expected_source_sha256: str,
    scale: float = _DEFAULT_SCALE,
    annotations: bool = False,
) -> bytes:
    """Render a bounded native PDF region without performing OCR.

    The Shadow UI uses this for image placements that StructureTree has already
    grounded to a unique table cell. Rendering stays on the original PDF bytes;
    it does not create a second extracted-asset store.
    """

    if pymupdf is None:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.PARSER_UNAVAILABLE,
            "The pinned PDF renderer is unavailable.",
        )
    path = Path(source_path)
    if not path.exists() or not path.is_file():
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.SOURCE_UNAVAILABLE,
            "The source PDF is unavailable.",
        )
    expected_hash = str(expected_source_sha256 or "").strip().lower()
    if not _SHA256_PATTERN.fullmatch(expected_hash):
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.SOURCE_UNAVAILABLE,
            "A valid preview source identity is required.",
        )
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.INVALID_PAGE,
            "The PDF crop page must be a positive integer.",
        )
    if (
        not isinstance(bbox, tuple)
        or len(bbox) != 4
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in bbox
        )
    ):
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.INVALID_BBOX,
            "The PDF crop bounding box is invalid.",
        )
    x0, y0, x1, y1 = (float(value) for value in bbox)
    if x1 <= x0 or y1 <= y0:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.INVALID_BBOX,
            "The PDF crop bounding box must have positive area.",
        )
    if (
        not isinstance(scale, (int, float))
        or isinstance(scale, bool)
        or not math.isfinite(float(scale))
        or float(scale) <= 0.0
        or float(scale) > 4.0
    ):
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.RESOURCE_BUDGET_EXCEEDED,
            "The PDF crop scale exceeds the allowed rendering budget.",
        )
    output_pixels = (x1 - x0) * float(scale) * (y1 - y0) * float(scale)
    if output_pixels > _MAX_OUTPUT_PIXELS:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.RESOURCE_BUDGET_EXCEEDED,
            "The PDF crop exceeds the allowed rendering budget.",
        )

    try:
        source_bytes = path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != expected_hash:
            raise PdfSourceCropRejected(
                PdfSourceCropRejectionCode.SOURCE_UNAVAILABLE,
                "The source PDF changed after upload.",
            )
        document = pymupdf.open(stream=source_bytes, filetype="pdf")
    except PdfSourceCropRejected:
        raise
    except Exception as exc:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.RENDER_FAILED,
            "The source PDF could not be opened for bounded rendering.",
        ) from exc
    try:
        if page_number > document.page_count:
            raise PdfSourceCropRejected(
                PdfSourceCropRejectionCode.INVALID_PAGE,
                "The requested PDF crop page is outside the document.",
            )
        page = document.load_page(page_number - 1)
        page_rect = page.rect
        tolerance = 0.5
        if (
            x0 < page_rect.x0 - tolerance
            or y0 < page_rect.y0 - tolerance
            or x1 > page_rect.x1 + tolerance
            or y1 > page_rect.y1 + tolerance
        ):
            raise PdfSourceCropRejected(
                PdfSourceCropRejectionCode.INVALID_BBOX,
                "The requested PDF crop is outside the source page.",
            )
        clip = pymupdf.Rect(
            max(x0, page_rect.x0),
            max(y0, page_rect.y0),
            min(x1, page_rect.x1),
            min(y1, page_rect.y1),
        )
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(float(scale), float(scale)),
            clip=clip,
            alpha=False,
            annots=annotations,
        )
        payload = bytes(pixmap.tobytes("png"))
        if not payload:
            raise PdfSourceCropRejected(
                PdfSourceCropRejectionCode.RENDER_FAILED,
                "The bounded PDF renderer returned no image bytes.",
            )
        return payload
    except PdfSourceCropRejected:
        raise
    except (MemoryError, RuntimeError) as exc:
        raise PdfSourceCropRejected(
            PdfSourceCropRejectionCode.RENDER_FAILED,
            "The bounded PDF region could not be rendered.",
        ) from exc
    finally:
        document.close()


__all__ = [
    "PdfVisiblePageGeometry",
    "PdfSourceCropRejected",
    "PdfSourceCropRejectionCode",
    "inspect_pdf_page_bounds",
    "inspect_pdf_page_geometry",
    "render_pdf_source_crop",
]
