from __future__ import annotations

import hashlib
import json
import math
import re
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Any, Callable

from pypdf import PasswordType, PdfReader
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    IndirectObject,
    NameObject,
)

try:
    import pymupdf
except ImportError:  # pragma: no cover - the production dependency is pinned
    pymupdf = None  # type: ignore[assignment]

from reqpilotx.domain.extraction.preflight import (
    DocumentFormat,
    DocumentPreflightReport,
)
from reqpilotx.domain.extraction.pdf_page_furniture import (
    PDF_PAGE_FURNITURE_IMAGE_GEOMETRY_TOLERANCE_RATIO,
    PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES,
    PdfPageFurnitureCandidate,
    PdfPageFurnitureImageCandidate,
    PdfPageFurnitureKind,
    classify_pdf_page_furniture,
    classify_pdf_repeated_image_furniture,
    pdf_image_furniture_diagnostic_code,
    pdf_page_furniture_margin_kind,
)
from reqpilotx.domain.extraction.source_ir import (
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
    PdfTextLineGeometry,
    SourceAlphaFootprint,
    SourceAlphaFootprintRun,
    SourceAssetDerivation,
    SourceAssetDerivationKind,
    SourceAssetKind,
    SourceAssetRef,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_source_alpha_footprint_evidence_sha256,
    stable_table_id,
)
from reqpilotx.domain.source_visual_text import (
    VisualTextPageInput,
    VisualTextSnapshot,
)
from reqpilotx.domain.extraction.visual_table import (
    VisualTableCropInput,
    VisualTableSnapshot,
)
from reqpilotx.domain.extraction.visual_table_reconciliation import (
    visual_table_topology_matches_native,
)
from reqpilotx.ports.source_ir_adapter import (
    SourceIRAdapterRejected,
    SourceIRAdapterRejectionCode,
)
from reqpilotx.ports.visual_text_provider import (
    VisualTextCaptureCancelled,
    VisualTextProviderPort,
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from reqpilotx.ports.visual_table_provider import (
    VisualTableProviderPort,
    VisualTableProviderRejected,
    VisualTableProviderRejectionCode,
)
from reqpilotx.adapters.documents.pdf_page_evidence_cache import PdfPageEvidenceCache


PDF_SOURCE_IR_ADAPTER_ID = "native-pdf-rawdict"
PDF_SOURCE_IR_ADAPTER_VERSION = "1.13.0"
_MAX_PDF_BLOCKS_PER_PAGE = 100_000
_MAX_PDF_SOURCE_NODES = 500_000
_MAX_PDF_PAGE_FURNITURE_SCAN_BLOCKS = _MAX_PDF_SOURCE_NODES
_MAX_PDF_PAGE_FURNITURE_IMAGE_HASH_BYTES = 64 * 1024 * 1024
_MAX_PDF_TABLE_CANDIDATES = 2_000
_MAX_PDF_TABLE_CELLS = 500_000
_MAX_PDF_IMAGE_PLACEMENTS = 50_000
_MAX_PDF_UNIQUE_ASSET_BYTES = 512 * 1024 * 1024
_MAX_PDF_IMAGE_MASK_PROOF_WORK_BYTES = 512 * 1024 * 1024
_MAX_PDF_IMAGE_COMPOSITE_WORK_BYTES = 512 * 1024 * 1024
_MAX_PDF_ALPHA_MASK_ISOLATED_RENDERS = 1_024
_MAX_PDF_ALPHA_FOOTPRINT_PIXELS = 16_777_216
_MAX_PDF_ALPHA_FOOTPRINT_RUNS = 8_192
_MAX_PDF_VISUAL_RENDER_BYTES = 512 * 1024 * 1024
VISUAL_TABLE_MAX_REQUESTS_PER_PAGE = 8
VISUAL_TABLE_MAX_REQUESTS_PER_DOCUMENT = 64
_MAX_PDF_PATTERN_RESOURCES_PER_PAGE = 10_000
_MAX_PDF_PATTERN_XOBJECTS_PER_PATTERN = 10_000
_MAX_PDF_PATTERN_ANALYSIS_STREAM_BYTES = 32 * 1024 * 1024
_MAX_PDF_PATTERN_ANALYSIS_XOBJECTS = 10_000
_MAX_PDF_PATTERN_ANALYSIS_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_PDF_PATTERN_ANALYSIS_IMAGE_PIXELS = 4_000_000
_MAX_PDF_PATTERN_TILE_PIXELS = 262_144
_MAX_PDF_UNIFORM_WHITE_IMAGE_PIXELS = 4_000_000
_MAX_PDF_UNIFORM_WHITE_IMAGE_PROOF_WORK_BYTES = 64 * 1024 * 1024
_FULL_PAGE_IMAGE_LOW_NATIVE_TEXT_CHARACTER_LIMIT = 80
_MIN_PDF_INLINE_HEADER_LOGO_FRAGMENTS = 128
_MAX_PDF_INLINE_HEADER_LOGO_FRAGMENTS = 10_000
_MAX_PDF_INLINE_HEADER_LOGO_OPERATIONS = 100_000
_MAX_PDF_INLINE_HEADER_LOGO_ENCODED_BYTES = 16 * 1024 * 1024
_MAX_PDF_INLINE_HEADER_LOGO_INTRINSIC_WIDTH = 64
_MAX_PDF_INLINE_HEADER_LOGO_PLACEMENT_WIDTH_POINTS = 16.0
_MAX_PDF_INLINE_HEADER_LOGO_PLACEMENT_HEIGHT_POINTS = 0.5
_MAX_PDF_INLINE_HEADER_LOGO_BAND_RATIO = 0.12
_MAX_PDF_INLINE_HEADER_LOGO_WIDTH_RATIO = 0.10
_MAX_PDF_INLINE_HEADER_LOGO_HEIGHT_RATIO = 0.02
_MAX_PDF_INLINE_HEADER_LOGO_AREA_RATIO = 0.002
_MAX_PDF_MRC_LAYERS_PER_PAGE = 256
_MIN_PDF_IMAGE_MOSAIC_OCCURRENCES = 128
_MAX_PDF_IMAGE_MOSAIC_OCCURRENCES_PER_PAGE = 10_000
_MAX_PDF_IMAGE_MOSAIC_PROOF_OCCURRENCES = _MAX_PDF_IMAGE_PLACEMENTS
_MAX_PDF_IMAGE_MOSAIC_UNIQUE_XREFS_PER_PAGE = 4_096
_MAX_PDF_IMAGE_MOSAIC_PROOF_UNIQUE_XREFS = 8_192
_MAX_PDF_IMAGE_MOSAIC_PROOF_ENCODED_BYTES = 16 * 1024 * 1024
_MAX_PDF_IMAGE_MOSAIC_ADJACENCY_CHECKS = 1_000_000
_PDF_IMAGE_MOSAIC_COMPONENT_GAP_POINTS = 16.0
_PDF_IMAGE_MOSAIC_GRID_CELL_POINTS = _PDF_IMAGE_MOSAIC_COMPONENT_GAP_POINTS
_PDF_IMAGE_MOSAIC_MAX_INTRINSIC_WIDTH = 32
_PDF_IMAGE_MOSAIC_MAX_PLACEMENT_WIDTH_POINTS = 16.0
_PDF_IMAGE_MOSAIC_MAX_PLACEMENT_HEIGHT_POINTS = 1.0
_PDF_IMAGE_MOSAIC_MAX_RELATIVE_SCALE_SPREAD = 0.001
_PDF_IMAGE_MOSAIC_MAX_LATTICE_RESIDUAL_PIXELS = 0.05
_VISUAL_TEXT_RENDER_SCALE = 2.0
_VISUAL_TABLE_RENDER_SCALE = 2.0
_VECTOR_FIGURE_RENDER_SCALE = 2.0
_VECTOR_FIGURE_RENDER_MAX_PIXELS = 8_388_608
_MAX_PDF_DRAWINGS_PER_PAGE = 100_000
_MAX_PDF_DRAWING_ITEMS_PER_PAGE = 1_000_000
_MAX_PDF_BBOXLOG_ENTRIES_PER_PAGE = 100_000
_MAX_VECTOR_FIGURE_ADJACENCY_CHECKS = 1_000_000
_MAX_VECTOR_FIGURE_MASK_COMPANION_CHECKS = 100_000
_MAX_QUADRILATERAL_BBOXLOG_MATCH_CHECKS = 1_000_000
_MAX_CONNECTOR_DIAGRAM_ENDPOINT_CHECKS = 100_000
_MAX_CONNECTOR_DIAGRAM_ADJACENCY_CHECKS = 1_000_000
_CONNECTOR_DIAGRAM_MAX_COMPONENT_DRAWINGS = 4_096
_CONNECTOR_DIAGRAM_MAX_ADJACENT_TEXT_BLOCKS = 1_024
_CONNECTOR_DIAGRAM_COMPONENT_GAP_POINTS = 16.0
_CONNECTOR_DIAGRAM_TEXT_GAP_POINTS = 12.0
_CONNECTOR_DIAGRAM_CAPTION_GAP_POINTS = 24.0
_CONNECTOR_DIAGRAM_ARROWHEAD_ENDPOINT_GAP_POINTS = 14.0
_CONNECTOR_DIAGRAM_MIN_SHAFT_LENGTH_POINTS = 32.0
_CONNECTOR_DIAGRAM_MIN_SHAFT_ARROWHEAD_PAIRS = 3
_CONNECTOR_DIAGRAM_ARROWHEAD_MIN_DIMENSION = 2.0
_CONNECTOR_DIAGRAM_ARROWHEAD_MAX_DIMENSION = 24.0
_CONNECTOR_DIAGRAM_ARROWHEAD_MIN_POLYGON_AREA = 4.0
_CONNECTOR_DIAGRAM_MIN_REGION_WIDTH = 80.0
_CONNECTOR_DIAGRAM_MIN_REGION_HEIGHT = 40.0
_CONNECTOR_DIAGRAM_MIN_REGION_AREA = 3_000.0
_CONNECTOR_DIAGRAM_MAX_PAGE_AREA_RATIO = 0.60
_CONNECTOR_DIAGRAM_MAX_DRAWING_PAGE_AREA_RATIO = 0.50
_CONNECTOR_DIAGRAM_HEADER_BAND_RATIO = 0.10
_CONNECTOR_DIAGRAM_FOOTER_BAND_RATIO = 0.92
_CONNECTOR_DIAGRAM_TABLE_EDGE_TOLERANCE_POINTS = 1.0
_CONNECTOR_DIAGRAM_TABLE_MAX_COLUMN_WIDTH_RATIO = 1.05
_CONNECTOR_DIAGRAM_TABLE_MAX_INTER_GROUP_GAP_RATIO = 0.25
_VECTOR_FIGURE_COMPONENT_GAP_POINTS = 32.0
_VECTOR_FIGURE_ENCLOSURE_ATTACHMENT_GAP_POINTS = _VECTOR_FIGURE_COMPONENT_GAP_POINTS
_VECTOR_FIGURE_ZERO_EXTENT_HALF_WIDTH_POINTS = 0.25
_VECTOR_FIGURE_PADDING_POINTS = 2.0
_VECTOR_FIGURE_ANCHOR_MIN_AREA = 1_000.0
_VECTOR_FIGURE_ANCHOR_MIN_WIDTH = 40.0
_VECTOR_FIGURE_ANCHOR_MIN_HEIGHT = 10.0
_VECTOR_FIGURE_ANCHOR_WIDE_WIDTH = 80.0
_VECTOR_FIGURE_ANCHOR_WIDE_HEIGHT = 12.0
_VECTOR_FIGURE_MIN_REGION_AREA = 1_000.0
_VECTOR_FIGURE_MAX_PAGE_AREA_RATIO = 0.80
_VECTOR_FIGURE_THIN_FILL_MAX_DIMENSION = 2.5
_VECTOR_FIGURE_HEADER_BAND_RATIO = 0.10
_VECTOR_FIGURE_FOOTER_BAND_RATIO = 0.92
_VECTOR_FIGURE_MASKED_MIN_COMPANION_OVERLAP_RATIO = 0.93
_VECTOR_FIGURE_COMPANION_MIN_MASKED_OVERLAP_RATIO = 0.975
_SPARSE_FILLED_FIGURE_MIN_COLUMNS = 20
_SPARSE_FILLED_FIGURE_MAX_OCCUPANCY_RATIO = 0.12
_SPARSE_FILLED_FIGURE_MIN_PAGE_AREA_RATIO = 0.10
_SPARSE_FILLED_FIGURE_MAX_PAGE_AREA_RATIO = 0.80
_SPARSE_FILLED_FIGURE_MIN_SIGNIFICANT_FILL_PATHS = 8
_SPARSE_FILLED_FIGURE_MIN_FILL_PATH_AREA = 25.0
_SPARSE_FILLED_FIGURE_MIN_FILL_COVERAGE_RATIO = 0.15
_SPARSE_FILLED_FIGURE_MAX_FILL_PATHS = 4_096
_SPARSE_FILLED_FIGURE_MAX_HORIZONTAL_EDGE_DRIFT = 4.0
_SPARSE_FILLED_FIGURE_MIN_VERTICAL_GAP = -1.0
_SPARSE_FILLED_FIGURE_MAX_VERTICAL_GAP = 8.0
_SPARSE_FILLED_FIGURE_REFERENCE_GAP = 96.0
_SPARSE_FILLED_FIGURE_CAPTION_GAP = 72.0
_QUADRILATERAL_FIGURE_MIN_PATH_DIMENSION = 4.0
_QUADRILATERAL_FIGURE_COMPONENT_GAP_POINTS = 16.0
_QUADRILATERAL_FIGURE_MIN_PATHS = 6
_QUADRILATERAL_FIGURE_MIN_ENCLOSURE_AREA = 8_000.0
_QUADRILATERAL_FIGURE_MIN_REGION_WIDTH = 80.0
_QUADRILATERAL_FIGURE_MIN_REGION_HEIGHT = 40.0
_QUADRILATERAL_FIGURE_MIN_REGION_AREA = 3_000.0
_QUADRILATERAL_FIGURE_MAX_PAGE_AREA_RATIO = 0.80
_QUADRILATERAL_FIGURE_LABEL_GAP_POINTS = 24.0
_QUADRILATERAL_FIGURE_CAPTION_CENTER_MARGIN_RATIO = 0.15
_QUADRILATERAL_FIGURE_BBOXLOG_EDGE_TOLERANCE = 1.0
_QUADRILATERAL_FIGURE_MIN_OWNED_TABLES = 2
_QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_ROWS = 2
_QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_COLUMNS = 2
_QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_PHYSICAL_CELLS = 3
_QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_AREA_RATIO = 0.10
_QUADRILATERAL_FIGURE_MAX_TOTAL_OWNED_TABLE_AREA_RATIO = 0.35
_QUADRILATERAL_TABLE_CAPTION_MAX_OVERLAP_POINTS = 12.0
_MRC_PAGE_RENDER_SCALE = 4.0
_MRC_PAGE_RENDER_MAX_PIXELS = 8_388_608
_MRC_CROSS_API_MIN_BBOX_IOU = 0.97
_MRC_CROSS_API_MAX_CENTER_DELTA_POINTS = 0.05
_MRC_TRANSFORM_ABSOLUTE_TOLERANCE = 0.05
_MRC_ORTHOGONAL_WHITE_RECT_MAX_COUNT = 8
_MRC_ORTHOGONAL_WHITE_RECT_MAX_PAGE_AREA_RATIO = 0.05
_FULL_PAGE_IMAGE_MIN_AREA_RATIO = 0.90
_VECTOR_TEXT_MAX_GLYPH_DIMENSION = 18.0
_VECTOR_TEXT_MAX_GLYPH_AREA = 256.0
_VECTOR_TEXT_MIN_SMALL_FILL_PATHS = 120
_VECTOR_TEXT_MIN_PATH_DENSITY = 1.0 / 5_000.0
_VECTOR_TEXT_MIN_PATH_TO_NATIVE_CHARACTER_RATIO = 0.50
_VECTOR_TEXT_MAX_NATIVE_CJK_RATIO = 0.02
_NATIVE_WATERMARK_MIN_ABS_DIRECTION_COMPONENT = 0.25
_NATIVE_WATERMARK_MAX_ALPHA = 192.0
_VECTOR_TEXT_MIN_NATIVE_CJK_ALLOWANCE = 2
_TABLE_CANDIDATE_OVERLAP_RATIO = 0.80
_STRICT_TABLE_MAX_PAGE_AREA_RATIO = 0.85
_STRICT_TABLE_MAX_REPLACEMENT_AREA_RATIO = 1.75
_STRICT_TABLE_MIN_COMPLETENESS_MULTIPLIER = 1.50
_STRICT_TABLE_MIN_AMBIGUITY_ROW_RATIO = 0.80
_STRICT_TABLE_MIN_AMBIGUITY_CELL_RATIO = 0.25
_STRICT_TABLE_MIN_AMBIGUITY_TEXT_RATIO = 0.90
_STRICT_TABLE_MAX_NEAR_FULL_REPLACEMENT_AREA_RATIO = 0.90
_STRICT_TABLE_MIN_DEDUPLICATED_CHARACTER_COUNT = 128
_STRICT_TABLE_MIN_DEDUPLICATED_CHARACTER_RATIO = 0.95
_STRICT_TABLE_MAX_DEDUPLICATED_EXCESS_RATIO = 0.05
_TABLE_HORIZONTAL_RULE_MAX_THICKNESS_POINTS = 3.0
_TABLE_RULE_BOUNDARY_TOLERANCE_POINTS = 2.0
_TABLE_RULE_INTERVAL_GAP_TOLERANCE_POINTS = 2.0
_TABLE_RULE_MIN_WIDTH_COVERAGE = 0.98
_TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT = 90
_NEAR_FULL_PSEUDO_TABLE_MIN_PAGE_AREA_RATIO = 0.85
_NEAR_FULL_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS = 8
_NEAR_FULL_PSEUDO_TABLE_MIN_CHARACTER_COUNT = 128
_WATERMARK_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO = 0.05
_WATERMARK_PSEUDO_TABLE_MAX_ROWS = 3
_WATERMARK_PSEUDO_TABLE_MAX_COLUMNS = 4
_WATERMARK_PSEUDO_TABLE_MAX_NONEMPTY_CELLS = 3
_WATERMARK_PSEUDO_TABLE_MAX_CHARACTER_COUNT = 32
_SHALLOW_WIDE_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO = 0.10
_SHALLOW_WIDE_PSEUDO_TABLE_MAX_ROWS = 5
_SHALLOW_WIDE_PSEUDO_TABLE_MIN_COLUMNS = 12
_SHALLOW_WIDE_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS = 2
_SHALLOW_WIDE_PSEUDO_TABLE_MAX_ORDINARY_BLOCKS = 4
_SHALLOW_WIDE_PSEUDO_TABLE_MIN_BLOCK_WIDTH_RATIO = 0.75
_MIXED_LAYOUT_TABLE_MIN_PAGE_AREA_RATIO = 0.60
_MIXED_LAYOUT_TABLE_MAX_PAGE_AREA_RATIO = 0.85
_MIXED_LAYOUT_TABLE_MIN_ROWS = 12
_MIXED_LAYOUT_TABLE_MIN_COLUMNS = 5
_MIXED_LAYOUT_TABLE_MIN_SPARSE_ROW_RATIO = 0.60
_MIXED_LAYOUT_TABLE_MIN_DENSE_COLUMN_RATIO = 0.65
_MIXED_LAYOUT_TABLE_MIN_DENSE_ROWS = 3
_MIXED_LAYOUT_TABLE_MIN_GIANT_ROW_HEIGHT_RATIO = 0.40
_MIXED_LAYOUT_TABLE_MAX_NESTED_CHILD_AREA_RATIO = 0.20
_TABLE_GRID_ALIGNMENT_TOLERANCE_POINTS = 0.75
_SAFE_EXTENSION_PATTERN = re.compile(r"^[a-z0-9]{1,12}$")
_PDF_FIGURE_LABEL_PATTERN = re.compile(
    r"(?i)(?:图|fig(?:ure)?\.?)\s*([0-9]+(?:[.\-][0-9]+)*)"
)
_PDF_TABLE_LABEL_PATTERN = re.compile(r"(?i)(?:表|table)\s*([0-9]+(?:[.\-][0-9]+)*)")
_QUADRILATERAL_FIGURE_REFERENCE_PATTERN = re.compile(
    r"(?i)(?:(?:见下|参见|如下|见|see(?:\s+below)?)\s*"
    r"(?:图|fig(?:ure)?\.?)\s*(?P<prefixed>[0-9]+(?:[.\-][0-9]+)*)"
    r"|(?:图|fig(?:ure)?\.?)\s*(?P<direct>[0-9]+(?:[.\-][0-9]+)*)"
    r"\s*(?:描述|示出|显示|depicts?|shows?|illustrates?))"
)
_QUADRILATERAL_FIGURE_CAPTION_PATTERN = re.compile(
    r"(?i)^\s*(?:图|fig(?:ure)?\.?)\s*"
    r"(?P<label>[0-9]+(?:[.\-][0-9]+)*)"
)
_QUADRILATERAL_TABLE_CAPTION_PATTERN = re.compile(
    r"(?i)^\s*(?:表|table)\s*(?P<label>[0-9]+(?:[.\-][0-9]+)*)"
)
_IMAGE_MEDIA_TYPES = {
    "bmp": "image/bmp",
    "gif": "image/gif",
    "jbig2": "image/jbig2",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "jpx": "image/jp2",
    "png": "image/png",
    "pnm": "image/x-portable-anymap",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


class _PdfResourceBudgetExceeded(RuntimeError):
    pass


class PageStagingAuthorityLostError(RuntimeError):
    """The current extraction delivery no longer owns staged-page writes."""


class _ImageMaskMaterializationRejected(ValueError):
    def __init__(self, diagnostic_code: str) -> None:
        super().__init__(diagnostic_code)
        self.diagnostic_code = diagnostic_code


@dataclass(frozen=True, slots=True)
class _VectorTextRisk:
    small_fill_path_count: int


@dataclass(frozen=True, slots=True)
class _PreparedTableRow:
    bbox: tuple[float, float, float, float]
    cells: tuple[tuple[float, float, float, float] | None, ...]


@dataclass(frozen=True, slots=True)
class _PreparedTableCandidate:
    bbox: tuple[float, float, float, float]
    row_count: int
    col_count: int
    rows: tuple[_PreparedTableRow, ...]
    matrix: tuple[tuple[str, ...], ...]

    def extract(self) -> tuple[tuple[str, ...], ...]:
        return self.matrix


@dataclass(frozen=True, slots=True)
class _TableSuppressionTextBlock:
    block_index: int
    kind: SourceNodeKind
    bbox: tuple[float, float, float, float]
    normalized_text: str


@dataclass(frozen=True, slots=True)
class _ProvenTableCellGeometry:
    bbox: tuple[float, float, float, float]
    row_span: int
    column_span: int


@dataclass(frozen=True, slots=True)
class _MaterializedImageAsset:
    content_sha256: str
    byte_count: int
    extension: str
    media_type: str
    content_bytes: bytes
    derivation: SourceAssetDerivation | None = None
    mask_materialization_strategy: str | None = None


@dataclass(frozen=True, slots=True)
class _ImageMaskMaterializationFailure:
    diagnostic_code: str


@dataclass(frozen=True, slots=True)
class _PdfAlphaMaskSourceProof:
    base_xref: int
    mask_xref: int
    native_width: int
    native_height: int
    transform: tuple[float, float, float, float, float, float]
    raw_mask_samples_proven: bool


@dataclass(frozen=True, slots=True)
class _RenderedPdfPage:
    image_bytes: bytes
    image_sha256: str
    width_px: int
    height_px: int
    scale: float


@dataclass(frozen=True, slots=True)
class _VectorDrawingRecord:
    bbox: tuple[float, float, float, float]
    anchor: bool
    rectangle_only: bool


@dataclass(frozen=True, slots=True)
class _StrokedQuadrilateralRecord:
    bbox: tuple[float, float, float, float]
    bboxlog_index: int


@dataclass(frozen=True, slots=True)
class _ConnectorDiagramDrawingRecord:
    drawing_index: int
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class _ConnectorDiagramShaftRecord:
    drawing_index: int
    bbox: tuple[float, float, float, float]
    endpoints: tuple[tuple[float, float], tuple[float, float]]


@dataclass(frozen=True, slots=True)
class _ConnectorDiagramArrowheadRecord:
    drawing_index: int
    bbox: tuple[float, float, float, float]
    vertices: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]


@dataclass(frozen=True, slots=True)
class _VectorFigureRegion:
    bbox: tuple[float, float, float, float]
    drawing_count: int
    anchor_count: int
    anchor_bboxes: tuple[tuple[float, float, float, float], ...]
    proof_kind: str = "curve_seeded"
    owned_table_bboxes: tuple[tuple[float, float, float, float], ...] = ()
    owned_image_block_indexes: tuple[int, ...] = ()
    evidence_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _MaterializedVectorFigure:
    region: _VectorFigureRegion
    rendered: _RenderedPdfPage
    locator: NativeLocator
    node_id: str
    asset_id: str


@dataclass(frozen=True, slots=True)
class _CurveFigureMaskedCompanionOwnership:
    figure: _MaterializedVectorFigure
    companion_block_index: int
    masked_bbox: tuple[float, float, float, float]
    companion_bbox: tuple[float, float, float, float]
    companion_width: int
    companion_height: int
    masked_overlap_ratio: float
    companion_overlap_ratio: float


@dataclass(frozen=True, slots=True)
class _CurveFigureImageOccurrence:
    block_index: int
    bbox: tuple[float, float, float, float]
    width: int
    height: int
    figure: _MaterializedVectorFigure


@dataclass(frozen=True, slots=True)
class _ImageMosaicOccurrence:
    block_index: int
    bbox: tuple[float, float, float, float]
    transform: tuple[float, float, float, float, float, float]
    width: int
    height: int
    xref: int
    content_sha256: str
    decoded_digest: str


@dataclass(frozen=True, slots=True)
class _ImageMosaicRegion:
    bbox: tuple[float, float, float, float]
    occurrences: tuple[_ImageMosaicOccurrence, ...]
    unique_xref_count: int
    scale_x: float
    scale_y: float
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class _MaterializedImageMosaic:
    region: _ImageMosaicRegion
    rendered: _RenderedPdfPage
    locator: NativeLocator
    node_id: str
    asset_id: str


@dataclass(frozen=True, slots=True)
class _MrcImageLayer:
    block_index: int
    xref: int
    bbox: tuple[float, float, float, float]
    native_width: int
    native_height: int
    transform: tuple[float, float, float, float, float, float]
    content_sha256: str
    is_stencil: bool


@dataclass(frozen=True, slots=True)
class _MrcPageCompositePlan:
    base_layer: _MrcImageLayer
    stencil_layers: tuple[_MrcImageLayer, ...]
    preserved_text_block_indexes: tuple[int, ...] = ()
    orthogonal_drawing_count: int = 0

    @property
    def layers(self) -> tuple[_MrcImageLayer, ...]:
        return (self.base_layer, *self.stencil_layers)


@dataclass(slots=True)
class _PdfResourceBudget:
    page_furniture_scan_blocks: int = 0
    source_nodes: int = 0
    table_candidates: int = 0
    table_cells: int = 0
    image_placements: int = 0
    unique_asset_bytes: int = 0
    image_mask_proof_work_bytes: int = 0
    image_composite_work_bytes: int = 0
    alpha_mask_isolated_renders: int = 0
    visual_render_bytes: int = 0
    visual_table_requests: int = 0
    visual_table_requests_by_page: dict[int, int] = field(default_factory=dict)
    image_mosaic_proof_occurrences: int = 0
    image_mosaic_proof_unique_xrefs: int = 0
    image_mosaic_proof_encoded_bytes: int = 0
    uniform_white_image_proof_work_bytes: int = 0

    def consume_page_furniture_scan_blocks(self, count: int) -> None:
        self.page_furniture_scan_blocks += count
        if self.page_furniture_scan_blocks > _MAX_PDF_PAGE_FURNITURE_SCAN_BLOCKS:
            raise _PdfResourceBudgetExceeded

    def consume_source_nodes(self, count: int = 1) -> None:
        self.source_nodes += count
        if self.source_nodes > _MAX_PDF_SOURCE_NODES:
            raise _PdfResourceBudgetExceeded

    def consume_table_candidates(self, count: int) -> None:
        self.table_candidates += count
        if self.table_candidates > _MAX_PDF_TABLE_CANDIDATES:
            raise _PdfResourceBudgetExceeded

    def consume_table_cells(self, count: int) -> None:
        self.table_cells += count
        if self.table_cells > _MAX_PDF_TABLE_CELLS:
            raise _PdfResourceBudgetExceeded

    def consume_image_placement(self) -> None:
        self.image_placements += 1
        if self.image_placements > _MAX_PDF_IMAGE_PLACEMENTS:
            raise _PdfResourceBudgetExceeded

    def consume_unique_asset_bytes(self, count: int) -> None:
        self.unique_asset_bytes += count
        if self.unique_asset_bytes > _MAX_PDF_UNIQUE_ASSET_BYTES:
            raise _PdfResourceBudgetExceeded

    def consume_image_mask_proof_work(
        self,
        *,
        width: int,
        height: int,
        mask_width: int,
        mask_height: int,
    ) -> None:
        # Proof decodes an RGBA/RGB base plus a grayscale mask and may resize
        # both the raw occurrence and its intrinsic XObject representation.
        self.image_mask_proof_work_bytes += (
            width * height * 8 + mask_width * mask_height * 4
        )
        if self.image_mask_proof_work_bytes > _MAX_PDF_IMAGE_MASK_PROOF_WORK_BYTES:
            raise _PdfResourceBudgetExceeded

    def consume_image_composite_work(
        self,
        *,
        width: int,
        height: int,
        mask_width: int,
        mask_height: int,
    ) -> None:
        # Account conservatively for decoded / converted base buffers plus a
        # scaled base, decoded mask, RGBA composite, and encoded output at the
        # mask resolution before asking MuPDF to allocate them. PDF explicit
        # masks may legitimately use a higher resolution than their base image.
        self.image_composite_work_bytes += (
            width * height * 8 + mask_width * mask_height * 16
        )
        if self.image_composite_work_bytes > _MAX_PDF_IMAGE_COMPOSITE_WORK_BYTES:
            raise _PdfResourceBudgetExceeded

    def consume_alpha_mask_isolated_render(self) -> None:
        self.alpha_mask_isolated_renders += 1
        if self.alpha_mask_isolated_renders > _MAX_PDF_ALPHA_MASK_ISOLATED_RENDERS:
            raise _PdfResourceBudgetExceeded

    def consume_visual_render_bytes(self, count: int) -> None:
        # Page/crop pixmaps are materialized and released sequentially.  Their
        # decoded byte size is therefore a per-render peak, not resident bytes
        # accumulated across the whole document.  Persisted PNG output remains
        # bounded separately by ``consume_unique_asset_bytes``.
        self.visual_render_bytes = max(self.visual_render_bytes, count)
        if count > _MAX_PDF_VISUAL_RENDER_BYTES:
            raise _PdfResourceBudgetExceeded

    def reserve_visual_table_request(self, page_number: int) -> bool:
        page_requests = self.visual_table_requests_by_page.get(page_number, 0)
        if (
            page_requests >= VISUAL_TABLE_MAX_REQUESTS_PER_PAGE
            or self.visual_table_requests >= VISUAL_TABLE_MAX_REQUESTS_PER_DOCUMENT
        ):
            return False
        self.visual_table_requests += 1
        self.visual_table_requests_by_page[page_number] = page_requests + 1
        return True

    def reserve_image_mosaic_proof(
        self,
        *,
        occurrence_count: int,
        unique_xref_count: int,
        encoded_byte_count: int,
    ) -> bool:
        next_occurrences = self.image_mosaic_proof_occurrences + occurrence_count
        next_unique_xrefs = self.image_mosaic_proof_unique_xrefs + unique_xref_count
        next_encoded_bytes = self.image_mosaic_proof_encoded_bytes + encoded_byte_count
        if (
            next_occurrences > _MAX_PDF_IMAGE_MOSAIC_PROOF_OCCURRENCES
            or next_unique_xrefs > _MAX_PDF_IMAGE_MOSAIC_PROOF_UNIQUE_XREFS
            or next_encoded_bytes > _MAX_PDF_IMAGE_MOSAIC_PROOF_ENCODED_BYTES
        ):
            return False
        self.image_mosaic_proof_occurrences = next_occurrences
        self.image_mosaic_proof_unique_xrefs = next_unique_xrefs
        self.image_mosaic_proof_encoded_bytes = next_encoded_bytes
        return True

    def reserve_uniform_white_image_proof(
        self,
        *,
        encoded_byte_count: int,
        pixel_count: int,
    ) -> bool:
        if (
            encoded_byte_count < 0
            or pixel_count < 1
            or pixel_count > _MAX_PDF_UNIFORM_WHITE_IMAGE_PIXELS
        ):
            return False
        next_work_bytes = (
            self.uniform_white_image_proof_work_bytes
            + encoded_byte_count
            + pixel_count * 3
        )
        if next_work_bytes > _MAX_PDF_UNIFORM_WHITE_IMAGE_PROOF_WORK_BYTES:
            return False
        self.uniform_white_image_proof_work_bytes = next_work_bytes
        return True


@dataclass(slots=True)
class _PdfPatternAnalysisBudget:
    stream_bytes: int = 0
    xobjects: int = 0
    image_bytes: int = 0
    image_pixels: int = 0
    exhausted: bool = False

    def consume_stream_bytes(self, count: int) -> bool:
        if self.exhausted:
            return False
        self.stream_bytes += count
        if self.stream_bytes > _MAX_PDF_PATTERN_ANALYSIS_STREAM_BYTES:
            self.exhausted = True
            return False
        return True

    def consume_xobjects(self, count: int) -> bool:
        if self.exhausted:
            return False
        self.xobjects += count
        if self.xobjects > _MAX_PDF_PATTERN_ANALYSIS_XOBJECTS:
            self.exhausted = True
            return False
        return True

    def consume_image_bytes(self, count: int) -> bool:
        if self.exhausted:
            return False
        self.image_bytes += count
        if self.image_bytes > _MAX_PDF_PATTERN_ANALYSIS_IMAGE_BYTES:
            self.exhausted = True
            return False
        return True

    def consume_image_pixels(self, count: int) -> bool:
        if self.exhausted:
            return False
        self.image_pixels += count
        if self.image_pixels > _MAX_PDF_PATTERN_ANALYSIS_IMAGE_PIXELS:
            self.exhausted = True
            return False
        return True


@dataclass(frozen=True, slots=True)
class _PdfPatternImageEvidence:
    content_sha256: str
    width: int
    height: int


@dataclass(slots=True)
class _PdfPatternAnalysisState:
    budget: _PdfPatternAnalysisBudget = field(default_factory=_PdfPatternAnalysisBudget)
    image_evidence_by_xref: dict[int, _PdfPatternImageEvidence] = field(
        default_factory=dict
    )
    uniform_opaque_by_xref: dict[int, bool] = field(default_factory=dict)
    pattern_image_hashes_by_xref: dict[int, Counter[str] | None] = field(
        default_factory=dict
    )


class PdfSourceIRAdapter:
    """Map native PyMuPDF ``rawdict`` blocks to immutable SourceIR facts.

    The adapter deliberately performs no cleaning of authoritative raw blocks,
    semantic heading inference or requirement splitting. It keeps native block
    order, raw character text, geometry, and embedded image bytes as the
    source-fidelity boundary for later layers. A native text block is tagged as
    a watermark only when every non-empty span is both diagonal and explicitly
    translucent; its text and geometry remain intact. A detected whole-page
    image risk or dense vector-glyph page with low native Unicode coverage can
    trigger optional remote visual text recognition, whose immutable
    observations remain a sidecar and never overwrite native ``raw_text``.
    Native table detection adds a parallel structural candidate; its cells may
    exclude only character occurrences independently proven by raw geometry to
    belong to a watermark, while the original watermark fact remains intact.
    """

    adapter_id = PDF_SOURCE_IR_ADAPTER_ID
    adapter_version = PDF_SOURCE_IR_ADAPTER_VERSION
    document_format = DocumentFormat.PDF

    def __init__(
        self,
        *,
        visual_text_provider: VisualTextProviderPort | None = None,
        visual_text_page_numbers: frozenset[int]
        | set[int]
        | tuple[int, ...]
        | None = None,
        force_visual_text_page_numbers: frozenset[int]
        | set[int]
        | tuple[int, ...] = (),
        visual_table_provider: VisualTableProviderPort | None = None,
        visual_table_page_numbers: frozenset[int]
        | set[int]
        | tuple[int, ...]
        | None = None,
        visual_table_on_visual_text_pages: bool = False,
        source_asset_publisher: Callable[[SourceAssetRef, bytes], None] | None = None,
        page_progress_callback: Callable[[int, int, int], None] | None = None,
        page_staging_callback: Callable[[dict[str, Any]], None] | None = None,
        native_evidence_observer: Callable[[int, str, dict[str, Any], Any], None] | None = None,
    ) -> None:
        # OCR is an explicit shadow capability.  The default native adapter
        # only records visual-risk diagnostics so a corpus run cannot acquire
        # an unbounded remote-service latency or data-transfer cost by accident.
        self._visual_text_provider = visual_text_provider
        self._visual_text_page_numbers = _normalize_visual_text_page_numbers(
            visual_text_page_numbers
        )
        self._force_visual_text_page_numbers = (
            _normalize_visual_text_page_numbers(force_visual_text_page_numbers)
            or frozenset()
        )
        self._visual_table_provider = visual_table_provider
        self._visual_table_page_numbers = _normalize_visual_text_page_numbers(
            visual_table_page_numbers
        )
        if not isinstance(visual_table_on_visual_text_pages, bool):
            raise TypeError("visual_table_on_visual_text_pages must be boolean")
        self._visual_table_on_visual_text_pages = visual_table_on_visual_text_pages
        self._visual_table_processing_stats: dict[int, dict[str, int]] = {}
        self._page_processing_blocker_reason_codes: dict[int, set[str]] = {}
        if source_asset_publisher is not None and not callable(source_asset_publisher):
            raise TypeError("source_asset_publisher must be callable")
        self._source_asset_publisher = source_asset_publisher
        if native_evidence_observer is not None and not callable(native_evidence_observer):
            raise TypeError("native_evidence_observer must be callable")
        self._native_evidence_observer = native_evidence_observer
        if page_progress_callback is not None and not callable(page_progress_callback):
            raise TypeError("page_progress_callback must be callable")
        self._page_progress_callback = page_progress_callback
        if page_staging_callback is not None and not callable(page_staging_callback):
            raise TypeError("page_staging_callback must be callable")
        self._page_staging_callback = page_staging_callback
        if self._visual_text_page_numbers and visual_text_provider is None:
            raise TypeError(
                "non-empty visual_text_page_numbers requires a visual_text_provider"
            )
        if self._force_visual_text_page_numbers and visual_text_provider is None:
            raise TypeError("forced visual pages require a visual_text_provider")
        if (
            self._visual_text_page_numbers is not None
            and not self._force_visual_text_page_numbers.issubset(
                self._visual_text_page_numbers
            )
        ):
            raise ValueError(
                "forced visual pages must stay inside visual_text_page_numbers"
            )
        if self._visual_table_page_numbers:
            if visual_text_provider is None or self._visual_text_page_numbers is None:
                raise TypeError(
                    "visual_table_page_numbers require an explicit remote visual text route"
                )
            if not self._visual_table_page_numbers.issubset(
                self._visual_text_page_numbers
            ):
                raise ValueError(
                    "visual_table_page_numbers must be a visual text route subset"
                )
        if (
            self._visual_table_on_visual_text_pages
            and self._visual_table_page_numbers is not None
        ):
            raise ValueError(
                "dynamic visual table routing cannot also declare explicit pages"
            )
        if self._visual_table_on_visual_text_pages and visual_table_provider is None:
            raise TypeError(
                "dynamic visual table routing requires a visual_table_provider"
            )
        if (
            visual_table_provider is not None
            and self._visual_table_page_numbers is None
            and not self._visual_table_on_visual_text_pages
        ):
            raise TypeError(
                "visual_table_provider requires an explicit visual_table_page_numbers scope"
            )
        if visual_table_provider is not None and visual_text_provider is None:
            raise TypeError(
                "visual_table_provider requires the page-level visual text route"
            )
        if visual_table_provider is not None and getattr(
            visual_table_provider, "provider_id", None
        ) != getattr(visual_text_provider, "provider_id", None):
            raise TypeError(
                "visual table and page text providers must use the same authorized provider"
            )
        self.adapter_version = _adapter_version_for_visual_provider(
            visual_text_provider,
            page_numbers=self._visual_text_page_numbers,
            forced_page_numbers=self._force_visual_text_page_numbers,
            visual_table_provider=visual_table_provider,
            visual_table_page_numbers=self._visual_table_page_numbers,
            visual_table_on_visual_text_pages=(self._visual_table_on_visual_text_pages),
        )

    def extract(
        self,
        source_path: str | PathLike[str],
        *,
        preflight: DocumentPreflightReport,
        created_at: datetime,
    ) -> SourceIRSnapshot:
        path = Path(source_path)
        self._visual_table_processing_stats = {}
        self._page_processing_blocker_reason_codes = {}
        _validate_created_at(created_at)
        self._validate_preflight(path, preflight)

        # Parse exactly the bytes that were identity-checked.  Opening the path
        # again after hashing would leave a time-of-check/time-of-use window.
        source_bytes = path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if (
            len(source_bytes) != preflight.identity.source_size
            or source_sha256 != preflight.identity.source_sha256
        ):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.SOURCE_CHANGED,
                "The source bytes changed after document preflight.",
            )

        document_id = stable_document_id(source_sha256)
        source_ir_id = stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )
        if pymupdf is None:
            return _failed_snapshot(
                source_ir_id=source_ir_id,
                document_id=document_id,
                source_sha256=source_sha256,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                created_at=created_at,
                code="pdf_native_parser_unavailable",
                message="The pinned PyMuPDF runtime is unavailable.",
            )

        try:
            document = pymupdf.open(stream=source_bytes, filetype="pdf")
        except Exception as exc:
            return _failed_snapshot(
                source_ir_id=source_ir_id,
                document_id=document_id,
                source_sha256=source_sha256,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                created_at=created_at,
                code="pdf_native_open_failed",
                message=f"PyMuPDF could not open the approved PDF ({type(exc).__name__}).",
            )

        nodes: list[SourceNode] = []
        assets_by_id: dict[str, SourceAssetRef] = {}
        visual_text_snapshots: list[VisualTextSnapshot] = []
        visual_table_snapshots: list[VisualTableSnapshot] = []
        dynamic_visual_table_page_numbers: set[int] = set()
        diagnostics: list[SourceDiagnostic] = []
        partial = False
        annotation_count = 0
        whitespace_only_text_block_count = 0
        whitespace_only_text_evidence = hashlib.sha256(
            json.dumps(
                {
                    "schema": "pdf-whitespace-only-text-disposition/v1",
                    "source_sha256": source_sha256,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        resource_budget = _PdfResourceBudget()
        prepared_masked_images: dict[
            tuple[str, str, int, int, int, int, tuple[int, int] | None],
            _MaterializedImageAsset | _ImageMaskMaterializationFailure,
        ] = {}
        uniform_white_image_proofs: dict[tuple[str, int, int], str | None] = {}
        pattern_reader: PdfReader | None = None
        pattern_reader_unavailable = False
        pattern_analysis_state = _PdfPatternAnalysisState()
        try:
            if document.needs_pass and document.authenticate("") <= 0:
                return _failed_snapshot(
                    source_ir_id=source_ir_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    adapter_id=self.adapter_id,
                    adapter_version=self.adapter_version,
                    created_at=created_at,
                    code="pdf_native_authentication_failed",
                    message="PyMuPDF could not apply the preflight-approved empty password.",
                )

            if (
                preflight.page_count is not None
                and preflight.page_count != document.page_count
            ):
                partial = True
                diagnostics.append(
                    _diagnostic(
                        "pdf_page_count_mismatch",
                        (
                            "The native parser page count differs from the preflight page count "
                            f"({document.page_count} versus {preflight.page_count})."
                        ),
                    )
                )

            if document.page_count == 0:
                partial = True
                diagnostics.append(
                    _diagnostic(
                        "pdf_document_without_pages",
                        "The PDF contains no pages and therefore no locatable source facts.",
                    )
                )

            if document.page_count and self._page_progress_callback is not None:
                # Publish the known denominator before the document-wide
                # furniture scan.  On long PDFs that scan is real work; keeping
                # the first heartbeat behind it leaves the UI looking frozen
                # even though the worker has already opened the source.
                self._page_progress_callback(
                    0,
                    document.page_count,
                    1,
                )
            page_furniture_kinds = _pdf_variable_page_marker_kinds(
                document,
                resource_budget=resource_budget,
            )
            for page_index in range(document.page_count):
                page_number = page_index + 1
                page_node_start = len(nodes)
                page_visual_text_start = len(visual_text_snapshots)
                page_visual_table_start = len(visual_table_snapshots)
                page_diagnostic_start = len(diagnostics)
                if page_index and self._page_progress_callback is not None:
                    self._page_progress_callback(
                        page_index,
                        document.page_count,
                        page_number,
                    )
                try:
                    page = document.load_page(page_index)
                except Exception as exc:
                    partial = True
                    diagnostics.append(
                        _diagnostic(
                            "pdf_page_load_failed",
                            f"The native parser could not load page {page_number} ({type(exc).__name__}).",
                            severity=SourceDiagnosticSeverity.ERROR,
                        )
                    )
                    self._publish_page_staging_candidate(
                        source_sha256=source_sha256,
                        page_number=page_number,
                        total_pages=document.page_count,
                        page_bounds=None,
                        nodes=nodes[page_node_start:],
                        assets_by_id=assets_by_id,
                        visual_text_snapshots=visual_text_snapshots[
                            page_visual_text_start:
                        ],
                        visual_table_snapshots=visual_table_snapshots[
                            page_visual_table_start:
                        ],
                        diagnostics=diagnostics[page_diagnostic_start:],
                    )
                    continue
                page = PdfPageEvidenceCache(
                    page,
                    max_table_candidates=_MAX_PDF_TABLE_CANDIDATES,
                    max_table_cell_slots=_MAX_PDF_TABLE_CELLS,
                    evidence_observer=(
                        lambda method, config, result, number=page_number:
                        self._native_evidence_observer(number, method, config, result)
                    ) if self._native_evidence_observer else None,
                )
                page_locator = _page_locator(page_number, page.rect)
                page_rotation_matrix = getattr(page, "rotation_matrix", None)
                page_annotations = getattr(page, "annots", None)
                if callable(page_annotations):
                    try:
                        annotations = page_annotations()
                        if annotations is not None:
                            annotation_count += sum(1 for _ in annotations)
                    except Exception as exc:
                        partial = True
                        diagnostics.append(
                            _diagnostic(
                                "pdf_annotation_scan_failed",
                                (
                                    "The PDF annotation layer could not be inspected on page "
                                    f"{page_number} ({type(exc).__name__})."
                                ),
                                native_locator=page_locator,
                            )
                        )
                try:
                    raw_page = page.get_text("rawdict", sort=False)
                except Exception as exc:
                    partial = True
                    diagnostics.append(
                        _diagnostic(
                            "pdf_page_rawdict_failed",
                            f"Native block extraction failed on page {page_number} ({type(exc).__name__}).",
                            severity=SourceDiagnosticSeverity.ERROR,
                            native_locator=page_locator,
                        )
                    )
                    raw_page = {}

                blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
                if not isinstance(blocks, (list, tuple)):
                    partial = True
                    diagnostics.append(
                        _diagnostic(
                            "pdf_page_blocks_unavailable",
                            f"Page {page_number} did not expose a native block sequence.",
                            native_locator=page_locator,
                        )
                    )
                    blocks = ()
                if len(blocks) > _MAX_PDF_BLOCKS_PER_PAGE:
                    raise _PdfResourceBudgetExceeded

                mrc_composite_plan: _MrcPageCompositePlan | None = None
                mrc_rendered_page: _RenderedPdfPage | None = None
                mrc_preservable_text_block_indexes = frozenset(
                    block_index
                    for block_index, block in enumerate(blocks)
                    if isinstance(block, dict)
                    and block.get("type") == 0
                    and (
                        _visible_text_character_counts(block)[0] == 0
                        or page_furniture_kinds.get((page_number, block_index))
                        in {
                            SourceNodeKind.HEADER,
                            SourceNodeKind.FOOTER,
                            SourceNodeKind.WATERMARK,
                        }
                    )
                )
                try:
                    mrc_composite_plan = _proven_mrc_page_composite_plan(
                        document=document,
                        page=page,
                        blocks=blocks,
                        page_bbox=(
                            page_locator.bbox if page_locator is not None else None
                        ),
                        preservable_text_block_indexes=(
                            mrc_preservable_text_block_indexes
                        ),
                        rotation_matrix=page_rotation_matrix,
                    )
                except MemoryError as exc:
                    raise _PdfResourceBudgetExceeded from exc
                except Exception:
                    # MRC folding is an optional, proof-driven disposition. Any
                    # parser ambiguity keeps the original image placements.
                    mrc_composite_plan = None
                if mrc_composite_plan is not None:
                    try:
                        mrc_rendered_page = _render_pdf_page(
                            page=page,
                            scale=_MRC_PAGE_RENDER_SCALE,
                            max_pixels=_MRC_PAGE_RENDER_MAX_PIXELS,
                            resource_budget=resource_budget,
                        )
                    except _PdfResourceBudgetExceeded:
                        raise
                    except MemoryError as exc:
                        raise _PdfResourceBudgetExceeded from exc
                    except Exception:
                        partial = True
                        diagnostics.append(
                            _diagnostic(
                                "pdf_mrc_page_composite_render_failed",
                                (
                                    f"Page {page_number} has a proven scan-base and "
                                    "paint-stencil layer stack, but its annotation-free "
                                    "page PNG could not be materialized under the strict "
                                    "4x / 8,388,608-pixel policy; original image "
                                    "placements were preserved."
                                ),
                                native_locator=page_locator,
                            )
                        )
                        mrc_composite_plan = None

                mrc_layers_by_block_index = (
                    {layer.block_index: layer for layer in mrc_composite_plan.layers}
                    if mrc_composite_plan is not None
                    else {}
                )
                tiling_pattern_tile_hashes_by_block_index: dict[int, str] = {}
                inline_header_logo_evidence_by_block_index: dict[int, str] = {}
                has_unlocated_image_blocks = _has_unlocated_image_blocks(blocks)
                has_inline_header_logo_candidate = (
                    _has_possible_inline_header_logo_fragments(blocks)
                )
                unlocated_image_content_sha256s = _unlocated_image_content_sha256s(
                    blocks
                )
                if has_unlocated_image_blocks or has_inline_header_logo_candidate:
                    if pattern_reader is None and not pattern_reader_unavailable:
                        try:
                            pattern_reader = _open_pdf_pattern_reader(source_bytes)
                        except MemoryError as exc:
                            raise _PdfResourceBudgetExceeded from exc
                        pattern_reader_unavailable = pattern_reader is None
                    if pattern_reader is not None and has_unlocated_image_blocks:
                        try:
                            tiling_pattern_tile_hashes_by_block_index = (
                                _proven_tiling_pattern_tile_hashes_by_block_index(
                                    document=document,
                                    reader=pattern_reader,
                                    page=page,
                                    page_index=page_index,
                                    blocks=blocks,
                                    analysis_state=pattern_analysis_state,
                                )
                            )
                        except MemoryError as exc:
                            raise _PdfResourceBudgetExceeded from exc
                        except Exception:
                            tiling_pattern_tile_hashes_by_block_index = {}
                    if pattern_reader is not None and has_inline_header_logo_candidate:
                        try:
                            inline_header_logo_evidence_by_block_index = (
                                _proven_inline_header_logo_fragments(
                                    document=document,
                                    reader=pattern_reader,
                                    page=page,
                                    page_index=page_index,
                                    page_number=page_number,
                                    page_bbox=(
                                        page_locator.bbox
                                        if page_locator is not None
                                        else None
                                    ),
                                    rotation_matrix=page_rotation_matrix,
                                    blocks=blocks,
                                    source_sha256=source_sha256,
                                    budget=pattern_analysis_state.budget,
                                )
                            )
                        except MemoryError as exc:
                            raise _PdfResourceBudgetExceeded from exc
                        except Exception:
                            inline_header_logo_evidence_by_block_index = {}

                curve_seeded_figure_regions = _vector_figure_regions_from_page(
                    page=page,
                    page_bbox=(page_locator.bbox if page_locator is not None else None),
                    rotation_matrix=page_rotation_matrix,
                )
                quadrilateral_figure_regions = (
                    _caption_anchored_quadrilateral_figure_regions_from_page(
                        page=page,
                        raw_page=raw_page,
                        page_number=page_number,
                        page_bbox=(
                            page_locator.bbox if page_locator is not None else None
                        ),
                        rotation_matrix=page_rotation_matrix,
                        curve_seeded_regions=curve_seeded_figure_regions,
                        resource_budget=resource_budget,
                    )
                )
                sparse_filled_figure_regions = (
                    _sparse_filled_table_figure_regions_from_page(
                        page=page,
                        raw_page=raw_page,
                        page_number=page_number,
                        page_bbox=(
                            page_locator.bbox if page_locator is not None else None
                        ),
                        rotation_matrix=page_rotation_matrix,
                        resource_budget=resource_budget,
                    )
                )
                connector_diagram_regions = (
                    _caption_anchored_connector_diagram_regions_from_page(
                        page=page,
                        raw_page=raw_page,
                        page_number=page_number,
                        page_bbox=(
                            page_locator.bbox if page_locator is not None else None
                        ),
                        rotation_matrix=page_rotation_matrix,
                        excluded_regions=(
                            *curve_seeded_figure_regions,
                            *quadrilateral_figure_regions,
                            *sparse_filled_figure_regions,
                        ),
                        source_sha256=source_sha256,
                        resource_budget=resource_budget,
                    )
                )
                vector_figure_regions = tuple(
                    sorted(
                        (
                            *curve_seeded_figure_regions,
                            *quadrilateral_figure_regions,
                            *sparse_filled_figure_regions,
                            *connector_diagram_regions,
                        ),
                        key=lambda region: (
                            region.bbox[1],
                            region.bbox[0],
                            region.bbox[3],
                            region.bbox[2],
                            region.proof_kind,
                        ),
                    )
                )
                materialized_vector_figures: list[_MaterializedVectorFigure] = []
                for region in vector_figure_regions:
                    locator = NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        bbox=region.bbox,
                    )
                    node_id = stable_node_id(
                        source_sha256=source_sha256,
                        adapter_id=self.adapter_id,
                        adapter_version=self.adapter_version,
                        kind=SourceNodeKind.FIGURE,
                        native_locator=locator,
                    )
                    try:
                        rendered_figure = _render_pdf_crop(
                            page=page,
                            bbox=region.bbox,
                            scale=_VECTOR_FIGURE_RENDER_SCALE,
                            max_pixels=_VECTOR_FIGURE_RENDER_MAX_PIXELS,
                            resource_budget=resource_budget,
                        )
                    except _PdfResourceBudgetExceeded:
                        raise
                    except MemoryError as exc:
                        raise _PdfResourceBudgetExceeded from exc
                    except Exception:
                        partial = True
                        proof_description = _vector_figure_proof_description(
                            region.proof_kind
                        )
                        diagnostic_code = _vector_figure_diagnostic_code(
                            proof_kind=region.proof_kind,
                            event="crop_render_failed",
                        )
                        diagnostics.append(
                            _diagnostic(
                                diagnostic_code,
                                (
                                    f"Page {page_number} has a {proof_description} "
                                    "region, but its bounded annotation-free crop could "
                                    "not be materialized; original source facts were "
                                    "preserved."
                                ),
                                native_locator=locator,
                                evidence_sha256=region.evidence_sha256,
                            )
                        )
                        continue
                    resource_budget.consume_image_placement()
                    materialized_vector_figures.append(
                        _MaterializedVectorFigure(
                            region=region,
                            rendered=rendered_figure,
                            locator=locator,
                            node_id=node_id,
                            asset_id=_stable_asset_id(
                                content_sha256=rendered_figure.image_sha256,
                                media_type="image/png",
                            ),
                        )
                    )

                curve_figure_masked_companion_ownership = (
                    _curve_figure_masked_companion_ownership(
                        figures=materialized_vector_figures,
                        blocks=blocks,
                        rotation_matrix=page_rotation_matrix,
                    )
                )
                materialized_image_mosaic: _MaterializedImageMosaic | None = None
                if (
                    mrc_composite_plan is None
                    and not tiling_pattern_tile_hashes_by_block_index
                    and not materialized_vector_figures
                ):
                    image_mosaic_region = _fragmented_image_mosaic_region_from_page(
                        document=document,
                        page=page,
                        page_number=page_number,
                        blocks=blocks,
                        page_bbox=(
                            page_locator.bbox if page_locator is not None else None
                        ),
                        rotation_matrix=page_rotation_matrix,
                        resource_budget=resource_budget,
                    )
                    if image_mosaic_region is not None:
                        mosaic_locator = NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=page_number,
                            bbox=image_mosaic_region.bbox,
                        )
                        mosaic_node_id = stable_node_id(
                            source_sha256=source_sha256,
                            adapter_id=self.adapter_id,
                            adapter_version=self.adapter_version,
                            kind=SourceNodeKind.FIGURE,
                            native_locator=mosaic_locator,
                        )
                        try:
                            rendered_mosaic = _render_pdf_crop(
                                page=page,
                                bbox=image_mosaic_region.bbox,
                                scale=_VECTOR_FIGURE_RENDER_SCALE,
                                max_pixels=_VECTOR_FIGURE_RENDER_MAX_PIXELS,
                                resource_budget=resource_budget,
                            )
                        except _PdfResourceBudgetExceeded:
                            raise
                        except MemoryError as exc:
                            raise _PdfResourceBudgetExceeded from exc
                        except Exception:
                            partial = True
                            diagnostics.append(
                                _diagnostic(
                                    "pdf_fragmented_image_mosaic_crop_render_failed",
                                    (
                                        f"Page {page_number} has a proven fragmented "
                                        "image mosaic, but its bounded annotation-free "
                                        "crop could not be materialized; original image "
                                        "occurrences were preserved."
                                    ),
                                    native_locator=mosaic_locator,
                                    evidence_sha256=(
                                        image_mosaic_region.evidence_sha256
                                    ),
                                )
                            )
                        else:
                            resource_budget.consume_image_placement()
                            materialized_image_mosaic = _MaterializedImageMosaic(
                                region=image_mosaic_region,
                                rendered=rendered_mosaic,
                                locator=mosaic_locator,
                                node_id=mosaic_node_id,
                                asset_id=_stable_asset_id(
                                    content_sha256=(rendered_mosaic.image_sha256),
                                    media_type="image/png",
                                ),
                            )

                image_mosaic_block_indexes = frozenset(
                    occurrence.block_index
                    for occurrence in (
                        materialized_image_mosaic.region.occurrences
                        if materialized_image_mosaic is not None
                        else ()
                    )
                )
                alpha_mask_image_infos: tuple[Any, ...] | None = None
                page_node_count = 0
                page_invisible_span_count = 0
                page_invisible_character_count = 0
                page_visible_character_count = 0
                page_visible_cjk_character_count = 0
                page_full_image_block_count = 0
                for block_index, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        partial = True
                        diagnostics.append(
                            _diagnostic(
                                "pdf_block_invalid",
                                f"Page {page_number} block {block_index} is not a native block object.",
                                native_locator=NativeLocator(
                                    format=NativeDocumentFormat.PDF,
                                    page_number=page_number,
                                    block_index=block_index,
                                ),
                            )
                        )
                        continue

                    bbox = _pdf_visual_bbox(
                        _native_bbox(block.get("bbox")),
                        rotation_matrix=page_rotation_matrix,
                    )
                    locator = NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=block_index,
                        bbox=bbox,
                    )
                    block_type = block.get("type")
                    if block_type == 0:
                        raw_text = _raw_text_from_block(block)
                        text_kind = _native_pdf_text_kind(
                            block,
                            fallback=page_furniture_kinds.get(
                                (page_number, block_index),
                                SourceNodeKind.PARAGRAPH,
                            ),
                        )
                        (
                            invisible_span_count,
                            invisible_character_count,
                        ) = _invisible_text_statistics(block)
                        page_invisible_span_count += invisible_span_count
                        page_invisible_character_count += invisible_character_count
                        (
                            visible_character_count,
                            visible_cjk_character_count,
                        ) = _visible_text_character_counts(block)
                        page_visible_character_count += visible_character_count
                        page_visible_cjk_character_count += visible_cjk_character_count
                        if not raw_text.strip():
                            whitespace_only_text_block_count += 1
                            whitespace_record = json.dumps(
                                {
                                    "page_number": page_number,
                                    "block_index": block_index,
                                    "bbox": (
                                        list(_rounded_bbox(bbox))
                                        if bbox is not None
                                        else None
                                    ),
                                    "codepoint_count": len(raw_text),
                                    "line_break_count": raw_text.count("\n"),
                                },
                                ensure_ascii=True,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                            whitespace_only_text_evidence.update(
                                len(whitespace_record).to_bytes(8, "big")
                            )
                            whitespace_only_text_evidence.update(whitespace_record)
                            continue
                        node_id = stable_node_id(
                            source_sha256=source_sha256,
                            adapter_id=self.adapter_id,
                            adapter_version=self.adapter_version,
                            kind=text_kind,
                            native_locator=locator,
                        )
                        local_diagnostics: list[SourceDiagnostic] = []
                        if invisible_span_count:
                            partial = True
                            invisible_text = _diagnostic(
                                "pdf_invisible_text_span_detected",
                                (
                                    f"Page {page_number} block {block_index} contains "
                                    f"{invisible_span_count} alpha-zero native text span(s) "
                                    "whose raw characters were preserved without claiming "
                                    "visual equivalence."
                                ),
                                source_node_id=node_id,
                                native_locator=locator,
                            )
                            local_diagnostics.append(invisible_text)
                            diagnostics.append(invisible_text)
                        if bbox is None:
                            partial = True
                            missing_bbox = _diagnostic(
                                "pdf_block_bbox_missing",
                                f"Page {page_number} block {block_index} has no usable bounding box.",
                                source_node_id=node_id,
                                native_locator=locator,
                            )
                            local_diagnostics.append(missing_bbox)
                            diagnostics.append(missing_bbox)
                        nodes.append(
                            SourceNode(
                                source_node_id=node_id,
                                kind=text_kind,
                                source_order=len(nodes),
                                raw_text=raw_text,
                                native_locator=locator,
                                visual_locator=locator if bbox is not None else None,
                                pdf_text_lines=_pdf_text_lines_from_block(
                                    block,
                                    rotation_matrix=page_rotation_matrix,
                                ),
                                extraction_diagnostics=tuple(local_diagnostics),
                            )
                        )
                        resource_budget.consume_source_nodes()
                        page_node_count += 1
                        continue

                    if block_type == 1:
                        resource_budget.consume_image_placement()
                        mrc_layer = mrc_layers_by_block_index.get(block_index)
                        if mrc_layer is not None:
                            if mrc_layer.is_stencil:
                                # The complete layer disposition is emitted with
                                # the base node so diagnostics remain bound to
                                # the single page-render fact.
                                continue
                            assert mrc_composite_plan is not None
                            assert mrc_rendered_page is not None
                            assert page_locator is not None
                            page_full_image_block_count += 1
                            asset_id = _stable_page_render_asset_id(
                                source_sha256=source_sha256,
                                page_number=page_number,
                                content_sha256=(mrc_rendered_page.image_sha256),
                            )
                            node_id = stable_node_id(
                                source_sha256=source_sha256,
                                adapter_id=self.adapter_id,
                                adapter_version=self.adapter_version,
                                kind=SourceNodeKind.IMAGE,
                                native_locator=page_locator,
                            )
                            layer_block_indexes = tuple(
                                layer.block_index for layer in mrc_composite_plan.layers
                            )
                            stencil_block_indexes = tuple(
                                layer.block_index
                                for layer in mrc_composite_plan.stencil_layers
                            )
                            composite_evidence_sha256 = _mrc_composite_evidence_sha256(
                                source_sha256=source_sha256,
                                page_number=page_number,
                                rendered_page=mrc_rendered_page,
                                plan=mrc_composite_plan,
                            )
                            local_diagnostics = [
                                _diagnostic(
                                    "pdf_mrc_scan_page_composited",
                                    (
                                        f"Page {page_number} folded "
                                        f"{len(layer_block_indexes)} original image "
                                        f"layer(s) at block locators "
                                        f"{list(layer_block_indexes)} (scan base "
                                        f"{mrc_composite_plan.base_layer.block_index}; "
                                        f"paint stencils {list(stencil_block_indexes)}) "
                                    "into one annotation-free 4x PAGE_RENDER PNG; "
                                        "native text blocks retained separately "
                                        f"{list(mrc_composite_plan.preserved_text_block_indexes)}; "
                                        "orthogonal drawing count "
                                        f"{mrc_composite_plan.orthogonal_drawing_count}; "
                                        f"render size is {mrc_rendered_page.width_px}x"
                                        f"{mrc_rendered_page.height_px}."
                                    ),
                                    severity=SourceDiagnosticSeverity.INFO,
                                    source_node_id=node_id,
                                    native_locator=page_locator,
                                    evidence_sha256=composite_evidence_sha256,
                                )
                            ]
                            for source_layer in mrc_composite_plan.layers:
                                source_layer_locator = NativeLocator(
                                    format=NativeDocumentFormat.PDF,
                                    page_number=page_number,
                                    block_index=source_layer.block_index,
                                    bbox=source_layer.bbox,
                                )
                                local_diagnostics.append(
                                    _diagnostic(
                                        "pdf_mrc_source_layer_folded",
                                        (
                                            f"Page {page_number} block "
                                            f"{source_layer.block_index} was proven "
                                            f"to be the {'paint stencil' if source_layer.is_stencil else 'full-page scan base'} "
                                            "of the page-render composite and is not "
                                            "a standalone figure/content asset."
                                        ),
                                        severity=SourceDiagnosticSeverity.INFO,
                                        source_node_id=node_id,
                                        native_locator=source_layer_locator,
                                        evidence_sha256=(
                                            _mrc_layer_evidence_sha256(
                                                source_sha256=source_sha256,
                                                page_number=page_number,
                                                layer=source_layer,
                                            )
                                        ),
                                    )
                                )
                            diagnostics.extend(local_diagnostics)
                            resource_budget.consume_unique_asset_bytes(
                                len(mrc_rendered_page.image_bytes)
                            )
                            asset = SourceAssetRef(
                                asset_id=asset_id,
                                kind=SourceAssetKind.PAGE_RENDER,
                                content_sha256=(mrc_rendered_page.image_sha256),
                                media_type="image/png",
                                storage_key=_logical_page_render_storage_key(
                                    source_sha256=source_sha256,
                                    page_number=page_number,
                                    content_sha256=(mrc_rendered_page.image_sha256),
                                ),
                                native_locator=page_locator,
                            )
                            assets_by_id[asset_id] = asset
                            self._publish_source_asset(
                                asset,
                                mrc_rendered_page.image_bytes,
                            )
                            nodes.append(
                                SourceNode(
                                    source_node_id=node_id,
                                    kind=SourceNodeKind.IMAGE,
                                    source_order=len(nodes),
                                    raw_text="",
                                    native_locator=page_locator,
                                    visual_locator=page_locator,
                                    asset_ref=asset_id,
                                    extraction_diagnostics=tuple(local_diagnostics),
                                )
                            )
                            resource_budget.consume_source_nodes()
                            page_node_count += 1
                            continue
                        tiling_pattern_tile_sha256 = (
                            tiling_pattern_tile_hashes_by_block_index.get(block_index)
                        )
                        if tiling_pattern_tile_sha256 is not None:
                            diagnostics.append(
                                _diagnostic(
                                    "pdf_tiling_pattern_tile_excluded",
                                    (
                                        f"Page {page_number} block {block_index} is an "
                                        "internal image tile of a native tiling pattern; "
                                        "it was excluded from standalone content image "
                                        "placements without altering the source PDF."
                                    ),
                                    severity=SourceDiagnosticSeverity.INFO,
                                    native_locator=locator,
                                    evidence_sha256=tiling_pattern_tile_sha256,
                                )
                            )
                            continue
                        inline_header_logo_evidence_sha256 = (
                            inline_header_logo_evidence_by_block_index.get(block_index)
                        )
                        if inline_header_logo_evidence_sha256 is not None:
                            diagnostics.append(
                                _diagnostic(
                                    "pdf_inline_header_logo_fragment_excluded",
                                    (
                                        f"Page {page_number} block {block_index} is "
                                        "one source-bound, one-row inline raster "
                                        "fragment in a compact header logo; the "
                                        "fragment was excluded from standalone "
                                        "semantic image nodes without altering the "
                                        "source PDF or page render."
                                    ),
                                    severity=SourceDiagnosticSeverity.INFO,
                                    native_locator=locator,
                                    evidence_sha256=(
                                        inline_header_logo_evidence_sha256
                                    ),
                                )
                            )
                            continue
                        raw_mask = block.get("mask")
                        compound_figure = _vector_figure_for_image_occurrence(
                            materialized_vector_figures,
                            bbox=bbox,
                            block_index=block_index,
                            has_mask=raw_mask is not None,
                        )
                        if compound_figure is not None:
                            proof_description = _vector_figure_proof_description(
                                compound_figure.region.proof_kind
                            )
                            diagnostic_code = _vector_figure_diagnostic_code(
                                proof_kind=compound_figure.region.proof_kind,
                                event="image_occurrence_folded",
                            )
                            ownership_description = (
                                "explicitly owned by"
                                if compound_figure.region.proof_kind
                                == "caption_anchored_quadrilateral"
                                else "materially contained by"
                            )
                            diagnostics.append(
                                _diagnostic(
                                    diagnostic_code,
                                    (
                                        f"Page {page_number} block {block_index} is "
                                        f"{ownership_description} a {proof_description} "
                                        "crop and was not emitted as a duplicate "
                                        "standalone image occurrence."
                                    ),
                                    severity=SourceDiagnosticSeverity.INFO,
                                    source_node_id=compound_figure.node_id,
                                    native_locator=locator,
                                    evidence_sha256=(
                                        compound_figure.region.evidence_sha256
                                        or compound_figure.rendered.image_sha256
                                    ),
                                )
                            )
                            continue
                        if raw_mask is None:
                            if (
                                materialized_image_mosaic is not None
                                and block_index in image_mosaic_block_indexes
                            ):
                                diagnostics.append(
                                    _diagnostic(
                                        "pdf_fragmented_image_mosaic_occurrence_folded",
                                        (
                                            f"Page {page_number} block {block_index} "
                                            "is one proven direct one-row image "
                                            "occurrence in a single native raster "
                                            "mosaic and was not emitted as a "
                                            "standalone image."
                                        ),
                                        severity=SourceDiagnosticSeverity.INFO,
                                        source_node_id=(
                                            materialized_image_mosaic.node_id
                                        ),
                                        native_locator=locator,
                                        evidence_sha256=(
                                            materialized_image_mosaic.region.evidence_sha256
                                        ),
                                    )
                                )
                                continue
                        image_bytes = _image_bytes(block.get("image"))
                        if image_bytes is None:
                            partial = True
                            diagnostics.append(
                                _diagnostic(
                                    "pdf_image_bytes_unavailable",
                                    (
                                        f"Page {page_number} block {block_index} is a visual image "
                                        "object without extractable embedded bytes."
                                    ),
                                    native_locator=locator,
                                )
                            )
                            continue
                        if (
                            raw_mask is None
                            and bbox is not None
                            and not _is_full_page_image_bbox(
                                image_bbox=bbox,
                                page_bbox=(
                                    page_locator.bbox
                                    if page_locator is not None
                                    else None
                                ),
                            )
                            and hashlib.sha256(image_bytes).hexdigest()
                            not in unlocated_image_content_sha256s
                        ):
                            uniform_white_evidence_sha256 = (
                                _uniform_opaque_white_image_evidence_sha256(
                                    image_bytes=image_bytes,
                                    width=block.get("width"),
                                    height=block.get("height"),
                                    cache=uniform_white_image_proofs,
                                    resource_budget=resource_budget,
                                )
                            )
                            if uniform_white_evidence_sha256 is not None:
                                diagnostics.append(
                                    _diagnostic(
                                        "pdf_uniform_opaque_white_image_excluded",
                                        (
                                            f"Page {page_number} block {block_index} "
                                            "is a mask-free embedded image whose "
                                            "bounded decode proved every pixel to be "
                                            "opaque white; it was excluded as a "
                                            "standalone semantic image without "
                                            "altering the source PDF or page render."
                                        ),
                                        severity=SourceDiagnosticSeverity.INFO,
                                        native_locator=locator,
                                        evidence_sha256=(uniform_white_evidence_sha256),
                                    )
                                )
                                continue
                        if _is_full_page_image_bbox(
                            image_bbox=bbox,
                            page_bbox=page_locator.bbox
                            if page_locator is not None
                            else None,
                        ):
                            page_full_image_block_count += 1
                        extension = _safe_image_extension(block.get("ext"))
                        materialized_image = _MaterializedImageAsset(
                            content_sha256=hashlib.sha256(image_bytes).hexdigest(),
                            byte_count=len(image_bytes),
                            extension=extension,
                            media_type=_IMAGE_MEDIA_TYPES.get(
                                extension,
                                "application/octet-stream",
                            ),
                            content_bytes=image_bytes,
                        )
                        mask_bytes = _image_bytes(raw_mask)
                        mask_materialization_dimensions: (
                            tuple[int, int, int, int] | None
                        ) = None
                        mask_materialization_diagnostic_code: str | None = None
                        alpha_mask_source_proof: _PdfAlphaMaskSourceProof | None = None
                        if raw_mask is not None and mask_bytes is None:
                            mask_materialization_diagnostic_code = (
                                "pdf_image_mask_invalid_bytes"
                            )
                            partial = True
                        elif mask_bytes is not None:
                            try:
                                width, height = _native_image_dimensions(
                                    width=block.get("width"),
                                    height=block.get("height"),
                                )
                                mask_width, mask_height = _png_image_dimensions(
                                    mask_bytes
                                )
                                mask_materialization_dimensions = (
                                    width,
                                    height,
                                    mask_width,
                                    mask_height,
                                )
                            except _ImageMaskMaterializationRejected as exc:
                                mask_materialization_diagnostic_code = (
                                    exc.diagnostic_code
                                )
                                partial = True
                            else:
                                resource_budget.consume_image_mask_proof_work(
                                    width=width,
                                    height=height,
                                    mask_width=mask_width,
                                    mask_height=mask_height,
                                )
                                try:
                                    base_has_alpha = bool(
                                        pymupdf.Pixmap(image_bytes).alpha
                                    )
                                except MemoryError as exc:
                                    raise _PdfResourceBudgetExceeded from exc
                                except Exception:
                                    base_has_alpha = False
                                if base_has_alpha:
                                    if alpha_mask_image_infos is None:
                                        try:
                                            raw_image_infos = page.get_image_info(
                                                hashes=True,
                                                xrefs=True,
                                            )
                                            alpha_mask_image_infos = tuple(
                                                raw_image_infos
                                            )
                                        except MemoryError as exc:
                                            raise _PdfResourceBudgetExceeded from exc
                                        except Exception:
                                            alpha_mask_image_infos = ()
                                    alpha_mask_source_proof = (
                                        _proven_pdf_alpha_mask_source(
                                            document=document,
                                            block=block,
                                            image_infos=alpha_mask_image_infos,
                                            image_bytes=image_bytes,
                                            mask_bytes=mask_bytes,
                                            expected_width=width,
                                            expected_height=height,
                                            expected_mask_width=mask_width,
                                            expected_mask_height=mask_height,
                                            resource_budget=resource_budget,
                                        )
                                    )
                                masked_image_key = (
                                    materialized_image.content_sha256,
                                    hashlib.sha256(mask_bytes).hexdigest(),
                                    width,
                                    height,
                                    mask_width,
                                    mask_height,
                                    (
                                        (
                                            alpha_mask_source_proof.base_xref,
                                            alpha_mask_source_proof.mask_xref,
                                        )
                                        if alpha_mask_source_proof is not None
                                        else None
                                    ),
                                )
                                cached_outcome = prepared_masked_images.get(
                                    masked_image_key
                                )
                                if cached_outcome is None:
                                    if mask_width < width or mask_height < height:
                                        cached_outcome = _ImageMaskMaterializationFailure(
                                            diagnostic_code=(
                                                "pdf_image_mask_lower_resolution_unsupported"
                                            )
                                        )
                                    else:
                                        resource_budget.consume_image_composite_work(
                                            width=width,
                                            height=height,
                                            mask_width=mask_width,
                                            mask_height=mask_height,
                                        )
                                        try:
                                            cached_outcome = (
                                                _materialize_alpha_mask_composite(
                                                    image_bytes=image_bytes,
                                                    mask_bytes=mask_bytes,
                                                    expected_width=width,
                                                    expected_height=height,
                                                    expected_mask_width=mask_width,
                                                    expected_mask_height=mask_height,
                                                    source_bytes=source_bytes,
                                                    alpha_mask_source_proof=(
                                                        alpha_mask_source_proof
                                                    ),
                                                    resource_budget=(resource_budget),
                                                )
                                            )
                                        except MemoryError as exc:
                                            raise _PdfResourceBudgetExceeded from exc
                                        except _PdfResourceBudgetExceeded:
                                            raise
                                        except _ImageMaskMaterializationRejected as exc:
                                            cached_outcome = (
                                                _ImageMaskMaterializationFailure(
                                                    diagnostic_code=(
                                                        exc.diagnostic_code
                                                    )
                                                )
                                            )
                                        except Exception:
                                            cached_outcome = _ImageMaskMaterializationFailure(
                                                diagnostic_code=(
                                                    "pdf_image_mask_materialization_failed"
                                                )
                                            )
                                    prepared_masked_images[masked_image_key] = (
                                        cached_outcome
                                    )
                                if isinstance(
                                    cached_outcome,
                                    _ImageMaskMaterializationFailure,
                                ):
                                    mask_materialization_diagnostic_code = (
                                        cached_outcome.diagnostic_code
                                    )
                                    partial = True
                                else:
                                    materialized_image = cached_outcome
                        masked_companion_ownership = (
                            curve_figure_masked_companion_ownership.get(block_index)
                        )
                        if (
                            masked_companion_ownership is not None
                            and mask_materialization_diagnostic_code is None
                            and materialized_image.derivation is not None
                        ):
                            diagnostics.append(
                                _diagnostic(
                                    (
                                        "pdf_vector_figure_masked_companion_"
                                        "occurrence_folded"
                                    ),
                                    (
                                        f"Page {page_number} block {block_index} "
                                        "was successfully materialized with its mask, "
                                        "uniquely paired with a materially overlapping "
                                        "mask-free one-pixel companion already owned by "
                                        "one curve-seeded compound figure, and was not "
                                        "emitted as a duplicate standalone image."
                                    ),
                                    severity=SourceDiagnosticSeverity.INFO,
                                    source_node_id=(
                                        masked_companion_ownership.figure.node_id
                                    ),
                                    native_locator=locator,
                                    evidence_sha256=(
                                        _curve_figure_masked_companion_evidence_sha256(
                                            source_sha256=source_sha256,
                                            page_number=page_number,
                                            masked_block_index=block_index,
                                            ownership=(masked_companion_ownership),
                                            materialized_image=materialized_image,
                                        )
                                    ),
                                )
                            )
                            continue
                        content_sha256 = materialized_image.content_sha256
                        extension = materialized_image.extension
                        media_type = materialized_image.media_type
                        asset_id = _stable_asset_id(
                            content_sha256=content_sha256,
                            media_type=media_type,
                        )
                        node_id = stable_node_id(
                            source_sha256=source_sha256,
                            adapter_id=self.adapter_id,
                            adapter_version=self.adapter_version,
                            kind=SourceNodeKind.IMAGE,
                            native_locator=locator,
                        )
                        local_diagnostics = []
                        image_furniture_kind = page_furniture_kinds.get(
                            (page_number, block_index)
                        )
                        if image_furniture_kind in {
                            SourceNodeKind.HEADER,
                            SourceNodeKind.FOOTER,
                        }:
                            furniture_diagnostic = (
                                _repeated_pdf_image_furniture_diagnostic(
                                    kind=(
                                        PdfPageFurnitureKind.HEADER
                                        if image_furniture_kind
                                        is SourceNodeKind.HEADER
                                        else PdfPageFurnitureKind.FOOTER
                                    ),
                                    source_sha256=source_sha256,
                                    page_number=page_number,
                                    block_index=block_index,
                                    node_id=node_id,
                                    locator=locator,
                                    asset_content_sha256=(
                                        materialized_image.content_sha256
                                    ),
                                )
                            )
                            local_diagnostics.append(furniture_diagnostic)
                            diagnostics.append(furniture_diagnostic)
                        if bbox is None:
                            partial = True
                            missing_bbox = _diagnostic(
                                "pdf_block_bbox_missing",
                                f"Page {page_number} block {block_index} has no usable bounding box.",
                                source_node_id=node_id,
                                native_locator=locator,
                            )
                            local_diagnostics.append(missing_bbox)
                            diagnostics.append(missing_bbox)
                        if mask_materialization_diagnostic_code is not None:
                            mask_diagnostic = _diagnostic(
                                mask_materialization_diagnostic_code,
                                (
                                    "The embedded image has a separate mask, but its "
                                    "rendered composite could not be materialized under "
                                    f"the strict policy ({mask_materialization_diagnostic_code}); "
                                    "the primary "
                                    "bytes were preserved without claiming visual "
                                    "equivalence."
                                ),
                                source_node_id=node_id,
                                native_locator=locator,
                            )
                            local_diagnostics.append(mask_diagnostic)
                            diagnostics.append(mask_diagnostic)
                        elif materialized_image.derivation is not None:
                            if mask_materialization_dimensions is None:
                                raise RuntimeError(
                                    "mask derivation lacks validated dimensions"
                                )
                            mask_diagnostic = _diagnostic(
                                "pdf_image_mask_materialized",
                                (
                                    "The embedded image and its separate mask were "
                                    "materialized in memory as a PNG composite and "
                                    "recorded with component lineage."
                                ),
                                severity=SourceDiagnosticSeverity.INFO,
                                source_node_id=node_id,
                                native_locator=locator,
                                evidence_sha256=(
                                    _image_mask_materialization_evidence_sha256(
                                        source_sha256=source_sha256,
                                        page_number=page_number,
                                        block_index=block_index,
                                        materialized_image=materialized_image,
                                        source_proof=alpha_mask_source_proof,
                                        dimensions=(mask_materialization_dimensions),
                                    )
                                ),
                            )
                            local_diagnostics.append(mask_diagnostic)
                            diagnostics.append(mask_diagnostic)
                        if asset_id not in assets_by_id:
                            resource_budget.consume_unique_asset_bytes(
                                materialized_image.byte_count
                            )
                            asset = SourceAssetRef(
                                asset_id=asset_id,
                                kind=SourceAssetKind.IMAGE,
                                content_sha256=content_sha256,
                                media_type=media_type,
                                storage_key=_logical_image_storage_key(
                                    content_sha256=content_sha256,
                                    extension=extension,
                                ),
                            )
                            assets_by_id[asset_id] = asset
                            self._publish_source_asset(
                                asset,
                                materialized_image.content_bytes,
                            )
                        nodes.append(
                            SourceNode(
                                source_node_id=node_id,
                                kind=SourceNodeKind.IMAGE,
                                source_order=len(nodes),
                                raw_text="",
                                native_locator=locator,
                                visual_locator=locator if bbox is not None else None,
                                asset_ref=asset_id,
                                asset_derivation=materialized_image.derivation,
                                extraction_diagnostics=tuple(local_diagnostics),
                            )
                        )
                        resource_budget.consume_source_nodes()
                        page_node_count += 1
                        continue

                    partial = True
                    diagnostics.append(
                        _diagnostic(
                            "pdf_native_block_type_unsupported",
                            (
                                f"Page {page_number} block {block_index} has unsupported native "
                                f"block type {block_type!r}."
                            ),
                            native_locator=locator,
                        )
                    )

                for vector_figure in materialized_vector_figures:
                    if vector_figure.region.proof_kind == "curve_seeded":
                        diagnostic_code = "pdf_vector_figure_materialized"
                        diagnostic_message = (
                            f"Page {page_number} grouped "
                            f"{vector_figure.region.drawing_count} nearby native "
                            "drawing path(s), including "
                            f"{vector_figure.region.anchor_count} material curve "
                            "anchor(s), into one annotation-free compound FIGURE crop."
                        )
                    elif (
                        vector_figure.region.proof_kind == "sparse_filled_table_figure"
                    ):
                        diagnostic_code = "pdf_sparse_filled_figure_materialized"
                        diagnostic_message = (
                            f"Page {page_number} retyped "
                            f"{len(vector_figure.region.owned_table_bboxes)} sparse "
                            "unproven table-layout candidate(s), grounded by one native "
                            "Figure label and "
                            f"{vector_figure.region.drawing_count} material fill path(s), "
                            "as one annotation-free FIGURE crop."
                        )
                    elif (
                        vector_figure.region.proof_kind
                        == "caption_anchored_connector_diagram"
                    ):
                        diagnostic_code = _vector_figure_diagnostic_code(
                            proof_kind=vector_figure.region.proof_kind,
                            event="materialized",
                        )
                        diagnostic_message = (
                            f"Page {page_number} grouped "
                            f"{vector_figure.region.drawing_count} visible native "
                            "drawing path(s), grounded by "
                            f"{vector_figure.region.anchor_count} uniquely matched "
                            "connector-shaft and filled-arrowhead pair(s) and one "
                            "immediately following Figure caption, as one "
                            "annotation-free FIGURE crop."
                        )
                    else:
                        diagnostic_code = _vector_figure_diagnostic_code(
                            proof_kind=vector_figure.region.proof_kind,
                            event="materialized",
                        )
                        diagnostic_message = (
                            f"Page {page_number} grouped "
                            f"{vector_figure.region.drawing_count} visible native "
                            "stroked quadrilateral path(s), including "
                            f"{vector_figure.region.anchor_count} material "
                            "enclosure(s), under one same-number preceding Figure "
                            "reference and following Figure caption as one "
                            "annotation-free FIGURE crop."
                        )
                    local_diagnostics = (
                        _diagnostic(
                            diagnostic_code,
                            diagnostic_message,
                            severity=SourceDiagnosticSeverity.INFO,
                            source_node_id=vector_figure.node_id,
                            native_locator=vector_figure.locator,
                            evidence_sha256=(
                                vector_figure.region.evidence_sha256
                                or vector_figure.rendered.image_sha256
                            ),
                        ),
                    )
                    if vector_figure.asset_id not in assets_by_id:
                        resource_budget.consume_unique_asset_bytes(
                            len(vector_figure.rendered.image_bytes)
                        )
                        asset = SourceAssetRef(
                            asset_id=vector_figure.asset_id,
                            kind=SourceAssetKind.FIGURE,
                            content_sha256=vector_figure.rendered.image_sha256,
                            media_type="image/png",
                            storage_key=_logical_image_storage_key(
                                content_sha256=(vector_figure.rendered.image_sha256),
                                extension="png",
                            ),
                            native_locator=vector_figure.locator,
                        )
                        assets_by_id[vector_figure.asset_id] = asset
                        self._publish_source_asset(
                            asset,
                            vector_figure.rendered.image_bytes,
                        )
                    nodes.append(
                        SourceNode(
                            source_node_id=vector_figure.node_id,
                            kind=SourceNodeKind.FIGURE,
                            source_order=len(nodes),
                            raw_text="",
                            native_locator=vector_figure.locator,
                            visual_locator=vector_figure.locator,
                            asset_ref=vector_figure.asset_id,
                            extraction_diagnostics=local_diagnostics,
                        )
                    )
                    diagnostics.extend(local_diagnostics)
                    resource_budget.consume_source_nodes()
                    page_node_count += 1

                if materialized_image_mosaic is not None:
                    local_diagnostics = (
                        _diagnostic(
                            "pdf_fragmented_image_mosaic_materialized",
                            (
                                f"Page {page_number} grouped "
                                f"{len(materialized_image_mosaic.region.occurrences)} "
                                "direct one-row image occurrence(s), backed by "
                                f"{materialized_image_mosaic.region.unique_xref_count} "
                                "uniquely bound native image XObject(s), into one "
                                "annotation-free FIGURE crop."
                            ),
                            severity=SourceDiagnosticSeverity.INFO,
                            source_node_id=materialized_image_mosaic.node_id,
                            native_locator=materialized_image_mosaic.locator,
                            evidence_sha256=(
                                materialized_image_mosaic.region.evidence_sha256
                            ),
                        ),
                    )
                    if materialized_image_mosaic.asset_id not in assets_by_id:
                        resource_budget.consume_unique_asset_bytes(
                            len(materialized_image_mosaic.rendered.image_bytes)
                        )
                        asset = SourceAssetRef(
                            asset_id=materialized_image_mosaic.asset_id,
                            kind=SourceAssetKind.FIGURE,
                            content_sha256=(
                                materialized_image_mosaic.rendered.image_sha256
                            ),
                            media_type="image/png",
                            storage_key=_logical_image_storage_key(
                                content_sha256=(
                                    materialized_image_mosaic.rendered.image_sha256
                                ),
                                extension="png",
                            ),
                            native_locator=(materialized_image_mosaic.locator),
                        )
                        assets_by_id[materialized_image_mosaic.asset_id] = asset
                        self._publish_source_asset(
                            asset,
                            materialized_image_mosaic.rendered.image_bytes,
                        )
                    nodes.append(
                        SourceNode(
                            source_node_id=materialized_image_mosaic.node_id,
                            kind=SourceNodeKind.FIGURE,
                            source_order=len(nodes),
                            raw_text="",
                            native_locator=materialized_image_mosaic.locator,
                            visual_locator=materialized_image_mosaic.locator,
                            asset_ref=materialized_image_mosaic.asset_id,
                            extraction_diagnostics=local_diagnostics,
                        )
                    )
                    diagnostics.extend(local_diagnostics)
                    resource_budget.consume_source_nodes()
                    page_node_count += 1

                vector_text_risk = _dense_vector_text_risk(
                    page=page,
                    page_bbox=(page_locator.bbox if page_locator is not None else None),
                    native_visible_character_count=page_visible_character_count,
                    native_visible_cjk_character_count=(
                        page_visible_cjk_character_count
                    ),
                )
                if vector_text_risk is not None:
                    partial = True
                    diagnostics.append(
                        _diagnostic(
                            "pdf_vector_text_with_low_native_unicode_coverage",
                            (
                                f"Page {page_number} contains "
                                f"{vector_text_risk.small_fill_path_count} dense small "
                                "fill-path object(s), while visible native text exposes "
                                f"{page_visible_character_count} character(s), including "
                                f"{page_visible_cjk_character_count} CJK character(s); "
                                "native text remains unchanged and requires visual "
                                "reconciliation."
                            ),
                            native_locator=page_locator,
                        )
                    )

                full_page_image_low_native_text = bool(
                    page_full_image_block_count
                    and 0
                    < page_visible_character_count
                    < _FULL_PAGE_IMAGE_LOW_NATIVE_TEXT_CHARACTER_LIMIT
                )
                if full_page_image_low_native_text:
                    # Neutral readability signal only.  Formal extraction keeps
                    # its existing visual-routing policy; source-capture may
                    # offer opt-in OCR/Layout for this page.
                    diagnostics.append(
                        _diagnostic(
                            "pdf_full_page_image_with_low_native_text_coverage",
                            (
                                f"Page {page_number} contains "
                                f"{page_full_image_block_count} whole-page image block(s), "
                                "while the visible native layer exposes only "
                                f"{page_visible_character_count} character(s), including "
                                f"{page_visible_cjk_character_count} CJK character(s); this "
                                "is a neutral candidate for opt-in visual text capture."
                            ),
                            severity=SourceDiagnosticSeverity.INFO,
                            native_locator=page_locator,
                        )
                    )

                image_requires_visual_text = bool(
                    page_full_image_block_count
                    and (page_invisible_span_count or page_visible_character_count == 0)
                )
                forced_visual_text = page_number in self._force_visual_text_page_numbers
                requires_visual_text = (
                    image_requires_visual_text
                    or vector_text_risk is not None
                    or forced_visual_text
                )
                if requires_visual_text:
                    partial = True
                    if forced_visual_text:
                        diagnostics.append(
                            _diagnostic(
                                "pdf_visual_text_user_forced",
                                (
                                    f"Page {page_number} was explicitly selected "
                                    "for visual reprocessing."
                                ),
                                severity=SourceDiagnosticSeverity.INFO,
                                native_locator=page_locator,
                            )
                        )
                    if image_requires_visual_text and page_invisible_span_count:
                        diagnostics.append(
                            _diagnostic(
                                "pdf_full_page_image_with_invisible_text_layer",
                                (
                                    f"Page {page_number} contains "
                                    f"{page_full_image_block_count} whole-page image block(s) and "
                                    f"{page_invisible_span_count} alpha-zero text span(s) "
                                    f"({page_invisible_character_count} native character(s)); "
                                    "native text remains unchanged and requires visual reconciliation."
                                ),
                                native_locator=page_locator,
                            )
                        )
                    elif image_requires_visual_text:
                        diagnostics.append(
                            _diagnostic(
                                "pdf_full_page_image_without_visible_text",
                                (
                                    f"Page {page_number} contains "
                                    f"{page_full_image_block_count} whole-page image block(s) "
                                    "without trusted visible native text and requires visual "
                                    "text recognition."
                                ),
                                native_locator=page_locator,
                            )
                        )
                    if (
                        self._visual_text_page_numbers is not None
                        and page_number not in self._visual_text_page_numbers
                    ):
                        diagnostics.append(
                            _diagnostic(
                                "pdf_visual_text_page_out_of_scope",
                                (
                                    f"Page {page_number} requires visual text recognition "
                                    "but is outside the explicitly selected shadow scope."
                                ),
                                severity=SourceDiagnosticSeverity.INFO,
                                native_locator=page_locator,
                            )
                        )
                    else:
                        visual_snapshot, visual_diagnostics = (
                            _visual_text_snapshot_from_page(
                                page=page,
                                page_number=page_number,
                                page_locator=page_locator,
                                source_bytes=source_bytes,
                                source_sha256=source_sha256,
                                provider=self._visual_text_provider,
                                resource_budget=resource_budget,
                                rendered_page=mrc_rendered_page,
                            )
                        )
                        diagnostics.extend(visual_diagnostics)
                        if visual_snapshot is not None:
                            visual_text_snapshots.append(visual_snapshot)
                            if (
                                image_requires_visual_text
                                and "FIGURE"
                                not in visual_snapshot.evaluated_content_types
                            ):
                                figure_diagnostic = _diagnostic(
                                    "pdf_visual_figure_content_not_evaluated",
                                    (
                                        f"Page {page_number} is a full-page raster "
                                        "without trusted native text, but the adopted "
                                        "visual provider evaluated text only. OCR text "
                                        "remains available; image and figure regions "
                                        "require a page-layout or visual-understanding "
                                        "provider before this page is complete."
                                    ),
                                    native_locator=page_locator,
                                )
                                diagnostics.append(figure_diagnostic)
                                self._record_page_processing_blockers(
                                    page_number,
                                    (figure_diagnostic,),
                                )

                # A table candidate is a structural view over the same page
                # facts.  Raw blocks above intentionally remain in SourceIR,
                # including text that also appears in table cells.  Later
                # layers can reconcile those views without losing provenance.
                table_first_source_order = len(nodes)
                table_nodes, table_diagnostics, tables_partial = _table_nodes_from_page(
                    page=page,
                    raw_page=raw_page,
                    page_rotation_matrix=page_rotation_matrix,
                    page_number=page_number,
                    page_locator=page_locator,
                    source_sha256=source_sha256,
                    adapter_id=self.adapter_id,
                    adapter_version=self.adapter_version,
                    first_source_order=table_first_source_order,
                    resource_budget=resource_budget,
                )
                if materialized_vector_figures and table_nodes:
                    folded_table_node_ids: set[str] = set()
                    retained_table_nodes: list[SourceNode] = []
                    for table_node in table_nodes:
                        compound_figure = _vector_figure_containing_bbox(
                            materialized_vector_figures,
                            bbox=table_node.native_locator.bbox,
                            minimum_containment_ratio=0.80,
                        )
                        if compound_figure is None:
                            retained_table_nodes.append(table_node)
                            continue
                        if compound_figure.region.proof_kind != "curve_seeded":
                            if not _vector_figure_owns_table_candidate(
                                table_node=table_node,
                                figure=compound_figure,
                            ):
                                retained_table_nodes.append(table_node)
                                continue
                        elif _table_candidate_owns_all_vector_anchors(
                            table_node=table_node,
                            figure=compound_figure,
                        ):
                            retained_table_nodes.append(table_node)
                            continue
                        folded_table_node_ids.add(table_node.source_node_id)
                        proof_description = _vector_figure_proof_description(
                            compound_figure.region.proof_kind
                        )
                        diagnostic_code = _vector_figure_diagnostic_code(
                            proof_kind=compound_figure.region.proof_kind,
                            event="table_candidate_folded",
                        )
                        diagnostics.append(
                            _diagnostic(
                                diagnostic_code,
                                (
                                    f"Native table candidate "
                                    f"{table_node.native_locator.table_index} on page "
                                    f"{page_number} is materially contained by a "
                                    f"{proof_description} and was not emitted as a "
                                    "duplicate semantic table."
                                ),
                                severity=SourceDiagnosticSeverity.INFO,
                                native_locator=table_node.native_locator,
                                evidence_sha256=(
                                    compound_figure.region.evidence_sha256
                                    or compound_figure.rendered.image_sha256
                                ),
                            )
                        )
                    table_nodes = [
                        table_node.model_copy(
                            update={
                                "source_order": (
                                    table_first_source_order + retained_index
                                )
                            }
                        )
                        for retained_index, table_node in enumerate(
                            retained_table_nodes
                        )
                    ]
                    table_diagnostics = [
                        diagnostic
                        for diagnostic in table_diagnostics
                        if diagnostic.source_node_id not in folded_table_node_ids
                    ]
                    # A folded detector artifact is no longer part of the
                    # SourceIR table view.  Do not retain its stale partial
                    # flag after its node-scoped diagnostics are removed.
                    tables_partial = any(
                        diagnostic.severity is not SourceDiagnosticSeverity.INFO
                        for diagnostic in table_diagnostics
                    )
                resource_budget.consume_source_nodes(len(table_nodes))
                nodes.extend(table_nodes)
                diagnostics.extend(table_diagnostics)
                page_node_count += len(table_nodes)
                partial = partial or tables_partial

                page_has_visual_text_snapshot = any(
                    snapshot.page_number == page_number
                    for snapshot in visual_text_snapshots[page_visual_text_start:]
                )
                visual_table_page_selected = bool(
                    (
                        self._visual_table_page_numbers is not None
                        and page_number in self._visual_table_page_numbers
                    )
                    or (
                        self._visual_table_on_visual_text_pages
                        and page_has_visual_text_snapshot
                    )
                )
                if visual_table_page_selected:
                    table_stats = {
                        "candidate_count": len(table_nodes),
                        "attempt_count": 0,
                        "ready_count": 0,
                        "failed_count": 0,
                        "budget_skipped_count": 0,
                    }
                    self._visual_table_processing_stats[page_number] = table_stats
                if visual_table_page_selected and table_nodes:
                    if self._visual_table_on_visual_text_pages:
                        dynamic_visual_table_page_numbers.add(page_number)
                    for table_node in table_nodes:
                        if not resource_budget.reserve_visual_table_request(
                            page_number
                        ):
                            partial = True
                            table_stats["budget_skipped_count"] += 1
                            budget_diagnostic = _diagnostic(
                                "pdf_visual_table_request_budget_exceeded",
                                (
                                    "Visual table parsing was not called for native "
                                    f"table {table_node.native_locator.table_index} on "
                                    f"page {page_number}; the strict limit is "
                                    f"{VISUAL_TABLE_MAX_REQUESTS_PER_PAGE} table crops "
                                    "per page and "
                                    f"{VISUAL_TABLE_MAX_REQUESTS_PER_DOCUMENT} per document."
                                ),
                                severity=SourceDiagnosticSeverity.ERROR,
                                source_node_id=table_node.source_node_id,
                                native_locator=table_node.native_locator,
                            )
                            diagnostics.append(budget_diagnostic)
                            self._record_page_processing_blockers(
                                page_number,
                                (budget_diagnostic,),
                            )
                            continue
                        table_stats["attempt_count"] += 1
                        visual_table_snapshot, visual_table_diagnostics = (
                            _visual_table_snapshot_from_native_table(
                                page=page,
                                page_number=page_number,
                                page_locator=page_locator,
                                table_node=table_node,
                                source_bytes=source_bytes,
                                source_sha256=source_sha256,
                                provider=self._visual_table_provider,
                                resource_budget=resource_budget,
                            )
                        )
                        diagnostics.extend(visual_table_diagnostics)
                        self._record_page_processing_blockers(
                            page_number,
                            visual_table_diagnostics,
                        )
                        if visual_table_snapshot is None:
                            partial = True
                            table_stats["failed_count"] += 1
                            continue
                        visual_table_snapshots.append(visual_table_snapshot)
                        if not visual_table_topology_matches_native(
                            visual_table_snapshot,
                            table_node,
                        ):
                            partial = True
                            table_stats["failed_count"] += 1
                            topology_diagnostic = _diagnostic(
                                "pdf_visual_table_topology_mismatch",
                                (
                                    f"Visual table topology did not exactly match native "
                                    f"table {table_node.native_locator.table_index} on page "
                                    f"{page_number}; no visual cell text can be projected."
                                ),
                                severity=SourceDiagnosticSeverity.ERROR,
                                source_node_id=table_node.source_node_id,
                                native_locator=table_node.native_locator,
                            )
                            diagnostics.append(topology_diagnostic)
                            self._record_page_processing_blockers(
                                page_number,
                                (topology_diagnostic,),
                            )
                        else:
                            table_stats["ready_count"] += 1

                if page_node_count == 0:
                    partial = True
                    diagnostics.append(
                        _diagnostic(
                            "pdf_page_without_locatable_content",
                            (
                                f"Page {page_number} produced no text or embedded-image source "
                                "facts; blank, vector-only, or unsupported content may be present."
                            ),
                            native_locator=page_locator,
                        )
                    )
                self._publish_page_staging_candidate(
                    source_sha256=source_sha256,
                    page_number=page_number,
                    total_pages=document.page_count,
                    page_bounds=page_locator.bbox,
                    nodes=nodes[page_node_start:],
                    assets_by_id=assets_by_id,
                    visual_text_snapshots=visual_text_snapshots[
                        page_visual_text_start:
                    ],
                    visual_table_snapshots=visual_table_snapshots[
                        page_visual_table_start:
                    ],
                    diagnostics=diagnostics[page_diagnostic_start:],
                )
            if document.page_count and self._page_progress_callback is not None:
                self._page_progress_callback(
                    document.page_count,
                    document.page_count,
                    document.page_count,
                )
            if whitespace_only_text_block_count:
                diagnostics.append(
                    _diagnostic(
                        "pdf_whitespace_only_text_blocks_ignored",
                        (
                            "The PDF contained "
                            f"{whitespace_only_text_block_count} native text block(s) "
                            "made only of Unicode whitespace; they were recorded in "
                            "content-free disposition evidence and excluded from "
                            "semantic SourceIR nodes."
                        ),
                        severity=SourceDiagnosticSeverity.INFO,
                        evidence_sha256=whitespace_only_text_evidence.hexdigest(),
                    )
                )
            if annotation_count:
                diagnostics.append(
                    _diagnostic(
                        "pdf_annotations_ignored_by_policy",
                        (
                            f"The PDF contains {annotation_count} annotation object(s); their "
                            "contents were intentionally excluded from SourceIR by policy."
                        ),
                        severity=SourceDiagnosticSeverity.INFO,
                    )
                )
        except (_PdfResourceBudgetExceeded, MemoryError):
            return _failed_snapshot(
                source_ir_id=source_ir_id,
                document_id=document_id,
                source_sha256=source_sha256,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                created_at=created_at,
                code="pdf_resource_budget_exceeded",
                message=(
                    "The validated PDF exceeds the safe native block, page-furniture "
                    "scan, table, vector-drawing adjacency, node, or "
                    "embedded-asset/visual-render work budget."
                ),
            )
        finally:
            document.close()

        return SourceIRSnapshot(
            source_ir_id=source_ir_id,
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            status=SourceIRStatus.PARTIAL if partial else SourceIRStatus.COMPLETE,
            nodes=tuple(nodes),
            assets=tuple(assets_by_id.values()),
            visual_text_snapshots=tuple(visual_text_snapshots),
            visual_table_page_numbers=tuple(
                sorted(
                    dynamic_visual_table_page_numbers
                    if self._visual_table_on_visual_text_pages
                    else (self._visual_table_page_numbers or ())
                )
            ),
            visual_table_snapshots=tuple(visual_table_snapshots),
            diagnostics=tuple(diagnostics),
            created_at=created_at,
        )

    def _record_page_processing_blockers(
        self,
        page_number: int,
        page_diagnostics: tuple[SourceDiagnostic, ...] | list[SourceDiagnostic],
    ) -> None:
        """Retain content-free page blocker codes for the frozen-route receipt."""

        blocker_codes = {
            diagnostic.code
            for diagnostic in page_diagnostics
            if diagnostic.severity is not SourceDiagnosticSeverity.INFO
            and (
                diagnostic.code == "pdf_visual_figure_content_not_evaluated"
                or diagnostic.code.startswith("pdf_visual_table_")
            )
        }
        if blocker_codes:
            self._page_processing_blocker_reason_codes.setdefault(
                page_number,
                set(),
            ).update(blocker_codes)

    def _publish_page_staging_candidate(
        self,
        *,
        source_sha256: str,
        page_number: int,
        total_pages: int,
        page_bounds: tuple[float, float, float, float] | None,
        nodes: list[SourceNode],
        assets_by_id: dict[str, SourceAssetRef],
        visual_text_snapshots: list[VisualTextSnapshot],
        visual_table_snapshots: list[VisualTableSnapshot],
        diagnostics: list[SourceDiagnostic],
    ) -> None:
        """Emit one immutable source-only page slice after page processing.

        The callback is outside SourceIR identity construction. It contains no
        Structure or semantic decisions and is never read back into the
        adapter, so enabling staged inspection cannot change formal facts.
        """

        if self._page_staging_callback is None:
            return
        referenced_asset_ids = {
            node.asset_ref for node in nodes if node.asset_ref is not None
        }
        candidate = {
            "schema_version": "pdf-source-page-staging-candidate/v1",
            "source_sha256": source_sha256,
            "adapter": {
                "id": self.adapter_id,
                "version": self.adapter_version,
            },
            "page_number": page_number,
            "total_pages": total_pages,
            "bounds": list(page_bounds) if page_bounds is not None else None,
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "assets": [
                assets_by_id[asset_id].model_dump(mode="json")
                for asset_id in sorted(referenced_asset_ids)
                if asset_id in assets_by_id
            ],
            "visual_text_snapshots": [
                snapshot.model_dump(mode="json") for snapshot in visual_text_snapshots
            ],
            "visual_table_snapshots": [
                snapshot.model_dump(mode="json") for snapshot in visual_table_snapshots
            ],
            "diagnostics": [
                diagnostic.model_dump(mode="json") for diagnostic in diagnostics
            ],
        }
        try:
            self._page_staging_callback(candidate)
        except PageStagingAuthorityLostError:
            # A stale/cancelled delivery must stop before it can publish any
            # further formal result under an authority it no longer owns.
            raise
        except Exception:
            # Completed-page browsing is a UX side channel. Ordinary metadata
            # or object-store failures must never change formal SourceIR.
            return

    def _publish_source_asset(
        self,
        asset: SourceAssetRef,
        content_bytes: bytes,
    ) -> None:
        """Publish exact bytes only when the application supplied a sink.

        The native adapter remains read-only by default.  Production
        orchestration can inject an object-store boundary without changing
        SourceIR identity or allowing a logical ``storage_key`` to claim bytes
        that were never durably materialized.
        """

        if self._source_asset_publisher is None:
            return
        if (
            not content_bytes
            or hashlib.sha256(content_bytes).hexdigest() != asset.content_sha256
        ):
            raise RuntimeError("source asset bytes do not match SourceIR identity")
        self._source_asset_publisher(asset, content_bytes)

    def _validate_preflight(
        self,
        path: Path,
        preflight: DocumentPreflightReport,
    ) -> None:
        if not isinstance(preflight, DocumentPreflightReport):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_PREFLIGHT,
                "A DocumentPreflightReport is required before native PDF parsing.",
            )
        identity = preflight.identity
        if (
            not preflight.processable
            or identity.detected_format is not self.document_format
            or identity.source_name != path.name
            or identity.declared_extension != path.suffix.lower()
        ):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_PREFLIGHT,
                "The preflight report does not authorize this PDF source path.",
            )


def _adapter_version_for_visual_provider(
    provider: VisualTextProviderPort | None,
    *,
    page_numbers: frozenset[int] | None,
    forced_page_numbers: frozenset[int],
    visual_table_provider: VisualTableProviderPort | None,
    visual_table_page_numbers: frozenset[int] | None,
    visual_table_on_visual_text_pages: bool = False,
) -> str:
    if provider is None and visual_table_provider is None:
        return PDF_SOURCE_IR_ADAPTER_VERSION
    if visual_table_provider is None and not visual_table_page_numbers:
        provider_id = getattr(provider, "provider_id", None)
        provider_version = getattr(provider, "provider_version", None)
        if (
            not isinstance(provider_id, str)
            or not provider_id.strip()
            or not isinstance(provider_version, str)
            or not provider_version.strip()
        ):
            raise TypeError(
                "visual_text_provider requires non-blank provider_id and provider_version"
            )
        legacy_parts: tuple[object, ...] = (
            provider_id,
            provider_version,
            "all-pages" if page_numbers is None else tuple(sorted(page_numbers)),
        )
        if forced_page_numbers:
            legacy_parts = (
                *legacy_parts,
                ("forced-pages", tuple(sorted(forced_page_numbers))),
            )
        legacy_identity = json.dumps(
            legacy_parts,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        provider_digest = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()[
            :12
        ]
        return f"{PDF_SOURCE_IR_ADAPTER_VERSION}+visual.{provider_digest}"
    identities: list[tuple[object, ...]] = []
    for label, selected_provider, selected_pages in (
        ("page-text", provider, page_numbers),
        ("table", visual_table_provider, visual_table_page_numbers),
    ):
        if selected_provider is None:
            continue
        provider_id = getattr(selected_provider, "provider_id", None)
        provider_version = getattr(selected_provider, "provider_version", None)
        if (
            not isinstance(provider_id, str)
            or not provider_id.strip()
            or not isinstance(provider_version, str)
            or not provider_version.strip()
        ):
            raise TypeError(
                f"visual {label} provider requires non-blank provider identity"
            )
        identity: tuple[object, ...] = (
            label,
            provider_id,
            provider_version,
            (
                "visual-text-pages-with-native-tables"
                if label == "table" and visual_table_on_visual_text_pages
                else (
                    "all-pages"
                    if selected_pages is None
                    else tuple(sorted(selected_pages))
                )
            ),
        )
        if label == "page-text" and forced_page_numbers:
            identity = (
                *identity,
                ("forced-pages", tuple(sorted(forced_page_numbers))),
            )
        identities.append(identity)
    if visual_table_provider is None and visual_table_page_numbers:
        identities.append(
            (
                "table",
                "provider-unavailable",
                "provider-unavailable",
                tuple(sorted(visual_table_page_numbers)),
            )
        )
    identity = json.dumps(
        identities,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    provider_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{PDF_SOURCE_IR_ADAPTER_VERSION}+visual.{provider_digest}"


def _normalize_visual_text_page_numbers(
    value: frozenset[int] | set[int] | tuple[int, ...] | None,
) -> frozenset[int] | None:
    if value is None:
        return None
    if not isinstance(value, (frozenset, set, tuple)):
        raise TypeError("visual_text_page_numbers must be a set or tuple")
    normalized: set[int] = set()
    for page_number in value:
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("visual text page numbers must be positive integers")
        normalized.add(page_number)
    return frozenset(normalized)


def _fragmented_image_mosaic_region_from_page(
    *,
    document: Any,
    page: Any,
    page_number: int,
    blocks: list[Any] | tuple[Any, ...],
    page_bbox: tuple[float, float, float, float] | None,
    rotation_matrix: Any,
    resource_budget: _PdfResourceBudget,
) -> _ImageMosaicRegion | None:
    """Prove that direct one-row image XObjects form one raster mosaic.

    The proof is intentionally conjunctive.  Any metadata, native-resource,
    lattice, connectivity, or budget ambiguity preserves the original IMAGE
    occurrences.
    """

    if page_bbox is None:
        return None
    try:
        if int(getattr(page, "rotation", 0)) != 0:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    indexed_blocks = tuple(
        (block_index, block)
        for block_index, block in enumerate(blocks)
        if isinstance(block, dict) and block.get("type") == 1
    )
    occurrence_count = len(indexed_blocks)
    if not (
        _MIN_PDF_IMAGE_MOSAIC_OCCURRENCES
        <= occurrence_count
        <= _MAX_PDF_IMAGE_MOSAIC_OCCURRENCES_PER_PAGE
    ):
        return None

    raw_entries_by_signature: dict[
        tuple[object, ...],
        list[
            tuple[
                int,
                tuple[float, float, float, float],
                tuple[float, float, float, float, float, float],
                int,
                int,
                str,
                str,
            ]
        ],
    ] = defaultdict(list)
    unique_encoded_byte_count = 0
    visual_bboxes: list[tuple[float, float, float, float]] = []
    scale_x_values: list[float] = []
    scale_y_values: list[float] = []
    decoded_by_content_sha256: dict[str, tuple[str, int, int]] = {}
    try:
        for block_index, block in indexed_blocks:
            if (
                block.get("mask") is not None
                or block.get("ext") != "png"
                or block.get("colorspace") != 3
                or block.get("bpc") != 8
            ):
                return None
            width = int(block.get("width"))
            height = int(block.get("height"))
            if not 1 <= width <= _PDF_IMAGE_MOSAIC_MAX_INTRINSIC_WIDTH or height != 1:
                return None
            raw_bbox = _native_bbox(block.get("bbox"))
            visual_bbox = _pdf_visual_bbox(
                raw_bbox,
                rotation_matrix=rotation_matrix,
            )
            transform = _image_mosaic_transform(block.get("transform"))
            image_bytes = _image_bytes(block.get("image"))
            if (
                raw_bbox is None
                or visual_bbox is None
                or transform is None
                or image_bytes is None
                or abs(transform[1]) > 1e-6
                or abs(transform[2]) > 1e-6
            ):
                return None
            placement_width = visual_bbox[2] - visual_bbox[0]
            placement_height = visual_bbox[3] - visual_bbox[1]
            if (
                placement_width <= 0.0
                or placement_height <= 0.0
                or placement_width > _PDF_IMAGE_MOSAIC_MAX_PLACEMENT_WIDTH_POINTS
                or placement_height > _PDF_IMAGE_MOSAIC_MAX_PLACEMENT_HEIGHT_POINTS
            ):
                return None
            scale_x = placement_width / width
            scale_y = placement_height / height
            if (
                not math.isfinite(scale_x)
                or not math.isfinite(scale_y)
                or scale_x <= 0.0
                or scale_y <= 0.0
            ):
                return None
            content_sha256 = hashlib.sha256(image_bytes).hexdigest()
            decoded = decoded_by_content_sha256.get(content_sha256)
            if decoded is None:
                next_encoded_byte_count = (
                    resource_budget.image_mosaic_proof_encoded_bytes
                    + unique_encoded_byte_count
                    + len(image_bytes)
                )
                if next_encoded_byte_count > _MAX_PDF_IMAGE_MOSAIC_PROOF_ENCODED_BYTES:
                    return None
                pixmap = pymupdf.Pixmap(image_bytes)
                if (
                    int(pixmap.width) != width
                    or int(pixmap.height) != height
                    or int(pixmap.n) != 3
                    or bool(pixmap.alpha)
                ):
                    return None
                samples = bytes(pixmap.samples)
                if len(samples) != width * height * 3:
                    return None
                decoded = (
                    hashlib.md5(samples).hexdigest(),
                    width,
                    height,
                )
                decoded_by_content_sha256[content_sha256] = decoded
                unique_encoded_byte_count += len(image_bytes)
            elif decoded[1:] != (width, height):
                return None
            decoded_digest = decoded[0]
            signature = _image_mosaic_occurrence_signature(
                bbox=raw_bbox,
                transform=transform,
                width=width,
                height=height,
                decoded_digest=decoded_digest,
            )
            raw_entries_by_signature[signature].append(
                (
                    block_index,
                    visual_bbox,
                    transform,
                    width,
                    height,
                    content_sha256,
                    decoded_digest,
                )
            )
            visual_bboxes.append(visual_bbox)
            scale_x_values.append(scale_x)
            scale_y_values.append(scale_y)
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return None

    scale_x = _image_mosaic_median(scale_x_values)
    scale_y = _image_mosaic_median(scale_y_values)
    if (
        scale_x is None
        or scale_y is None
        or (max(scale_x_values) - min(scale_x_values)) / scale_x
        > _PDF_IMAGE_MOSAIC_MAX_RELATIVE_SCALE_SPREAD
        or (max(scale_y_values) - min(scale_y_values)) / scale_y
        > _PDF_IMAGE_MOSAIC_MAX_RELATIVE_SCALE_SPREAD
    ):
        return None
    origin_x = min(bbox[0] for bbox in visual_bboxes)
    origin_y = min(bbox[1] for bbox in visual_bboxes)
    for (_, block), bbox in zip(indexed_blocks, visual_bboxes, strict=True):
        width = int(block["width"])
        height = int(block["height"])
        if (
            _image_mosaic_lattice_residual(bbox[0], origin_x, scale_x)
            > _PDF_IMAGE_MOSAIC_MAX_LATTICE_RESIDUAL_PIXELS
            or _image_mosaic_lattice_residual(bbox[2], origin_x, scale_x)
            > _PDF_IMAGE_MOSAIC_MAX_LATTICE_RESIDUAL_PIXELS
            or _image_mosaic_lattice_residual(bbox[1], origin_y, scale_y)
            > _PDF_IMAGE_MOSAIC_MAX_LATTICE_RESIDUAL_PIXELS
            or _image_mosaic_lattice_residual(bbox[3], origin_y, scale_y)
            > _PDF_IMAGE_MOSAIC_MAX_LATTICE_RESIDUAL_PIXELS
            or abs((bbox[2] - bbox[0]) / scale_x - width)
            > _PDF_IMAGE_MOSAIC_MAX_LATTICE_RESIDUAL_PIXELS
            or abs((bbox[3] - bbox[1]) / scale_y - height)
            > _PDF_IMAGE_MOSAIC_MAX_LATTICE_RESIDUAL_PIXELS
        ):
            return None

    component_bbox = _bbox_union(tuple(visual_bboxes))
    if component_bbox is None or not _image_mosaic_is_single_component(
        tuple(visual_bboxes)
    ):
        return None
    padded_bbox = _padded_clipped_bbox(
        component_bbox,
        page_bbox=page_bbox,
        padding=_VECTOR_FIGURE_PADDING_POINTS,
    )
    if padded_bbox is None:
        return None
    width = padded_bbox[2] - padded_bbox[0]
    height = padded_bbox[3] - padded_bbox[1]
    page_area = _bbox_area(page_bbox)
    if (
        page_area <= 0.0
        or width < _VECTOR_FIGURE_ANCHOR_MIN_WIDTH
        or height < _VECTOR_FIGURE_ANCHOR_WIDE_HEIGHT
        or width * height < _VECTOR_FIGURE_MIN_REGION_AREA
        or width * height / page_area >= _VECTOR_FIGURE_MAX_PAGE_AREA_RATIO
    ):
        return None
    page_height = page_bbox[3] - page_bbox[1]
    center_y = (padded_bbox[1] + padded_bbox[3]) / 2.0
    if (
        center_y <= page_bbox[1] + page_height * _VECTOR_FIGURE_HEADER_BAND_RATIO
        or center_y >= page_bbox[1] + page_height * _VECTOR_FIGURE_FOOTER_BAND_RATIO
    ):
        return None

    try:
        raw_image_infos = page.get_image_info(hashes=True, xrefs=True)
        image_infos = (
            tuple(raw_image_infos) if isinstance(raw_image_infos, (list, tuple)) else ()
        )
        raw_page_images = page.get_images(full=True)
        page_images = (
            tuple(raw_page_images) if isinstance(raw_page_images, (list, tuple)) else ()
        )
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return None
    if len(image_infos) != occurrence_count:
        return None

    info_xrefs_by_signature: dict[tuple[object, ...], list[int]] = defaultdict(list)
    try:
        for info in image_infos:
            if not isinstance(info, dict):
                return None
            info_bbox = _native_bbox(info.get("bbox"))
            info_transform = _image_mosaic_transform(info.get("transform"))
            info_digest = info.get("digest")
            xref = int(info.get("xref"))
            width = int(info.get("width"))
            height = int(info.get("height"))
            if (
                info_bbox is None
                or info_transform is None
                or not isinstance(info_digest, (bytes, bytearray))
                or xref <= 0
            ):
                return None
            signature = _image_mosaic_occurrence_signature(
                bbox=info_bbox,
                transform=info_transform,
                width=width,
                height=height,
                decoded_digest=bytes(info_digest).hex(),
            )
            info_xrefs_by_signature[signature].append(xref)
    except (TypeError, ValueError, OverflowError):
        return None
    if Counter(
        {
            signature: len(entries)
            for signature, entries in raw_entries_by_signature.items()
        }
    ) != Counter(
        {signature: len(xrefs) for signature, xrefs in info_xrefs_by_signature.items()}
    ):
        return None

    info_xref_set = {
        xref for xrefs in info_xrefs_by_signature.values() for xref in xrefs
    }
    try:
        resource_xref_set = {
            int(image[0])
            for image in page_images
            if isinstance(image, (list, tuple)) and len(image) >= 2
        }
        resource_masks_are_clear = all(
            isinstance(image, (list, tuple)) and len(image) >= 2 and int(image[1]) == 0
            for image in page_images
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        len(info_xref_set) > _MAX_PDF_IMAGE_MOSAIC_UNIQUE_XREFS_PER_PAGE
        or resource_xref_set != info_xref_set
        or not resource_masks_are_clear
        or not resource_budget.reserve_image_mosaic_proof(
            occurrence_count=occurrence_count,
            unique_xref_count=len(info_xref_set),
            encoded_byte_count=unique_encoded_byte_count,
        )
    ):
        return None
    for xref in info_xref_set:
        try:
            if (
                document.xref_get_key(xref, "Subtype") != ("name", "/Image")
                or document.xref_get_key(xref, "Filter") != ("name", "/FlateDecode")
                or not _xref_key_is_null(document=document, xref=xref, key="Mask")
                or not _xref_key_is_null(document=document, xref=xref, key="SMask")
            ):
                return None
        except MemoryError as exc:
            raise _PdfResourceBudgetExceeded from exc
        except Exception:
            return None

    occurrences: list[_ImageMosaicOccurrence] = []
    xref_to_facts: dict[int, set[tuple[str, str, int, int]]] = defaultdict(set)
    content_sha256_to_xrefs: dict[str, set[int]] = defaultdict(set)
    for signature in sorted(raw_entries_by_signature, key=repr):
        raw_entries = sorted(
            raw_entries_by_signature[signature],
            key=lambda entry: entry[0],
        )
        xrefs = sorted(info_xrefs_by_signature[signature])
        for entry, xref in zip(raw_entries, xrefs, strict=True):
            (
                block_index,
                bbox,
                transform,
                width,
                height,
                content_sha256,
                decoded_digest,
            ) = entry
            xref_to_facts[xref].add((content_sha256, decoded_digest, width, height))
            content_sha256_to_xrefs[content_sha256].add(xref)
            occurrences.append(
                _ImageMosaicOccurrence(
                    block_index=block_index,
                    bbox=bbox,
                    transform=transform,
                    width=width,
                    height=height,
                    xref=xref,
                    content_sha256=content_sha256,
                    decoded_digest=decoded_digest,
                )
            )
    if any(len(facts) != 1 for facts in xref_to_facts.values()) or any(
        len(xrefs) != 1 for xrefs in content_sha256_to_xrefs.values()
    ):
        return None
    ordered_occurrences = tuple(
        sorted(occurrences, key=lambda occurrence: occurrence.block_index)
    )
    evidence_payload = {
        "schema": "pdf-fragmented-image-mosaic-proof/v1",
        "page_number": page_number,
        "bbox": [_image_mosaic_float(value) for value in padded_bbox],
        "scale_x": _image_mosaic_float(scale_x),
        "scale_y": _image_mosaic_float(scale_y),
        "component_gap_points": _PDF_IMAGE_MOSAIC_COMPONENT_GAP_POINTS,
        "occurrences": [
            {
                "block_index": occurrence.block_index,
                "bbox": [_image_mosaic_float(value) for value in occurrence.bbox],
                "transform": [
                    _image_mosaic_float(value) for value in occurrence.transform
                ],
                "width": occurrence.width,
                "height": occurrence.height,
                "xref": occurrence.xref,
                "content_sha256": occurrence.content_sha256,
                "decoded_digest": occurrence.decoded_digest,
            }
            for occurrence in ordered_occurrences
        ],
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _ImageMosaicRegion(
        bbox=padded_bbox,
        occurrences=ordered_occurrences,
        unique_xref_count=len(info_xref_set),
        scale_x=scale_x,
        scale_y=scale_y,
        evidence_sha256=evidence_sha256,
    )


def _image_mosaic_transform(
    value: object,
) -> tuple[float, float, float, float, float, float] | None:
    try:
        values = tuple(float(item) for item in value)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return None
    if len(values) != 6 or not all(math.isfinite(item) for item in values):
        return None
    return values  # type: ignore[return-value]


def _image_mosaic_occurrence_signature(
    *,
    bbox: tuple[float, float, float, float],
    transform: tuple[float, float, float, float, float, float],
    width: int,
    height: int,
    decoded_digest: str,
) -> tuple[object, ...]:
    return (
        tuple(_image_mosaic_float(value) for value in bbox),
        tuple(_image_mosaic_float(value) for value in transform),
        width,
        height,
        decoded_digest,
    )


def _image_mosaic_float(value: float) -> float:
    return round(float(value), 6)


def _image_mosaic_median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _image_mosaic_lattice_residual(
    value: float,
    origin: float,
    scale: float,
) -> float:
    scaled = (value - origin) / scale
    return abs(scaled - round(scaled))


def _image_mosaic_is_single_component(
    bboxes: tuple[tuple[float, float, float, float], ...],
) -> bool:
    if not bboxes:
        return False
    parent = list(range(len(bboxes)))
    sizes = [1] * len(bboxes)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if sizes[left_root] < sizes[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        sizes[left_root] += sizes[right_root]

    cell_size = _PDF_IMAGE_MOSAIC_GRID_CELL_POINTS
    gap = _PDF_IMAGE_MOSAIC_COMPONENT_GAP_POINTS
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    adjacency_checks = 0
    for index in sorted(
        range(len(bboxes)),
        key=lambda candidate: (
            bboxes[candidate][1],
            bboxes[candidate][0],
            candidate,
        ),
    ):
        bbox = bboxes[index]
        candidate_indexes: set[int] = set()
        for cell_x in range(
            math.floor((bbox[0] - gap) / cell_size),
            math.floor((bbox[2] + gap) / cell_size) + 1,
        ):
            for cell_y in range(
                math.floor((bbox[1] - gap) / cell_size),
                math.floor((bbox[3] + gap) / cell_size) + 1,
            ):
                candidate_indexes.update(grid.get((cell_x, cell_y), ()))
        for candidate_index in candidate_indexes:
            adjacency_checks += 1
            if adjacency_checks > _MAX_PDF_IMAGE_MOSAIC_ADJACENCY_CHECKS:
                return False
            if _bbox_distance(bbox, bboxes[candidate_index]) <= gap:
                union(index, candidate_index)
        for cell_x in range(
            math.floor(bbox[0] / cell_size),
            math.floor(bbox[2] / cell_size) + 1,
        ):
            for cell_y in range(
                math.floor(bbox[1] / cell_size),
                math.floor(bbox[3] / cell_size) + 1,
            ):
                grid[(cell_x, cell_y)].append(index)
    return len({find(index) for index in range(len(bboxes))}) == 1


def _caption_anchored_quadrilateral_figure_regions_from_page(
    *,
    page: Any,
    raw_page: Any,
    page_number: int,
    page_bbox: tuple[float, float, float, float] | None,
    rotation_matrix: Any,
    curve_seeded_regions: tuple[_VectorFigureRegion, ...],
    resource_budget: _PdfResourceBudget,
) -> tuple[_VectorFigureRegion, ...]:
    """Recognize one reference-and-caption-grounded quadrilateral figure.

    Imported engineering diagrams can expose every box as a native ``qu``
    stroke without any Bézier seed.  This route remains fail closed: every
    participating quadrilateral must have one unique bbox-log stroke record,
    the component needs a material enclosure, and the same Figure number must
    occur immediately above and below it.  A page with more than one qualifying
    component is ambiguous and keeps its original facts.
    """

    if page_bbox is None:
        return ()
    page_area = _bbox_area(page_bbox)
    if page_area <= 0.0:
        return ()
    figure_labels = tuple(
        record
        for record in _pdf_labeled_text_blocks(
            raw_page=raw_page,
            rotation_matrix=rotation_matrix,
        )
        if record[0] == "figure"
    )
    if len(figure_labels) < 2:
        return ()
    try:
        get_drawings = getattr(page, "get_drawings", None)
        get_bboxlog = getattr(page, "get_bboxlog", None)
    except Exception:
        return ()
    if not callable(get_drawings) or not callable(get_bboxlog):
        return ()
    try:
        drawings = get_drawings()
        page_bboxlog = get_bboxlog()
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return ()
    if (
        not isinstance(drawings, (list, tuple))
        or len(drawings) > _MAX_PDF_DRAWINGS_PER_PAGE
        or not isinstance(page_bboxlog, (list, tuple))
        or len(page_bboxlog) > _MAX_PDF_BBOXLOG_ENTRIES_PER_PAGE
    ):
        if (
            isinstance(page_bboxlog, (list, tuple))
            and len(page_bboxlog) > _MAX_PDF_BBOXLOG_ENTRIES_PER_PAGE
        ):
            raise _PdfResourceBudgetExceeded
        return ()

    stroke_path_bboxes: list[tuple[int, tuple[float, float, float, float]]] = []
    for bboxlog_index, entry in enumerate(page_bboxlog):
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) < 2
            or entry[0] != "stroke-path"
        ):
            continue
        bbox = _pdf_visual_bbox(
            _native_bbox(entry[1]),
            rotation_matrix=rotation_matrix,
        )
        bbox = _positive_bbox_intersection(bbox, page_bbox)
        if bbox is not None:
            stroke_path_bboxes.append((bboxlog_index, bbox))

    eligible_quadrilaterals: list[
        tuple[tuple[float, float, float, float], tuple[int, ...]]
    ] = []
    drawing_item_count = 0
    bboxlog_match_checks = 0
    for drawing in drawings:
        if not isinstance(drawing, dict):
            continue
        raw_items = drawing.get("items")
        if not isinstance(raw_items, (list, tuple)):
            continue
        drawing_item_count += len(raw_items)
        if drawing_item_count > _MAX_PDF_DRAWING_ITEMS_PER_PAGE:
            raise _PdfResourceBudgetExceeded
        if (
            len(raw_items) != 1
            or not isinstance(raw_items[0], (list, tuple))
            or not raw_items[0]
            or raw_items[0][0] != "qu"
        ):
            continue
        drawing_type = drawing.get("type")
        if not isinstance(drawing_type, str) or "s" not in drawing_type.lower():
            continue
        if drawing.get("color") is None:
            continue
        try:
            stroke_width = float(drawing.get("width"))
            stroke_opacity = float(drawing.get("stroke_opacity", 1.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not math.isfinite(stroke_width)
            or stroke_width <= 0.0
            or not math.isfinite(stroke_opacity)
            or stroke_opacity <= 0.0
        ):
            continue
        bbox = _pdf_visual_bbox(
            _native_bbox(drawing.get("rect") or drawing.get("bbox")),
            rotation_matrix=rotation_matrix,
        )
        bbox = _positive_bbox_intersection(bbox, page_bbox)
        if bbox is None:
            continue
        if (
            bbox[2] - bbox[0] < _QUADRILATERAL_FIGURE_MIN_PATH_DIMENSION
            or bbox[3] - bbox[1] < _QUADRILATERAL_FIGURE_MIN_PATH_DIMENSION
        ):
            continue
        matching_bboxlog_indexes_list: list[int] = []
        for bboxlog_index, stroke_bbox in stroke_path_bboxes:
            bboxlog_match_checks += 1
            if bboxlog_match_checks > _MAX_QUADRILATERAL_BBOXLOG_MATCH_CHECKS:
                raise _PdfResourceBudgetExceeded
            if (
                max(
                    abs(bbox_edge - stroke_edge)
                    for bbox_edge, stroke_edge in zip(
                        bbox,
                        stroke_bbox,
                        strict=True,
                    )
                )
                <= _QUADRILATERAL_FIGURE_BBOXLOG_EDGE_TOLERANCE
            ):
                matching_bboxlog_indexes_list.append(bboxlog_index)
                if len(matching_bboxlog_indexes_list) > 1:
                    break
        matching_bboxlog_indexes = tuple(matching_bboxlog_indexes_list)
        eligible_quadrilaterals.append((bbox, matching_bboxlog_indexes))

    unique_bboxlog_claim_counts = Counter(
        matches[0] for _, matches in eligible_quadrilaterals if len(matches) == 1
    )
    records = tuple(
        _StrokedQuadrilateralRecord(
            bbox=bbox,
            bboxlog_index=matches[0],
        )
        for bbox, matches in eligible_quadrilaterals
        if len(matches) == 1 and unique_bboxlog_claim_counts[matches[0]] == 1
    )
    invalid_quadrilateral_bboxes = tuple(
        bbox
        for bbox, matches in eligible_quadrilaterals
        if len(matches) != 1
        or (len(matches) == 1 and unique_bboxlog_claim_counts[matches[0]] != 1)
    )
    if len(records) < _QUADRILATERAL_FIGURE_MIN_PATHS:
        return ()

    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    adjacency_checks = 0
    ordered_indexes = tuple(
        sorted(
            range(len(records)),
            key=lambda index: (
                records[index].bbox[0],
                records[index].bbox[2],
                records[index].bbox[1],
                records[index].bbox[3],
                index,
            ),
        )
    )
    for position, record_index in enumerate(ordered_indexes):
        record_bbox = records[record_index].bbox
        horizontal_reach = record_bbox[2] + _QUADRILATERAL_FIGURE_COMPONENT_GAP_POINTS
        for candidate_position in range(position + 1, len(ordered_indexes)):
            candidate_index = ordered_indexes[candidate_position]
            candidate_bbox = records[candidate_index].bbox
            if candidate_bbox[0] > horizontal_reach:
                break
            adjacency_checks += 1
            if adjacency_checks > _MAX_VECTOR_FIGURE_ADJACENCY_CHECKS:
                raise _PdfResourceBudgetExceeded
            if (
                _bbox_distance(record_bbox, candidate_bbox)
                <= _QUADRILATERAL_FIGURE_COMPONENT_GAP_POINTS
            ):
                union(record_index, candidate_index)

    component_indexes: dict[int, list[int]] = {}
    for index in range(len(records)):
        component_indexes.setdefault(find(index), []).append(index)

    grounded_components: list[
        tuple[
            tuple[_StrokedQuadrilateralRecord, ...],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[tuple[float, float, float, float], ...],
            tuple[str, str, int, tuple[float, float, float, float]],
            tuple[str, str, int, tuple[float, float, float, float]],
        ]
    ] = []
    for indexes in component_indexes.values():
        if len(indexes) < _QUADRILATERAL_FIGURE_MIN_PATHS:
            continue
        component = tuple(records[index] for index in indexes)
        component_bbox = _bbox_union(tuple(record.bbox for record in component))
        if component_bbox is None:
            continue
        component_area = _bbox_area(component_bbox)
        if (
            component_bbox[2] - component_bbox[0]
            < _QUADRILATERAL_FIGURE_MIN_REGION_WIDTH
            or component_bbox[3] - component_bbox[1]
            < _QUADRILATERAL_FIGURE_MIN_REGION_HEIGHT
            or component_area < _QUADRILATERAL_FIGURE_MIN_REGION_AREA
            or component_area / page_area >= _QUADRILATERAL_FIGURE_MAX_PAGE_AREA_RATIO
        ):
            continue
        enclosure_bboxes = tuple(
            sorted(
                (
                    record.bbox
                    for record in component
                    if _bbox_area(record.bbox)
                    >= _QUADRILATERAL_FIGURE_MIN_ENCLOSURE_AREA
                ),
                key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
            )
        )
        if not enclosure_bboxes:
            continue
        if any(
            _bbox_distance(component_bbox, invalid_bbox)
            <= _QUADRILATERAL_FIGURE_COMPONENT_GAP_POINTS
            for invalid_bbox in invalid_quadrilateral_bboxes
        ):
            continue
        if any(
            _positive_bbox_intersection(component_bbox, region.bbox) is not None
            for region in curve_seeded_regions
        ):
            continue

        preceding_labels = tuple(
            record
            for record in figure_labels
            if 0.0
            <= component_bbox[1] - record[3][3]
            <= _QUADRILATERAL_FIGURE_LABEL_GAP_POINTS
            and _quadrilateral_label_is_horizontally_grounded(
                label_bbox=record[3],
                component_bbox=component_bbox,
            )
            and _quadrilateral_figure_reference_matches(
                raw_page=raw_page,
                record=record,
            )
        )
        following_labels = tuple(
            record
            for record in figure_labels
            if 0.0
            <= record[3][1] - component_bbox[3]
            <= _QUADRILATERAL_FIGURE_LABEL_GAP_POINTS
            and _quadrilateral_label_is_horizontally_grounded(
                label_bbox=record[3],
                component_bbox=component_bbox,
            )
            and _quadrilateral_figure_caption_matches(
                raw_page=raw_page,
                record=record,
            )
        )
        shared_labels = {record[1] for record in preceding_labels} & {
            record[1] for record in following_labels
        }
        label_groundings: list[
            tuple[
                tuple[str, str, int, tuple[float, float, float, float]],
                tuple[str, str, int, tuple[float, float, float, float]],
            ]
        ] = []
        region_width = component_bbox[2] - component_bbox[0]
        for label in sorted(shared_labels):
            reference_records = {
                (record[2], record[3]): record
                for record in preceding_labels
                if record[1] == label
            }
            caption_records = {
                (record[2], record[3]): record
                for record in following_labels
                if record[1] == label
            }
            if len(reference_records) != 1 or len(caption_records) != 1:
                continue
            reference_record = next(iter(reference_records.values()))
            caption_record = next(iter(caption_records.values()))
            if reference_record[2] == caption_record[2]:
                continue
            caption_center_x = (caption_record[3][0] + caption_record[3][2]) / 2.0
            if not (
                component_bbox[0]
                - region_width * _QUADRILATERAL_FIGURE_CAPTION_CENTER_MARGIN_RATIO
                <= caption_center_x
                <= component_bbox[2]
                + region_width * _QUADRILATERAL_FIGURE_CAPTION_CENTER_MARGIN_RATIO
            ):
                continue
            label_groundings.append((reference_record, caption_record))
        if len(label_groundings) != 1:
            continue
        reference_record, caption_record = label_groundings[0]
        padded_bbox = _padded_clipped_bbox(
            component_bbox,
            page_bbox=page_bbox,
            padding=_VECTOR_FIGURE_PADDING_POINTS,
        )
        if padded_bbox is None:
            continue
        grounded_components.append(
            (
                component,
                component_bbox,
                padded_bbox,
                enclosure_bboxes,
                reference_record,
                caption_record,
            )
        )

    if len(grounded_components) != 1:
        return ()
    (
        component,
        component_bbox,
        padded_bbox,
        enclosure_bboxes,
        reference_record,
        caption_record,
    ) = grounded_components[0]
    owned_table_bboxes = _quadrilateral_figure_owned_table_bboxes(
        page=page,
        raw_page=raw_page,
        component_bbox=component_bbox,
        rotation_matrix=rotation_matrix,
        resource_budget=resource_budget,
    )
    owned_image_block_indexes = _quadrilateral_figure_owned_image_block_indexes(
        raw_page=raw_page,
        figure_bbox=padded_bbox,
        rotation_matrix=rotation_matrix,
    )
    reference_text = _quadrilateral_label_block_text(
        raw_page=raw_page,
        block_index=reference_record[2],
    )
    caption_text = _quadrilateral_label_block_text(
        raw_page=raw_page,
        block_index=caption_record[2],
    )
    if reference_text is None or caption_text is None:
        return ()
    label_sha256 = hashlib.sha256(reference_record[1].encode("utf-8")).hexdigest()
    evidence_payload = {
        "schema": "pdf-caption-anchored-quadrilateral-figure-proof/v1",
        "page_number": page_number,
        "bbox": _rounded_bbox(padded_bbox),
        "component_bbox": _rounded_bbox(component_bbox),
        "quadrilateral_bboxes": [
            _rounded_bbox(record.bbox)
            for record in sorted(
                component,
                key=lambda record: (
                    record.bbox[1],
                    record.bbox[0],
                    record.bbox[3],
                    record.bbox[2],
                ),
            )
        ],
        "bboxlog_indexes": sorted(record.bboxlog_index for record in component),
        "enclosure_bboxes": [_rounded_bbox(bbox) for bbox in enclosure_bboxes],
        "figure_label_sha256": label_sha256,
        "reference_block_index": reference_record[2],
        "reference_bbox": _rounded_bbox(reference_record[3]),
        "reference_role": "body_reference",
        "reference_text_sha256": hashlib.sha256(
            reference_text.encode("utf-8")
        ).hexdigest(),
        "caption_block_index": caption_record[2],
        "caption_bbox": _rounded_bbox(caption_record[3]),
        "caption_role": "figure_caption",
        "caption_text_sha256": hashlib.sha256(caption_text.encode("utf-8")).hexdigest(),
        "owned_table_bboxes": [_rounded_bbox(bbox) for bbox in owned_table_bboxes],
        "owned_image_block_indexes": list(owned_image_block_indexes),
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return (
        _VectorFigureRegion(
            bbox=padded_bbox,
            drawing_count=len(component),
            anchor_count=len(enclosure_bboxes),
            anchor_bboxes=enclosure_bboxes,
            proof_kind="caption_anchored_quadrilateral",
            owned_table_bboxes=owned_table_bboxes,
            owned_image_block_indexes=owned_image_block_indexes,
            evidence_sha256=evidence_sha256,
        ),
    )


def _quadrilateral_label_block_text(
    *,
    raw_page: Any,
    block_index: int,
) -> str | None:
    blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
    if (
        not isinstance(blocks, (list, tuple))
        or block_index < 0
        or block_index >= len(blocks)
    ):
        return None
    block = blocks[block_index]
    if not isinstance(block, dict) or block.get("type") != 0:
        return None
    text = _raw_text_from_block(block)
    return text if text else None


def _quadrilateral_label_is_horizontally_grounded(
    *,
    label_bbox: tuple[float, float, float, float],
    component_bbox: tuple[float, float, float, float],
) -> bool:
    region_width = component_bbox[2] - component_bbox[0]
    if region_width <= 0.0:
        return False
    center_x = (label_bbox[0] + label_bbox[2]) / 2.0
    margin = region_width * _QUADRILATERAL_FIGURE_CAPTION_CENTER_MARGIN_RATIO
    return component_bbox[0] - margin <= center_x <= component_bbox[2] + margin


def _quadrilateral_figure_reference_matches(
    *,
    raw_page: Any,
    record: tuple[str, str, int, tuple[float, float, float, float]],
) -> bool:
    text = _quadrilateral_label_block_text(
        raw_page=raw_page,
        block_index=record[2],
    )
    if text is None:
        return False
    expected_label = record[1]
    for match in _QUADRILATERAL_FIGURE_REFERENCE_PATTERN.finditer(text):
        raw_label = match.group("prefixed") or match.group("direct")
        if raw_label.replace("-", ".") == expected_label:
            return True
    return False


def _quadrilateral_figure_caption_matches(
    *,
    raw_page: Any,
    record: tuple[str, str, int, tuple[float, float, float, float]],
) -> bool:
    text = _quadrilateral_label_block_text(
        raw_page=raw_page,
        block_index=record[2],
    )
    if text is None:
        return False
    match = _QUADRILATERAL_FIGURE_CAPTION_PATTERN.match(text)
    return bool(
        match is not None and match.group("label").replace("-", ".") == record[1]
    )


def _quadrilateral_figure_owned_image_block_indexes(
    *,
    raw_page: Any,
    figure_bbox: tuple[float, float, float, float],
    rotation_matrix: Any,
) -> tuple[int, ...]:
    """Bind only raw image blocks materially contained by this exact crop."""

    blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
    if not isinstance(blocks, (list, tuple)):
        return ()
    owned: list[int] = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 1:
            continue
        bbox = _pdf_visual_bbox(
            _native_bbox(block.get("bbox")),
            rotation_matrix=rotation_matrix,
        )
        if bbox is None or _bbox_area(bbox) <= 0.0:
            continue
        intersection = _positive_bbox_intersection(bbox, figure_bbox)
        if (
            intersection is not None
            and _bbox_area(intersection) / _bbox_area(bbox) >= 0.98
        ):
            owned.append(block_index)
    return tuple(owned)


def _quadrilateral_figure_owned_table_bboxes(
    *,
    page: Any,
    raw_page: Any = None,
    component_bbox: tuple[float, float, float, float],
    rotation_matrix: Any,
    resource_budget: _PdfResourceBudget,
) -> tuple[tuple[float, float, float, float], ...]:
    """Claim only a complete set of tiny, fully contained detector grids."""

    try:
        find_tables = getattr(page, "find_tables", None)
    except Exception:
        return ()
    if not callable(find_tables):
        return ()
    try:
        finder = find_tables()
        candidates = getattr(finder, "tables", None)
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return ()
    if not isinstance(candidates, (list, tuple)):
        return ()
    resource_budget.consume_table_candidates(len(candidates))
    component_area = _bbox_area(component_bbox)
    if component_area <= 0.0:
        return ()

    contained: list[
        tuple[
            Any,
            tuple[float, float, float, float],
            tuple[float, float, float, float],
        ]
    ] = []
    for candidate in candidates:
        native_bbox = _candidate_bbox(candidate)
        visible_bbox = _pdf_visual_bbox(
            native_bbox,
            rotation_matrix=rotation_matrix,
        )
        candidate_area = _bbox_area(visible_bbox) if visible_bbox is not None else 0.0
        intersection = _positive_bbox_intersection(visible_bbox, component_bbox)
        if (
            native_bbox is None
            or visible_bbox is None
            or candidate_area <= 0.0
            or intersection is None
            or _bbox_area(intersection) / candidate_area < 0.98
        ):
            continue
        contained.append((candidate, native_bbox, visible_bbox))
    table_labels = tuple(
        record
        for record in _pdf_labeled_text_blocks(
            raw_page=raw_page,
            rotation_matrix=rotation_matrix,
        )
        if record[0] == "table"
    )
    if any(
        _quadrilateral_table_has_independent_caption(
            raw_page=raw_page,
            table_bbox=visible_bbox,
            labels=table_labels,
        )
        for _candidate, _native_bbox, visible_bbox in contained
    ):
        return ()
    if len(contained) == 1:
        candidate, native_bbox, visible_bbox = contained[0]
        dimensions = _candidate_dimensions(candidate)
        physical_cell_count = _candidate_physical_cell_count(candidate)
        intersection = _positive_bbox_intersection(visible_bbox, component_bbox)
        if (
            dimensions == (1, 2)
            and physical_cell_count == 2
            and intersection is not None
            and _bbox_area(intersection) / _bbox_area(visible_bbox) >= 0.98
            and _bbox_area(intersection) / component_area >= 0.98
        ):
            return (native_bbox,)
        return ()
    if len(contained) < _QUADRILATERAL_FIGURE_MIN_OWNED_TABLES:
        return ()

    total_area = 0.0
    for candidate, _, visible_bbox in contained:
        dimensions = _candidate_dimensions(candidate)
        physical_cell_count = _candidate_physical_cell_count(candidate)
        candidate_area = _bbox_area(visible_bbox)
        if (
            dimensions is None
            or dimensions[0] > _QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_ROWS
            or dimensions[1] > _QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_COLUMNS
            or physical_cell_count is None
            or physical_cell_count
            > _QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_PHYSICAL_CELLS
            or candidate_area / component_area
            > _QUADRILATERAL_FIGURE_MAX_OWNED_TABLE_AREA_RATIO
        ):
            return ()
        total_area += candidate_area
    if (
        total_area / component_area
        > _QUADRILATERAL_FIGURE_MAX_TOTAL_OWNED_TABLE_AREA_RATIO
    ):
        return ()
    return tuple(
        native_bbox
        for _, native_bbox, _ in sorted(
            contained,
            key=lambda record: (
                record[2][1],
                record[2][0],
                record[2][3],
                record[2][2],
            ),
        )
    )


def _quadrilateral_table_has_independent_caption(
    *,
    raw_page: Any,
    table_bbox: tuple[float, float, float, float],
    labels: tuple[
        tuple[str, str, int, tuple[float, float, float, float]],
        ...,
    ],
) -> bool:
    table_width = table_bbox[2] - table_bbox[0]
    if table_width <= 0.0:
        return False
    for record in labels:
        label_bbox = record[3]
        if label_bbox[3] <= table_bbox[1]:
            vertical_gap = table_bbox[1] - label_bbox[3]
        elif label_bbox[1] >= table_bbox[3]:
            vertical_gap = label_bbox[1] - table_bbox[3]
        elif (
            label_bbox[1] < table_bbox[1] < label_bbox[3]
            and label_bbox[3] - table_bbox[1]
            <= _QUADRILATERAL_TABLE_CAPTION_MAX_OVERLAP_POINTS
        ):
            vertical_gap = 0.0
        elif (
            label_bbox[1] < table_bbox[3] < label_bbox[3]
            and table_bbox[3] - label_bbox[1]
            <= _QUADRILATERAL_TABLE_CAPTION_MAX_OVERLAP_POINTS
        ):
            vertical_gap = 0.0
        else:
            continue
        if vertical_gap > _QUADRILATERAL_FIGURE_LABEL_GAP_POINTS:
            continue
        label_center_x = (label_bbox[0] + label_bbox[2]) / 2.0
        margin = table_width * _QUADRILATERAL_FIGURE_CAPTION_CENTER_MARGIN_RATIO
        if not (table_bbox[0] - margin <= label_center_x <= table_bbox[2] + margin):
            continue
        text = _quadrilateral_label_block_text(
            raw_page=raw_page,
            block_index=record[2],
        )
        if text is None:
            continue
        match = _QUADRILATERAL_TABLE_CAPTION_PATTERN.match(text)
        if match is not None and match.group("label").replace("-", ".") == record[1]:
            return True
    return False


def _candidate_physical_cell_count(candidate: Any) -> int | None:
    dimensions = _candidate_dimensions(candidate)
    if dimensions is None:
        return None
    row_count, column_count = dimensions
    try:
        native_rows = getattr(candidate, "rows", None)
    except Exception:
        return None
    if not isinstance(native_rows, (list, tuple)) or len(native_rows) != row_count:
        return None
    physical_cell_count = 0
    for row in native_rows:
        try:
            cells = getattr(row, "cells", None)
        except Exception:
            return None
        if not isinstance(cells, (list, tuple)) or len(cells) < column_count:
            return None
        for cell in cells[:column_count]:
            if cell is None:
                continue
            if _native_bbox(cell) is None:
                return None
            physical_cell_count += 1
    return physical_cell_count


def _caption_anchored_connector_diagram_regions_from_page(
    *,
    page: Any,
    raw_page: Any,
    page_number: int,
    page_bbox: tuple[float, float, float, float] | None,
    rotation_matrix: Any,
    excluded_regions: tuple[_VectorFigureRegion, ...],
    source_sha256: str,
    resource_budget: _PdfResourceBudget,
) -> tuple[_VectorFigureRegion, ...]:
    """Recognize one caption-grounded connector-and-arrowhead diagram.

    The proof is intentionally independent from the curve and quadrilateral
    routes.  It requires at least three unique shaft/filled-triangle endpoint
    matches in one bounded drawing component and exactly one immediately
    following Figure caption.  Ambiguous matches, nearby Table captions,
    oversized components, and overlapping figure proofs fail closed.
    """

    if page_bbox is None:
        return ()
    page_area = _bbox_area(page_bbox)
    if page_area <= 0.0:
        return ()
    labeled_blocks = _pdf_labeled_text_blocks(
        raw_page=raw_page,
        rotation_matrix=rotation_matrix,
    )
    figure_captions = tuple(
        record
        for record in labeled_blocks
        if record[0] == "figure"
        and _quadrilateral_figure_caption_matches(
            raw_page=raw_page,
            record=record,
        )
    )
    if not figure_captions:
        return ()

    try:
        get_drawings = getattr(page, "get_drawings", None)
    except Exception:
        return ()
    if not callable(get_drawings):
        return ()
    try:
        drawings = get_drawings()
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return ()
    if not isinstance(drawings, (list, tuple)):
        return ()
    if len(drawings) > _MAX_PDF_DRAWINGS_PER_PAGE:
        raise _PdfResourceBudgetExceeded

    records: list[_ConnectorDiagramDrawingRecord] = []
    shafts: list[_ConnectorDiagramShaftRecord] = []
    arrowheads: list[_ConnectorDiagramArrowheadRecord] = []
    drawing_item_count = 0
    for drawing_index, drawing in enumerate(drawings):
        if not isinstance(drawing, dict):
            continue
        raw_items = drawing.get("items")
        if not isinstance(raw_items, (list, tuple)):
            continue
        drawing_item_count += len(raw_items)
        if drawing_item_count > _MAX_PDF_DRAWING_ITEMS_PER_PAGE:
            raise _PdfResourceBudgetExceeded
        if not _connector_diagram_drawing_is_visible(drawing):
            continue
        bbox = _pdf_visual_bbox(
            _native_bbox(drawing.get("rect") or drawing.get("bbox")),
            rotation_matrix=rotation_matrix,
        )
        bbox = _expanded_degenerate_drawing_bbox(
            bbox,
            stroke_width=drawing.get("width"),
        )
        bbox = _positive_bbox_intersection(bbox, page_bbox)
        if (
            bbox is None
            or _bbox_area(bbox) / page_area
            >= _CONNECTOR_DIAGRAM_MAX_DRAWING_PAGE_AREA_RATIO
        ):
            continue
        records.append(
            _ConnectorDiagramDrawingRecord(
                drawing_index=drawing_index,
                bbox=bbox,
            )
        )
        shaft = _connector_diagram_shaft_record(
            drawing=drawing,
            drawing_index=drawing_index,
            bbox=bbox,
            rotation_matrix=rotation_matrix,
        )
        if shaft is not None:
            shafts.append(shaft)
        arrowhead = _connector_diagram_arrowhead_record(
            drawing=drawing,
            drawing_index=drawing_index,
            bbox=bbox,
            rotation_matrix=rotation_matrix,
        )
        if arrowhead is not None:
            arrowheads.append(arrowhead)

    if (
        len(shafts) < _CONNECTOR_DIAGRAM_MIN_SHAFT_ARROWHEAD_PAIRS
        or len(arrowheads) < _CONNECTOR_DIAGRAM_MIN_SHAFT_ARROWHEAD_PAIRS
    ):
        return ()

    matched_pairs = _connector_diagram_unique_endpoint_pairs(
        shafts=tuple(shafts),
        arrowheads=tuple(arrowheads),
    )
    if len(matched_pairs) < _CONNECTOR_DIAGRAM_MIN_SHAFT_ARROWHEAD_PAIRS:
        return ()
    component_roots = _connector_diagram_component_roots(tuple(records))
    pairs_by_root: dict[
        int,
        list[tuple[_ConnectorDiagramShaftRecord, _ConnectorDiagramArrowheadRecord]],
    ] = {}
    for shaft, arrowhead in matched_pairs:
        shaft_root = component_roots.get(shaft.drawing_index)
        arrowhead_root = component_roots.get(arrowhead.drawing_index)
        if shaft_root is None or shaft_root != arrowhead_root:
            continue
        pairs_by_root.setdefault(shaft_root, []).append((shaft, arrowhead))

    grounded_regions: list[_VectorFigureRegion] = []
    for root, component_pairs in sorted(pairs_by_root.items()):
        if len(component_pairs) < _CONNECTOR_DIAGRAM_MIN_SHAFT_ARROWHEAD_PAIRS:
            continue
        component_records = tuple(
            record
            for record in records
            if component_roots.get(record.drawing_index) == root
        )
        if (
            not component_records
            or len(component_records) > _CONNECTOR_DIAGRAM_MAX_COMPONENT_DRAWINGS
        ):
            continue
        component_bbox = _bbox_union(tuple(record.bbox for record in component_records))
        if component_bbox is None:
            continue
        width = component_bbox[2] - component_bbox[0]
        height = component_bbox[3] - component_bbox[1]
        component_area = width * height
        page_height = page_bbox[3] - page_bbox[1]
        center_y = (component_bbox[1] + component_bbox[3]) / 2.0
        if (
            width < _CONNECTOR_DIAGRAM_MIN_REGION_WIDTH
            or height < _CONNECTOR_DIAGRAM_MIN_REGION_HEIGHT
            or component_area < _CONNECTOR_DIAGRAM_MIN_REGION_AREA
            or component_area / page_area >= _CONNECTOR_DIAGRAM_MAX_PAGE_AREA_RATIO
            or center_y
            <= page_bbox[1] + page_height * _CONNECTOR_DIAGRAM_HEADER_BAND_RATIO
            or center_y
            >= page_bbox[1] + page_height * _CONNECTOR_DIAGRAM_FOOTER_BAND_RATIO
            or any(
                _positive_bbox_intersection(component_bbox, region.bbox) is not None
                for region in excluded_regions
            )
            or _connector_diagram_has_independent_table_caption(
                raw_page=raw_page,
                labeled_blocks=labeled_blocks,
                component_bbox=component_bbox,
            )
        ):
            continue
        captions = tuple(
            record
            for record in figure_captions
            if 0.0
            <= record[3][1] - component_bbox[3]
            <= _CONNECTOR_DIAGRAM_CAPTION_GAP_POINTS
            and _quadrilateral_label_is_horizontally_grounded(
                label_bbox=record[3],
                component_bbox=component_bbox,
            )
        )
        if len(captions) != 1:
            continue
        caption = captions[0]
        adjacent_text_blocks = _connector_diagram_adjacent_text_blocks(
            raw_page=raw_page,
            rotation_matrix=rotation_matrix,
            component_bbox=component_bbox,
            excluded_block_indexes=frozenset({caption[2]}),
        )
        if len(adjacent_text_blocks) > _CONNECTOR_DIAGRAM_MAX_ADJACENT_TEXT_BLOCKS:
            continue
        content_bbox = _bbox_union(
            (
                *(record.bbox for record in component_records),
                *(bbox for _, bbox in adjacent_text_blocks),
            )
        )
        if content_bbox is None:
            continue
        padded_bbox = _padded_clipped_bbox(
            content_bbox,
            page_bbox=page_bbox,
            padding=_VECTOR_FIGURE_PADDING_POINTS,
        )
        if padded_bbox is None:
            continue
        padded_area = _bbox_area(padded_bbox)
        if (
            padded_area <= 0.0
            or padded_area / page_area >= _CONNECTOR_DIAGRAM_MAX_PAGE_AREA_RATIO
        ):
            continue
        owned_table_bboxes = _connector_diagram_owned_bitfield_table_bboxes(
            page=page,
            raw_page=raw_page,
            component_bbox=padded_bbox,
            rotation_matrix=rotation_matrix,
            resource_budget=resource_budget,
        )
        caption_text = _quadrilateral_label_block_text(
            raw_page=raw_page,
            block_index=caption[2],
        )
        if caption_text is None:
            continue
        ordered_component_pairs = tuple(
            sorted(
                component_pairs,
                key=lambda pair: (
                    pair[0].bbox[1],
                    pair[0].bbox[0],
                    pair[0].drawing_index,
                    pair[1].drawing_index,
                ),
            )
        )
        evidence_payload = {
            "schema": "pdf-caption-anchored-connector-diagram-proof/v1",
            "source_sha256": source_sha256,
            "page_number": page_number,
            "bbox": _rounded_bbox(padded_bbox),
            "component_bbox": _rounded_bbox(component_bbox),
            "drawing_indexes": sorted(
                record.drawing_index for record in component_records
            ),
            "shaft_arrowhead_pairs": [
                {
                    "shaft_drawing_index": shaft.drawing_index,
                    "shaft_bbox": _rounded_bbox(shaft.bbox),
                    "shaft_endpoints": [
                        [round(coordinate, 4) for coordinate in endpoint]
                        for endpoint in shaft.endpoints
                    ],
                    "arrowhead_drawing_index": arrowhead.drawing_index,
                    "arrowhead_bbox": _rounded_bbox(arrowhead.bbox),
                    "arrowhead_vertices": [
                        [round(coordinate, 4) for coordinate in vertex]
                        for vertex in arrowhead.vertices
                    ],
                }
                for shaft, arrowhead in ordered_component_pairs
            ],
            "adjacent_text_blocks": [
                {
                    "block_index": block_index,
                    "bbox": _rounded_bbox(bbox),
                }
                for block_index, bbox in adjacent_text_blocks
            ],
            "caption_block_index": caption[2],
            "caption_bbox": _rounded_bbox(caption[3]),
            "caption_text_sha256": hashlib.sha256(
                caption_text.encode("utf-8")
            ).hexdigest(),
            "owned_table_bboxes": [_rounded_bbox(bbox) for bbox in owned_table_bboxes],
            "thresholds": {
                "minimum_pair_count": (_CONNECTOR_DIAGRAM_MIN_SHAFT_ARROWHEAD_PAIRS),
                "maximum_component_gap_points": (
                    _CONNECTOR_DIAGRAM_COMPONENT_GAP_POINTS
                ),
                "maximum_caption_gap_points": (_CONNECTOR_DIAGRAM_CAPTION_GAP_POINTS),
            },
            "disposition": "MATERIALIZE_CONNECTOR_DIAGRAM_FIGURE",
        }
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                evidence_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        grounded_regions.append(
            _VectorFigureRegion(
                bbox=padded_bbox,
                drawing_count=len(component_records),
                anchor_count=len(ordered_component_pairs),
                anchor_bboxes=tuple(
                    arrowhead.bbox for _, arrowhead in ordered_component_pairs
                ),
                proof_kind="caption_anchored_connector_diagram",
                owned_table_bboxes=owned_table_bboxes,
                evidence_sha256=evidence_sha256,
            )
        )
    return tuple(grounded_regions) if len(grounded_regions) == 1 else ()


def _connector_diagram_drawing_is_visible(drawing: dict[str, Any]) -> bool:
    drawing_type = drawing.get("type")
    if not isinstance(drawing_type, str):
        return False
    visible = False
    if "s" in drawing_type.lower() and drawing.get("color") is not None:
        try:
            stroke_opacity = float(drawing.get("stroke_opacity", 1.0))
            stroke_width = float(drawing.get("width"))
        except (TypeError, ValueError, OverflowError):
            return False
        visible = (
            math.isfinite(stroke_opacity)
            and stroke_opacity > 0.0
            and math.isfinite(stroke_width)
            and stroke_width > 0.0
        )
    if "f" in drawing_type.lower() and drawing.get("fill") is not None:
        try:
            fill_opacity = float(drawing.get("fill_opacity", 1.0))
        except (TypeError, ValueError, OverflowError):
            return False
        visible = visible or (math.isfinite(fill_opacity) and fill_opacity > 0.0)
    return visible


def _connector_diagram_shaft_record(
    *,
    drawing: dict[str, Any],
    drawing_index: int,
    bbox: tuple[float, float, float, float],
    rotation_matrix: Any,
) -> _ConnectorDiagramShaftRecord | None:
    drawing_type = drawing.get("type")
    raw_items = drawing.get("items")
    if (
        not isinstance(drawing_type, str)
        or "s" not in drawing_type.lower()
        or not isinstance(raw_items, (list, tuple))
        or len(raw_items) != 1
    ):
        return None
    item = raw_items[0]
    if not isinstance(item, (list, tuple)) or len(item) < 3 or item[0] != "l":
        return None
    start = _pdf_visual_point(item[1], rotation_matrix=rotation_matrix)
    end = _pdf_visual_point(item[2], rotation_matrix=rotation_matrix)
    if start is None or end is None:
        return None
    if math.hypot(end[0] - start[0], end[1] - start[1]) < (
        _CONNECTOR_DIAGRAM_MIN_SHAFT_LENGTH_POINTS
    ):
        return None
    return _ConnectorDiagramShaftRecord(
        drawing_index=drawing_index,
        bbox=bbox,
        endpoints=(start, end),
    )


def _connector_diagram_arrowhead_record(
    *,
    drawing: dict[str, Any],
    drawing_index: int,
    bbox: tuple[float, float, float, float],
    rotation_matrix: Any,
) -> _ConnectorDiagramArrowheadRecord | None:
    drawing_type = drawing.get("type")
    raw_items = drawing.get("items")
    if (
        not isinstance(drawing_type, str)
        or "f" not in drawing_type.lower()
        or not isinstance(raw_items, (list, tuple))
        or len(raw_items) != 3
        or any(
            not isinstance(item, (list, tuple)) or len(item) < 3 or item[0] != "l"
            for item in raw_items
        )
    ):
        return None
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for item in raw_items:
        start = _pdf_visual_point(item[1], rotation_matrix=rotation_matrix)
        end = _pdf_visual_point(item[2], rotation_matrix=rotation_matrix)
        if start is None or end is None:
            return None
        segments.append((start, end))
    if any(
        math.hypot(left[1][0] - right[0][0], left[1][1] - right[0][1])
        > _TABLE_GRID_ALIGNMENT_TOLERANCE_POINTS
        for left, right in zip(
            segments,
            (*segments[1:], segments[0]),
            strict=True,
        )
    ):
        return None
    vertices = tuple(segment[0] for segment in segments)
    if len({(round(vertex[0], 3), round(vertex[1], 3)) for vertex in vertices}) != 3:
        return None
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    polygon_area = (
        abs(
            sum(
                left[0] * right[1] - right[0] * left[1]
                for left, right in zip(
                    vertices,
                    (*vertices[1:], vertices[0]),
                    strict=True,
                )
            )
        )
        / 2.0
    )
    if (
        width < _CONNECTOR_DIAGRAM_ARROWHEAD_MIN_DIMENSION
        or height < _CONNECTOR_DIAGRAM_ARROWHEAD_MIN_DIMENSION
        or width > _CONNECTOR_DIAGRAM_ARROWHEAD_MAX_DIMENSION
        or height > _CONNECTOR_DIAGRAM_ARROWHEAD_MAX_DIMENSION
        or polygon_area < _CONNECTOR_DIAGRAM_ARROWHEAD_MIN_POLYGON_AREA
    ):
        return None
    return _ConnectorDiagramArrowheadRecord(
        drawing_index=drawing_index,
        bbox=bbox,
        vertices=(vertices[0], vertices[1], vertices[2]),
    )


def _pdf_visual_point(
    value: Any,
    *,
    rotation_matrix: object,
) -> tuple[float, float] | None:
    try:
        x = float(value.x)
        y = float(value.y)
    except (AttributeError, TypeError, ValueError, OverflowError):
        try:
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                return None
            x = float(value[0])
            y = float(value[1])
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    try:
        a, b, c, d, e, f = (float(item) for item in rotation_matrix)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (x, y)
    transformed = (a * x + c * y + e, b * x + d * y + f)
    return transformed if all(math.isfinite(item) for item in transformed) else None


def _connector_diagram_unique_endpoint_pairs(
    *,
    shafts: tuple[_ConnectorDiagramShaftRecord, ...],
    arrowheads: tuple[_ConnectorDiagramArrowheadRecord, ...],
) -> tuple[
    tuple[_ConnectorDiagramShaftRecord, _ConnectorDiagramArrowheadRecord],
    ...,
]:
    matches_by_shaft: dict[int, list[_ConnectorDiagramArrowheadRecord]] = {}
    shaft_indexes_by_arrowhead: dict[int, list[int]] = {}
    endpoint_checks = 0
    for shaft in shafts:
        for arrowhead in arrowheads:
            endpoint_checks += 1
            if endpoint_checks > _MAX_CONNECTOR_DIAGRAM_ENDPOINT_CHECKS:
                raise _PdfResourceBudgetExceeded
            minimum_distance = min(
                math.hypot(
                    endpoint[0] - vertex[0],
                    endpoint[1] - vertex[1],
                )
                for endpoint in shaft.endpoints
                for vertex in arrowhead.vertices
            )
            if minimum_distance > _CONNECTOR_DIAGRAM_ARROWHEAD_ENDPOINT_GAP_POINTS:
                continue
            matches_by_shaft.setdefault(shaft.drawing_index, []).append(arrowhead)
            shaft_indexes_by_arrowhead.setdefault(
                arrowhead.drawing_index,
                [],
            ).append(shaft.drawing_index)
    shaft_by_index = {shaft.drawing_index: shaft for shaft in shafts}
    return tuple(
        (shaft_by_index[shaft_index], matches[0])
        for shaft_index, matches in sorted(matches_by_shaft.items())
        if len(matches) == 1
        and len(shaft_indexes_by_arrowhead.get(matches[0].drawing_index, ())) == 1
    )


def _connector_diagram_component_roots(
    records: tuple[_ConnectorDiagramDrawingRecord, ...],
) -> dict[int, int]:
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    adjacency_checks = 0
    ordered_indexes = tuple(
        sorted(
            range(len(records)),
            key=lambda index: (
                records[index].bbox[0],
                records[index].bbox[2],
                records[index].bbox[1],
                records[index].bbox[3],
                records[index].drawing_index,
            ),
        )
    )
    for position, record_index in enumerate(ordered_indexes):
        record_bbox = records[record_index].bbox
        horizontal_reach = record_bbox[2] + _CONNECTOR_DIAGRAM_COMPONENT_GAP_POINTS
        for candidate_position in range(position + 1, len(ordered_indexes)):
            candidate_index = ordered_indexes[candidate_position]
            candidate_bbox = records[candidate_index].bbox
            if candidate_bbox[0] > horizontal_reach:
                break
            adjacency_checks += 1
            if adjacency_checks > _MAX_CONNECTOR_DIAGRAM_ADJACENCY_CHECKS:
                raise _PdfResourceBudgetExceeded
            if (
                _bbox_distance(record_bbox, candidate_bbox)
                <= _CONNECTOR_DIAGRAM_COMPONENT_GAP_POINTS
            ):
                union(record_index, candidate_index)
    return {record.drawing_index: find(index) for index, record in enumerate(records)}


def _connector_diagram_has_independent_table_caption(
    *,
    raw_page: Any,
    labeled_blocks: tuple[
        tuple[str, str, int, tuple[float, float, float, float]],
        ...,
    ],
    component_bbox: tuple[float, float, float, float],
) -> bool:
    for record in labeled_blocks:
        if record[0] != "table":
            continue
        text = _quadrilateral_label_block_text(
            raw_page=raw_page,
            block_index=record[2],
        )
        if text is None or _QUADRILATERAL_TABLE_CAPTION_PATTERN.match(text) is None:
            continue
        label_bbox = record[3]
        if _positive_bbox_intersection(label_bbox, component_bbox) is not None or (
            _bbox_distance(label_bbox, component_bbox)
            <= _CONNECTOR_DIAGRAM_CAPTION_GAP_POINTS
            and _quadrilateral_label_is_horizontally_grounded(
                label_bbox=label_bbox,
                component_bbox=component_bbox,
            )
        ):
            return True
    return False


def _connector_diagram_adjacent_text_blocks(
    *,
    raw_page: Any,
    rotation_matrix: Any,
    component_bbox: tuple[float, float, float, float],
    excluded_block_indexes: frozenset[int],
) -> tuple[tuple[int, tuple[float, float, float, float]], ...]:
    blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
    if not isinstance(blocks, (list, tuple)):
        return ()
    adjacent: list[tuple[int, tuple[float, float, float, float]]] = []
    for block_index, block in enumerate(blocks):
        if (
            block_index in excluded_block_indexes
            or not isinstance(block, dict)
            or block.get("type") != 0
            or not _raw_text_from_block(block).strip()
        ):
            continue
        bbox = _pdf_visual_bbox(
            _native_bbox(block.get("bbox")),
            rotation_matrix=rotation_matrix,
        )
        if bbox is None:
            continue
        if _positive_bbox_intersection(bbox, component_bbox) is not None or (
            _bbox_distance(bbox, component_bbox) <= _CONNECTOR_DIAGRAM_TEXT_GAP_POINTS
            and _quadrilateral_label_is_horizontally_grounded(
                label_bbox=bbox,
                component_bbox=component_bbox,
            )
        ):
            adjacent.append((block_index, bbox))
    return tuple(adjacent)


def _connector_diagram_owned_bitfield_table_bboxes(
    *,
    page: Any,
    raw_page: Any,
    component_bbox: tuple[float, float, float, float],
    rotation_matrix: Any,
    resource_budget: _PdfResourceBudget,
) -> tuple[tuple[float, float, float, float], ...]:
    """Own exactly one continuous pair of fully contained 2x4 bit fields.

    The default table detector can expose the two halves of an eight-bit
    connector diagram as independent tables.  Claiming them is deliberately
    all-or-nothing: both candidates must have a merged first row over four
    continuous, equally sized second-row cells, share the same vertical span,
    sit next to one another, and have no independent Table caption.
    """

    try:
        find_tables = getattr(page, "find_tables", None)
    except Exception:
        return ()
    if not callable(find_tables):
        return ()
    try:
        finder = find_tables()
        candidates = getattr(finder, "tables", None)
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return ()
    if not isinstance(candidates, (list, tuple)):
        return ()
    resource_budget.consume_table_candidates(len(candidates))

    table_labels = tuple(
        record
        for record in _pdf_labeled_text_blocks(
            raw_page=raw_page,
            rotation_matrix=rotation_matrix,
        )
        if record[0] == "table"
    )
    eligible: list[
        tuple[
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
        ]
    ] = []
    for candidate in candidates:
        if _candidate_dimensions(candidate) != (2, 4):
            continue
        native_bbox = _candidate_bbox(candidate)
        visible_bbox = _pdf_visual_bbox(
            native_bbox,
            rotation_matrix=rotation_matrix,
        )
        candidate_area = _bbox_area(visible_bbox) if visible_bbox is not None else 0.0
        intersection = _positive_bbox_intersection(
            visible_bbox,
            component_bbox,
        )
        if (
            native_bbox is None
            or visible_bbox is None
            or candidate_area <= 0.0
            or intersection is None
            or _bbox_area(intersection) / candidate_area < 0.98
        ):
            continue
        geometry_observation = _candidate_geometry_observation(candidate)
        column_widths = _connector_diagram_bitfield_column_widths(candidate)
        if (
            geometry_observation is None
            or geometry_observation[0] is None
            or column_widths is None
        ):
            continue
        if _quadrilateral_table_has_independent_caption(
            raw_page=raw_page,
            table_bbox=visible_bbox,
            labels=table_labels,
        ):
            return ()
        eligible.append((native_bbox, visible_bbox, column_widths))
    if len(eligible) != 2:
        return ()

    left, right = sorted(
        eligible,
        key=lambda record: (
            record[1][0],
            record[1][1],
            record[1][2],
            record[1][3],
        ),
    )
    left_bbox = left[1]
    right_bbox = right[1]
    if (
        abs(left_bbox[1] - right_bbox[1])
        > _CONNECTOR_DIAGRAM_TABLE_EDGE_TOLERANCE_POINTS
        or abs(left_bbox[3] - right_bbox[3])
        > _CONNECTOR_DIAGRAM_TABLE_EDGE_TOLERANCE_POINTS
    ):
        return ()
    all_widths = (*left[2], *right[2])
    minimum_width = min(all_widths)
    maximum_width = max(all_widths)
    if (
        minimum_width <= 0.0
        or maximum_width / minimum_width
        > _CONNECTOR_DIAGRAM_TABLE_MAX_COLUMN_WIDTH_RATIO
    ):
        return ()
    ordered_widths = sorted(all_widths)
    median_width = (
        ordered_widths[len(ordered_widths) // 2 - 1]
        + ordered_widths[len(ordered_widths) // 2]
    ) / 2.0
    inter_group_gap = right_bbox[0] - left_bbox[2]
    if (
        inter_group_gap < -_CONNECTOR_DIAGRAM_TABLE_EDGE_TOLERANCE_POINTS
        or inter_group_gap
        > median_width * _CONNECTOR_DIAGRAM_TABLE_MAX_INTER_GROUP_GAP_RATIO
    ):
        return ()
    return (left[0], right[0])


def _connector_diagram_bitfield_column_widths(
    candidate: Any,
) -> tuple[float, float, float, float] | None:
    """Validate a merged header over one continuous row of four cells."""

    if _candidate_dimensions(candidate) != (2, 4):
        return None
    candidate_bbox = _candidate_bbox(candidate)
    if candidate_bbox is None:
        return None
    try:
        native_rows = getattr(candidate, "rows", None)
    except Exception:
        return None
    if not isinstance(native_rows, (list, tuple)) or len(native_rows) != 2:
        return None
    row_cells: list[tuple[tuple[float, float, float, float] | None, ...]] = []
    for row in native_rows:
        try:
            raw_cells = getattr(row, "cells", None)
        except Exception:
            return None
        if not isinstance(raw_cells, (list, tuple)) or len(raw_cells) < 4:
            return None
        cells = tuple(_native_bbox(cell) for cell in raw_cells[:4])
        row_cells.append(cells)
    header_cells, field_cells = row_cells
    header_bbox = header_cells[0]
    if header_bbox is None or any(cell is not None for cell in header_cells[1:]):
        return None
    if any(cell is None for cell in field_cells):
        return None
    concrete_fields = tuple(cell for cell in field_cells if cell is not None)
    if len(concrete_fields) != 4:
        return None

    tolerance = _CONNECTOR_DIAGRAM_TABLE_EDGE_TOLERANCE_POINTS
    if (
        abs(header_bbox[0] - candidate_bbox[0]) > tolerance
        or abs(header_bbox[2] - candidate_bbox[2]) > tolerance
        or abs(header_bbox[1] - candidate_bbox[1]) > tolerance
        or abs(concrete_fields[0][0] - candidate_bbox[0]) > tolerance
        or abs(concrete_fields[-1][2] - candidate_bbox[2]) > tolerance
        or abs(concrete_fields[0][3] - candidate_bbox[3]) > tolerance
        or abs(header_bbox[3] - concrete_fields[0][1]) > tolerance
    ):
        return None
    baseline = (concrete_fields[0][1], concrete_fields[0][3])
    if any(
        abs(cell[1] - baseline[0]) > tolerance or abs(cell[3] - baseline[1]) > tolerance
        for cell in concrete_fields[1:]
    ):
        return None
    if any(
        abs(left[2] - right[0]) > tolerance
        for left, right in zip(
            concrete_fields,
            concrete_fields[1:],
        )
    ):
        return None
    widths = tuple(cell[2] - cell[0] for cell in concrete_fields)
    if (
        len(widths) != 4
        or min(widths) <= 0.0
        or max(widths) / min(widths) > _CONNECTOR_DIAGRAM_TABLE_MAX_COLUMN_WIDTH_RATIO
    ):
        return None
    return (widths[0], widths[1], widths[2], widths[3])


def _vector_figure_proof_description(proof_kind: str) -> str:
    if proof_kind == "curve_seeded":
        return "curve-seeded compound vector figure"
    if proof_kind == "sparse_filled_table_figure":
        return "caption-grounded sparse filled figure"
    if proof_kind == "caption_anchored_quadrilateral":
        return "reference-and-caption-grounded stroked quadrilateral figure"
    if proof_kind == "caption_anchored_connector_diagram":
        return "caption-grounded connector-and-arrowhead diagram"
    return "proof-grounded vector figure"


def _vector_figure_diagnostic_code(*, proof_kind: str, event: str) -> str:
    codes = {
        "curve_seeded": {
            "crop_render_failed": "pdf_vector_figure_crop_render_failed",
            "image_occurrence_folded": "pdf_vector_figure_image_occurrence_folded",
            "materialized": "pdf_vector_figure_materialized",
            "table_candidate_folded": "pdf_vector_figure_table_candidate_folded",
        },
        "sparse_filled_table_figure": {
            "crop_render_failed": "pdf_sparse_filled_figure_crop_render_failed",
            "image_occurrence_folded": (
                "pdf_sparse_filled_figure_image_occurrence_folded"
            ),
            "materialized": "pdf_sparse_filled_figure_materialized",
            "table_candidate_folded": (
                "pdf_sparse_filled_figure_table_candidate_folded"
            ),
        },
        "caption_anchored_quadrilateral": {
            "crop_render_failed": (
                "pdf_caption_anchored_quadrilateral_figure_crop_render_failed"
            ),
            "image_occurrence_folded": (
                "pdf_caption_anchored_quadrilateral_figure_image_occurrence_folded"
            ),
            "materialized": ("pdf_caption_anchored_quadrilateral_figure_materialized"),
            "table_candidate_folded": (
                "pdf_caption_anchored_quadrilateral_figure_table_candidate_folded"
            ),
        },
        "caption_anchored_connector_diagram": {
            "crop_render_failed": (
                "pdf_caption_anchored_connector_diagram_crop_render_failed"
            ),
            "image_occurrence_folded": (
                "pdf_caption_anchored_connector_diagram_image_occurrence_folded"
            ),
            "materialized": ("pdf_caption_anchored_connector_diagram_materialized"),
            "table_candidate_folded": (
                "pdf_caption_anchored_connector_diagram_table_candidate_folded"
            ),
        },
    }
    return codes.get(proof_kind, codes["curve_seeded"]).get(
        event,
        f"pdf_vector_figure_{event}",
    )


def _sparse_filled_table_figure_regions_from_page(
    *,
    page: Any,
    raw_page: Any,
    page_number: int,
    page_bbox: tuple[float, float, float, float] | None,
    rotation_matrix: Any,
    resource_budget: _PdfResourceBudget,
) -> tuple[_VectorFigureRegion, ...]:
    """Recognize one caption/reference-grounded sparse filled figure.

    Some native vector diagrams expose a phantom many-column table grid.  A
    candidate is retyped only when its grid geometry is unproven, most logical
    slots are empty, material filled paths occupy the region, and a nearby
    native Figure label grounds the crop.  Any ambiguity keeps the TABLE path.
    """

    if page_bbox is None or _bbox_area(page_bbox) <= 0.0:
        return ()
    label_blocks = _pdf_labeled_text_blocks(
        raw_page=raw_page,
        rotation_matrix=rotation_matrix,
    )
    if not any(record[0] == "figure" for record in label_blocks):
        return ()
    try:
        get_drawings = getattr(page, "get_drawings", None)
        find_tables = getattr(page, "find_tables", None)
    except Exception:
        return ()
    if not callable(get_drawings) or not callable(find_tables):
        return ()
    try:
        drawings = get_drawings()
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return ()
    if (
        not isinstance(drawings, (list, tuple))
        or len(drawings) > _MAX_PDF_DRAWINGS_PER_PAGE
    ):
        return ()
    significant_fill_bboxes: list[tuple[float, float, float, float]] = []
    drawing_item_count = 0
    try:
        for drawing in drawings:
            if not isinstance(drawing, dict) or drawing.get("fill") is None:
                continue
            raw_items = drawing.get("items")
            if not isinstance(raw_items, (list, tuple)):
                continue
            drawing_item_count += len(raw_items)
            if drawing_item_count > _MAX_PDF_DRAWING_ITEMS_PER_PAGE:
                raise _PdfResourceBudgetExceeded
            fill_opacity = float(drawing.get("fill_opacity", 1.0))
            if not math.isfinite(fill_opacity) or fill_opacity <= 0.0:
                continue
            bbox = _pdf_visual_bbox(
                _native_bbox(drawing.get("rect") or drawing.get("bbox")),
                rotation_matrix=rotation_matrix,
            )
            bbox = _positive_bbox_intersection(bbox, page_bbox)
            if (
                bbox is not None
                and _bbox_area(bbox) >= _SPARSE_FILLED_FIGURE_MIN_FILL_PATH_AREA
            ):
                significant_fill_bboxes.append(bbox)
    except _PdfResourceBudgetExceeded:
        raise
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except (TypeError, ValueError, OverflowError):
        return ()
    if not (
        _SPARSE_FILLED_FIGURE_MIN_SIGNIFICANT_FILL_PATHS
        <= len(significant_fill_bboxes)
        <= _SPARSE_FILLED_FIGURE_MAX_FILL_PATHS
    ):
        return ()

    try:
        default_finder = find_tables()
        default_candidates = getattr(default_finder, "tables", None)
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return ()
    if not isinstance(default_candidates, (list, tuple)):
        return ()
    raw_strict_candidates = _lines_strict_table_candidates(find_tables)
    resource_budget.consume_table_candidates(
        len(default_candidates) + len(raw_strict_candidates)
    )
    page_bboxlog = _safe_page_bboxlog(page)
    prepared_candidates = tuple(
        _prepare_table_candidate(candidate, page_bboxlog=page_bboxlog)
        for candidate in default_candidates
    )
    eligible: list[tuple[Any, tuple[float, float, float, float], int, int, int]] = []
    for candidate in prepared_candidates:
        bbox = _candidate_bbox(candidate)
        dimensions = _candidate_dimensions(candidate)
        text_observation = _table_suppression_candidate_text(candidate)
        geometry_observation = _candidate_geometry_observation(candidate)
        if (
            bbox is None
            or dimensions is None
            or text_observation is None
            or geometry_observation is None
        ):
            continue
        row_count, column_count = dimensions
        _normalized_text, nonempty_count = text_observation
        proven_geometry, _candidate_text, _tokens = geometry_observation
        slot_count = row_count * column_count
        if (
            column_count < _SPARSE_FILLED_FIGURE_MIN_COLUMNS
            or slot_count < 1
            or nonempty_count < 1
            or nonempty_count > slot_count * _SPARSE_FILLED_FIGURE_MAX_OCCUPANCY_RATIO
            or proven_geometry is not None
        ):
            continue
        eligible.append((candidate, bbox, row_count, column_count, nonempty_count))
    if not eligible:
        return ()

    groups: list[
        list[tuple[Any, tuple[float, float, float, float], int, int, int]]
    ] = []
    for record in sorted(eligible, key=lambda item: (item[1][1], item[1][0])):
        if not groups:
            groups.append([record])
            continue
        prior_bbox = groups[-1][-1][1]
        bbox = record[1]
        vertical_gap = bbox[1] - prior_bbox[3]
        if (
            abs(bbox[0] - prior_bbox[0])
            <= _SPARSE_FILLED_FIGURE_MAX_HORIZONTAL_EDGE_DRIFT
            and abs(bbox[2] - prior_bbox[2])
            <= _SPARSE_FILLED_FIGURE_MAX_HORIZONTAL_EDGE_DRIFT
            and _SPARSE_FILLED_FIGURE_MIN_VERTICAL_GAP
            <= vertical_gap
            <= _SPARSE_FILLED_FIGURE_MAX_VERTICAL_GAP
        ):
            groups[-1].append(record)
        else:
            groups.append([record])

    page_area = _bbox_area(page_bbox)
    regions: list[_VectorFigureRegion] = []
    for group in groups:
        owned_table_bboxes = tuple(record[1] for record in group)
        region_bbox = _bbox_union(owned_table_bboxes)
        if region_bbox is None:
            continue
        region_area = _bbox_area(region_bbox)
        total_slots = sum(record[2] * record[3] for record in group)
        total_nonempty = sum(record[4] for record in group)
        max_columns = max(record[3] for record in group)
        if (
            region_area <= 0.0
            or region_area / page_area < _SPARSE_FILLED_FIGURE_MIN_PAGE_AREA_RATIO
            or region_area / page_area >= _SPARSE_FILLED_FIGURE_MAX_PAGE_AREA_RATIO
            or max_columns < _SPARSE_FILLED_FIGURE_MIN_COLUMNS
            or total_slots < 1
            or total_nonempty > total_slots * _SPARSE_FILLED_FIGURE_MAX_OCCUPANCY_RATIO
        ):
            continue
        clipped_fill_bboxes = tuple(
            intersection
            for fill_bbox in significant_fill_bboxes
            if (
                intersection := _positive_bbox_intersection(
                    fill_bbox,
                    region_bbox,
                )
            )
            is not None
            and _bbox_area(intersection) >= _SPARSE_FILLED_FIGURE_MIN_FILL_PATH_AREA
        )
        if (
            len(clipped_fill_bboxes) < _SPARSE_FILLED_FIGURE_MIN_SIGNIFICANT_FILL_PATHS
            or _bbox_union_area(clipped_fill_bboxes) / region_area
            < _SPARSE_FILLED_FIGURE_MIN_FILL_COVERAGE_RATIO
        ):
            continue
        nearby_figure_labels = _nearby_pdf_labels(
            label_blocks,
            kind="figure",
            region_bbox=region_bbox,
            above_gap=_SPARSE_FILLED_FIGURE_REFERENCE_GAP,
            below_gap=_SPARSE_FILLED_FIGURE_CAPTION_GAP,
        )
        nearby_table_labels = _nearby_pdf_labels(
            label_blocks,
            kind="table",
            region_bbox=region_bbox,
            above_gap=_SPARSE_FILLED_FIGURE_REFERENCE_GAP,
            below_gap=_SPARSE_FILLED_FIGURE_CAPTION_GAP,
        )
        distinct_figure_labels = {record[1] for record in nearby_figure_labels}
        if len(distinct_figure_labels) != 1 or nearby_table_labels:
            continue
        padded_bbox = _padded_clipped_bbox(
            region_bbox,
            page_bbox=page_bbox,
            padding=_VECTOR_FIGURE_PADDING_POINTS,
        )
        if padded_bbox is None:
            continue
        label_sha256 = hashlib.sha256(
            next(iter(distinct_figure_labels)).encode("utf-8")
        ).hexdigest()
        evidence_payload = {
            "schema": "pdf-sparse-filled-table-figure-proof/v1",
            "page_number": page_number,
            "bbox": _rounded_bbox(padded_bbox),
            "owned_table_bboxes": [_rounded_bbox(bbox) for bbox in owned_table_bboxes],
            "candidate_dimensions": [[record[2], record[3]] for record in group],
            "total_slots": total_slots,
            "total_nonempty": total_nonempty,
            "max_columns": max_columns,
            "fill_path_count": len(clipped_fill_bboxes),
            "fill_union_area": round(_bbox_union_area(clipped_fill_bboxes), 6),
            "region_area": round(region_area, 6),
            "figure_label_sha256": label_sha256,
            "figure_label_block_indexes": sorted(
                {record[2] for record in nearby_figure_labels}
            ),
        }
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                evidence_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        regions.append(
            _VectorFigureRegion(
                bbox=padded_bbox,
                drawing_count=len(clipped_fill_bboxes),
                anchor_count=len(clipped_fill_bboxes),
                anchor_bboxes=tuple(
                    sorted(
                        clipped_fill_bboxes,
                        key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
                    )
                ),
                proof_kind="sparse_filled_table_figure",
                owned_table_bboxes=owned_table_bboxes,
                evidence_sha256=evidence_sha256,
            )
        )
    return tuple(regions) if len(regions) == 1 else ()


def _pdf_labeled_text_blocks(
    *,
    raw_page: Any,
    rotation_matrix: Any,
) -> tuple[tuple[str, str, int, tuple[float, float, float, float]], ...]:
    blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
    if not isinstance(blocks, (list, tuple)):
        return ()
    records: list[tuple[str, str, int, tuple[float, float, float, float]]] = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        bbox = _pdf_visual_bbox(
            _native_bbox(block.get("bbox")),
            rotation_matrix=rotation_matrix,
        )
        if bbox is None:
            continue
        raw_text = _raw_text_from_block(block)
        for kind, pattern in (
            ("figure", _PDF_FIGURE_LABEL_PATTERN),
            ("table", _PDF_TABLE_LABEL_PATTERN),
        ):
            for match in pattern.finditer(raw_text):
                records.append(
                    (
                        kind,
                        match.group(1).replace("-", "."),
                        block_index,
                        bbox,
                    )
                )
    return tuple(records)


def _nearby_pdf_labels(
    records: tuple[tuple[str, str, int, tuple[float, float, float, float]], ...],
    *,
    kind: str,
    region_bbox: tuple[float, float, float, float],
    above_gap: float,
    below_gap: float,
) -> tuple[tuple[str, str, int, tuple[float, float, float, float]], ...]:
    region_width = region_bbox[2] - region_bbox[0]
    if region_width <= 0.0:
        return ()
    matches = []
    for record in records:
        if record[0] != kind:
            continue
        bbox = record[3]
        horizontal_overlap = max(
            0.0,
            min(region_bbox[2], bbox[2]) - max(region_bbox[0], bbox[0]),
        )
        center_x = (bbox[0] + bbox[2]) / 2.0
        horizontally_grounded = (
            horizontal_overlap / region_width >= 0.10
            or region_bbox[0] - region_width * 0.15
            <= center_x
            <= region_bbox[2] + region_width * 0.15
        )
        if not horizontally_grounded:
            continue
        gap_above = region_bbox[1] - bbox[3]
        gap_below = bbox[1] - region_bbox[3]
        if 0.0 <= gap_above <= above_gap or 0.0 <= gap_below <= below_gap:
            matches.append(record)
    return tuple(matches)


def _bbox_union_area(
    bboxes: tuple[tuple[float, float, float, float], ...],
) -> float:
    if not bboxes:
        return 0.0
    x_coordinates = sorted({bbox[0] for bbox in bboxes} | {bbox[2] for bbox in bboxes})
    area = 0.0
    for left, right in zip(x_coordinates, x_coordinates[1:]):
        if right <= left:
            continue
        intervals = sorted(
            (bbox[1], bbox[3])
            for bbox in bboxes
            if bbox[0] < right and bbox[2] > left and bbox[3] > bbox[1]
        )
        if not intervals:
            continue
        merged_height = 0.0
        start, end = intervals[0]
        for next_start, next_end in intervals[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                merged_height += end - start
                start, end = next_start, next_end
        merged_height += end - start
        area += (right - left) * merged_height
    return area


def _vector_figure_regions_from_page(
    *,
    page: Any,
    page_bbox: tuple[float, float, float, float] | None,
    rotation_matrix: Any,
) -> tuple[_VectorFigureRegion, ...]:
    """Find conservative compound-vector figure regions.

    A region must be seeded by a material Bézier path.  Rectangle-only paths
    (a common representation for ruled-table cells) cannot recursively bridge
    components; one enclosure may only attach directly to non-rectangle core
    paths within the bounded component gap.  Nearby non-rectangle paths may
    join the seeded component so a flowchart or engineering diagram is
    represented once instead of as dozens of unrelated paint fragments.
    """

    if page_bbox is None:
        return ()
    try:
        get_drawings = getattr(page, "get_drawings", None)
    except Exception:
        return ()
    if not callable(get_drawings):
        return ()
    try:
        drawings = get_drawings()
    except MemoryError:
        raise _PdfResourceBudgetExceeded
    except Exception:
        return ()
    if not isinstance(drawings, (list, tuple)):
        return ()
    if len(drawings) > _MAX_PDF_DRAWINGS_PER_PAGE:
        raise _PdfResourceBudgetExceeded

    page_area = _bbox_area(page_bbox)
    if page_area <= 0.0:
        return ()
    records: list[_VectorDrawingRecord] = []
    drawing_item_count = 0
    for drawing in drawings:
        if not isinstance(drawing, dict):
            continue
        raw_items = drawing.get("items")
        if not isinstance(raw_items, (list, tuple)):
            continue
        drawing_item_count += len(raw_items)
        if drawing_item_count > _MAX_PDF_DRAWING_ITEMS_PER_PAGE:
            raise _PdfResourceBudgetExceeded
        try:
            operations = tuple(
                str(item[0])
                for item in raw_items
                if isinstance(item, (list, tuple)) and item
            )
        except MemoryError:
            raise _PdfResourceBudgetExceeded
        if not operations:
            continue
        raw_bbox = drawing.get("rect") or drawing.get("bbox")
        bbox = _pdf_visual_bbox(
            _native_bbox(raw_bbox),
            rotation_matrix=rotation_matrix,
        )
        bbox = _expanded_degenerate_drawing_bbox(
            bbox,
            stroke_width=drawing.get("width"),
        )
        clipped_bbox = _positive_bbox_intersection(bbox, page_bbox)
        if clipped_bbox is None:
            continue
        width = clipped_bbox[2] - clipped_bbox[0]
        height = clipped_bbox[3] - clipped_bbox[1]
        area = width * height
        rectangle_only = all(operation == "re" for operation in operations)
        if (
            rectangle_only
            and min(width, height) <= _VECTOR_FIGURE_THIN_FILL_MAX_DIMENSION
        ):
            continue
        if area / page_area >= _VECTOR_FIGURE_MAX_PAGE_AREA_RATIO:
            continue
        anchor = (
            "c" in operations
            and width >= _VECTOR_FIGURE_ANCHOR_MIN_WIDTH
            and height >= _VECTOR_FIGURE_ANCHOR_MIN_HEIGHT
            and (
                area >= _VECTOR_FIGURE_ANCHOR_MIN_AREA
                or (
                    width >= _VECTOR_FIGURE_ANCHOR_WIDE_WIDTH
                    and height >= _VECTOR_FIGURE_ANCHOR_WIDE_HEIGHT
                )
            )
        )
        records.append(
            _VectorDrawingRecord(
                bbox=clipped_bbox,
                anchor=anchor,
                rectangle_only=rectangle_only,
            )
        )

    bridge_indexes = tuple(
        index for index, record in enumerate(records) if not record.rectangle_only
    )
    anchor_indexes = tuple(index for index in bridge_indexes if records[index].anchor)
    if not anchor_indexes:
        return ()

    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    adjacency_checks = 0
    ordered_bridge_indexes = tuple(
        sorted(
            bridge_indexes,
            key=lambda index: (
                records[index].bbox[0],
                records[index].bbox[2],
                records[index].bbox[1],
                records[index].bbox[3],
                index,
            ),
        )
    )
    for position, record_index in enumerate(ordered_bridge_indexes):
        record_bbox = records[record_index].bbox
        horizontal_reach = record_bbox[2] + _VECTOR_FIGURE_COMPONENT_GAP_POINTS
        for candidate_position in range(
            position + 1,
            len(ordered_bridge_indexes),
        ):
            candidate_index = ordered_bridge_indexes[candidate_position]
            candidate_bbox = records[candidate_index].bbox
            if candidate_bbox[0] > horizontal_reach:
                break
            adjacency_checks += 1
            if adjacency_checks > _MAX_VECTOR_FIGURE_ADJACENCY_CHECKS:
                raise _PdfResourceBudgetExceeded
            if (
                _bbox_distance(record_bbox, candidate_bbox)
                <= _VECTOR_FIGURE_COMPONENT_GAP_POINTS
            ):
                union(record_index, candidate_index)

    core_indexes_by_root: dict[int, set[int]] = {}
    for record_index in bridge_indexes:
        core_indexes_by_root.setdefault(find(record_index), set()).add(record_index)
    core_components = list(core_indexes_by_root.values())
    core_bboxes = [
        _bbox_union(tuple(records[index].bbox for index in component))
        for component in core_components
    ]
    component_parent = list(range(len(core_components)))

    def find_component(index: int) -> int:
        while component_parent[index] != index:
            component_parent[index] = component_parent[component_parent[index]]
            index = component_parent[index]
        return index

    def union_components(left: int, right: int) -> None:
        left_root = find_component(left)
        right_root = find_component(right)
        if left_root != right_root:
            component_parent[right_root] = left_root

    enclosure_matches: dict[int, tuple[int, ...]] = {}
    for record_index, record in enumerate(records):
        if not record.rectangle_only:
            continue
        matching_components: list[int] = []
        for component_index, component_bbox in enumerate(core_bboxes):
            if component_bbox is None:
                continue
            adjacency_checks += 1
            if adjacency_checks > _MAX_VECTOR_FIGURE_ADJACENCY_CHECKS:
                raise _PdfResourceBudgetExceeded
            if (
                _bbox_distance(record.bbox, component_bbox)
                <= _VECTOR_FIGURE_ENCLOSURE_ATTACHMENT_GAP_POINTS
            ):
                matching_components.append(component_index)
        if not matching_components:
            continue
        enclosure_matches[record_index] = tuple(matching_components)
        first_component = matching_components[0]
        for component_index in matching_components[1:]:
            union_components(first_component, component_index)

    component_records_by_root: dict[int, set[int]] = {}
    component_has_anchor: dict[int, bool] = {}
    for component_index, component in enumerate(core_components):
        root = find_component(component_index)
        component_records_by_root.setdefault(root, set()).update(component)
        component_has_anchor[root] = component_has_anchor.get(root, False) or any(
            records[index].anchor for index in component
        )
    for record_index, matching_components in enclosure_matches.items():
        root = find_component(matching_components[0])
        component_records_by_root.setdefault(root, set()).add(record_index)

    regions: list[_VectorFigureRegion] = []
    for root, component in component_records_by_root.items():
        if not component_has_anchor.get(root, False):
            continue
        component_bbox = _bbox_union(tuple(records[index].bbox for index in component))
        if component_bbox is None:
            continue
        padded_bbox = _padded_clipped_bbox(
            component_bbox,
            page_bbox=page_bbox,
            padding=_VECTOR_FIGURE_PADDING_POINTS,
        )
        if padded_bbox is None:
            continue
        width = padded_bbox[2] - padded_bbox[0]
        height = padded_bbox[3] - padded_bbox[1]
        if (
            width < _VECTOR_FIGURE_ANCHOR_MIN_WIDTH
            or height < _VECTOR_FIGURE_ANCHOR_WIDE_HEIGHT
            or width * height < _VECTOR_FIGURE_MIN_REGION_AREA
            or width * height / page_area >= _VECTOR_FIGURE_MAX_PAGE_AREA_RATIO
        ):
            continue
        page_height = page_bbox[3] - page_bbox[1]
        center_y = (padded_bbox[1] + padded_bbox[3]) / 2.0
        if (
            center_y <= page_bbox[1] + page_height * _VECTOR_FIGURE_HEADER_BAND_RATIO
            or center_y >= page_bbox[1] + page_height * _VECTOR_FIGURE_FOOTER_BAND_RATIO
        ):
            continue
        component_anchor_bboxes = tuple(
            sorted(
                (records[index].bbox for index in component if records[index].anchor),
                key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
            )
        )
        regions.append(
            _VectorFigureRegion(
                bbox=padded_bbox,
                drawing_count=len(component),
                anchor_count=len(component_anchor_bboxes),
                anchor_bboxes=component_anchor_bboxes,
            )
        )
    return tuple(
        sorted(
            regions,
            key=lambda region: (
                region.bbox[1],
                region.bbox[0],
                region.bbox[3],
                region.bbox[2],
            ),
        )
    )


def _expanded_degenerate_drawing_bbox(
    bbox: tuple[float, float, float, float] | None,
    *,
    stroke_width: object,
) -> tuple[float, float, float, float] | None:
    """Give zero-extent stroked connectors a bounded bridge-only footprint."""

    if bbox is None:
        return None
    try:
        numeric_stroke_width = (
            float(stroke_width)
            if not isinstance(stroke_width, bool) and stroke_width is not None
            else 0.0
        )
    except (TypeError, ValueError, OverflowError):
        numeric_stroke_width = 0.0
    if not math.isfinite(numeric_stroke_width):
        numeric_stroke_width = 0.0
    half_width = max(
        abs(numeric_stroke_width) / 2.0,
        _VECTOR_FIGURE_ZERO_EXTENT_HALF_WIDTH_POINTS,
    )
    x0, y0, x1, y1 = bbox
    if x1 == x0:
        x0 -= half_width
        x1 += half_width
    if y1 == y0:
        y0 -= half_width
        y1 += half_width
    return (x0, y0, x1, y1)


def _bbox_distance(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    horizontal_gap = max(0.0, right[0] - left[2], left[0] - right[2])
    vertical_gap = max(0.0, right[1] - left[3], left[1] - right[3])
    return math.hypot(horizontal_gap, vertical_gap)


def _positive_bbox_intersection(
    left: tuple[float, float, float, float] | None,
    right: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if left is None or right is None:
        return None
    intersection = (
        max(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        min(left[3], right[3]),
    )
    return intersection if _bbox_area(intersection) > 0.0 else None


def _bbox_union(
    values: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float] | None:
    if not values:
        return None
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    )


def _padded_clipped_bbox(
    bbox: tuple[float, float, float, float],
    *,
    page_bbox: tuple[float, float, float, float],
    padding: float,
) -> tuple[float, float, float, float] | None:
    return _positive_bbox_intersection(
        (
            bbox[0] - padding,
            bbox[1] - padding,
            bbox[2] + padding,
            bbox[3] + padding,
        ),
        page_bbox,
    )


def _vector_figure_containing_bbox(
    figures: list[_MaterializedVectorFigure],
    *,
    bbox: tuple[float, float, float, float] | None,
    minimum_containment_ratio: float = 0.98,
) -> _MaterializedVectorFigure | None:
    if (
        bbox is None
        or _bbox_area(bbox) <= 0.0
        or not 0.0 < minimum_containment_ratio <= 1.0
    ):
        return None
    matches: list[_MaterializedVectorFigure] = []
    bbox_area = _bbox_area(bbox)
    for figure in figures:
        intersection = _positive_bbox_intersection(bbox, figure.region.bbox)
        if (
            intersection is not None
            and _bbox_area(intersection) / bbox_area >= minimum_containment_ratio
        ):
            matches.append(figure)
    if not matches:
        return None
    return min(matches, key=lambda figure: _bbox_area(figure.region.bbox))


def _curve_figure_masked_companion_ownership(
    *,
    figures: list[_MaterializedVectorFigure],
    blocks: Any,
    rotation_matrix: object,
) -> dict[int, _CurveFigureMaskedCompanionOwnership]:
    """Prove masked fragments duplicate folded degenerate companions.

    Some authored vector diagrams encode each visible raster fragment as a
    masked image plus a mask-free one-pixel companion at the same placement.
    A curve-seeded compound figure already owns the companion.  The masked
    fragment may join that disposition only when the pairing is unique,
    bidirectional overlap is nearly exact, and the companion is intrinsically
    degenerate.  Materialization success is checked later before the masked
    source fact is actually folded.
    """

    if not isinstance(blocks, (list, tuple)):
        return {}
    curve_figures = [
        figure for figure in figures if figure.region.proof_kind == "curve_seeded"
    ]
    if not curve_figures:
        return {}

    masked: list[_CurveFigureImageOccurrence] = []
    companions: list[_CurveFigureImageOccurrence] = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 1:
            continue
        bbox = _pdf_visual_bbox(
            _native_bbox(block.get("bbox")),
            rotation_matrix=rotation_matrix,
        )
        if bbox is None or _bbox_area(bbox) <= 0.0:
            continue
        figure = _vector_figure_containing_bbox(curve_figures, bbox=bbox)
        if figure is None:
            continue
        try:
            width, height = _native_image_dimensions(
                width=block.get("width"),
                height=block.get("height"),
            )
        except _ImageMaskMaterializationRejected:
            continue
        occurrence = _CurveFigureImageOccurrence(
            block_index=block_index,
            bbox=bbox,
            width=width,
            height=height,
            figure=figure,
        )
        if block.get("mask") is not None:
            masked.append(occurrence)
        elif min(width, height) == 1 and max(width, height) > 1:
            companions.append(occurrence)

    matches_by_masked: dict[
        int,
        list[tuple[_CurveFigureImageOccurrence, float, float]],
    ] = {}
    masked_indexes_by_companion: dict[int, list[int]] = {}
    check_count = 0
    for masked_occurrence in masked:
        masked_area = _bbox_area(masked_occurrence.bbox)
        for companion in companions:
            check_count += 1
            if check_count > _MAX_VECTOR_FIGURE_MASK_COMPANION_CHECKS:
                return {}
            if companion.figure.node_id != masked_occurrence.figure.node_id:
                continue
            intersection = _positive_bbox_intersection(
                masked_occurrence.bbox,
                companion.bbox,
            )
            if intersection is None:
                continue
            intersection_area = _bbox_area(intersection)
            companion_area = _bbox_area(companion.bbox)
            masked_overlap_ratio = intersection_area / masked_area
            companion_overlap_ratio = intersection_area / companion_area
            if (
                masked_overlap_ratio < _VECTOR_FIGURE_MASKED_MIN_COMPANION_OVERLAP_RATIO
                or companion_overlap_ratio
                < _VECTOR_FIGURE_COMPANION_MIN_MASKED_OVERLAP_RATIO
            ):
                continue
            matches_by_masked.setdefault(masked_occurrence.block_index, []).append(
                (
                    companion,
                    masked_overlap_ratio,
                    companion_overlap_ratio,
                )
            )
            masked_indexes_by_companion.setdefault(
                companion.block_index,
                [],
            ).append(masked_occurrence.block_index)

    ownership: dict[int, _CurveFigureMaskedCompanionOwnership] = {}
    masked_by_index = {occurrence.block_index: occurrence for occurrence in masked}
    for masked_index, matches in matches_by_masked.items():
        if len(matches) != 1:
            continue
        companion, masked_overlap_ratio, companion_overlap_ratio = matches[0]
        if len(masked_indexes_by_companion.get(companion.block_index, ())) != 1:
            continue
        masked_occurrence = masked_by_index[masked_index]
        ownership[masked_index] = _CurveFigureMaskedCompanionOwnership(
            figure=masked_occurrence.figure,
            companion_block_index=companion.block_index,
            masked_bbox=masked_occurrence.bbox,
            companion_bbox=companion.bbox,
            companion_width=companion.width,
            companion_height=companion.height,
            masked_overlap_ratio=masked_overlap_ratio,
            companion_overlap_ratio=companion_overlap_ratio,
        )
    return ownership


def _curve_figure_masked_companion_evidence_sha256(
    *,
    source_sha256: str,
    page_number: int,
    masked_block_index: int,
    ownership: _CurveFigureMaskedCompanionOwnership,
    materialized_image: _MaterializedImageAsset,
) -> str:
    derivation = materialized_image.derivation
    if derivation is None:
        raise ValueError("masked companion evidence requires proven materialization")
    payload = {
        "schema": "pdf-curve-figure-masked-companion-ownership/v1",
        "source_sha256": source_sha256,
        "page_number": page_number,
        "masked_block_index": masked_block_index,
        "companion_block_index": ownership.companion_block_index,
        "figure_bbox": list(_rounded_bbox(ownership.figure.region.bbox)),
        "masked_bbox": list(_rounded_bbox(ownership.masked_bbox)),
        "companion_bbox": list(_rounded_bbox(ownership.companion_bbox)),
        "companion_dimensions": [
            ownership.companion_width,
            ownership.companion_height,
        ],
        "masked_overlap_ratio": round(ownership.masked_overlap_ratio, 6),
        "companion_overlap_ratio": round(ownership.companion_overlap_ratio, 6),
        "figure_render_sha256": ownership.figure.rendered.image_sha256,
        "composite_sha256": materialized_image.content_sha256,
        "base_content_sha256": derivation.base_content_sha256,
        "mask_content_sha256": derivation.mask_content_sha256,
        "materialization_strategy": materialized_image.mask_materialization_strategy,
        "disposition": "FOLD_MASKED_DUPLICATE_INTO_CURVE_FIGURE",
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _vector_figure_for_image_occurrence(
    figures: list[_MaterializedVectorFigure],
    *,
    bbox: tuple[float, float, float, float] | None,
    block_index: int,
    has_mask: bool,
) -> _MaterializedVectorFigure | None:
    """Return a figure only when its proof authorizes this image disposition."""

    figure = _vector_figure_containing_bbox(figures, bbox=bbox)
    if figure is None:
        return None
    if figure.region.proof_kind in {
        "caption_anchored_quadrilateral",
        "caption_anchored_connector_diagram",
    }:
        return (
            figure if block_index in figure.region.owned_image_block_indexes else None
        )
    return None if has_mask else figure


def _table_candidate_owns_all_vector_anchors(
    *,
    table_node: SourceNode,
    figure: _MaterializedVectorFigure,
) -> bool:
    """Keep a real grid when every curve seed is local to one of its cells."""

    table = table_node.table_payload
    if table is None or table.row_count < 2 or table.column_count < 2:
        return False
    anchor_bboxes = figure.region.anchor_bboxes
    if not anchor_bboxes:
        return False
    cell_bboxes = tuple(
        cell.native_locator.bbox
        for cell in table.cells
        if cell.native_locator.bbox is not None
        and _bbox_area(cell.native_locator.bbox) > 0.0
    )
    if not cell_bboxes:
        return False
    for anchor_bbox in anchor_bboxes:
        anchor_area = _bbox_area(anchor_bbox)
        if not any(
            (
                intersection := _positive_bbox_intersection(
                    anchor_bbox,
                    cell_bbox,
                )
            )
            is not None
            and _bbox_area(intersection) / anchor_area >= 0.98
            for cell_bbox in cell_bboxes
        ):
            return False
    return True


def _vector_figure_owns_table_candidate(
    *,
    table_node: SourceNode,
    figure: _MaterializedVectorFigure,
) -> bool:
    """Match only table layouts explicitly claimed by a non-curve proof.

    Caption-grounded figure proofs carry the exact detector bboxes used to
    establish the region.  Other overlapping tables remain independent source
    facts, including a real table immediately following a diagram.
    """

    table_bbox = table_node.native_locator.bbox
    if table_bbox is None or _bbox_area(table_bbox) <= 0.0:
        return False
    matches = 0
    for owned_bbox in figure.region.owned_table_bboxes:
        intersection = _positive_bbox_intersection(table_bbox, owned_bbox)
        if intersection is None:
            continue
        intersection_area = _bbox_area(intersection)
        if (
            intersection_area / _bbox_area(table_bbox) >= 0.98
            and intersection_area / _bbox_area(owned_bbox) >= 0.98
        ):
            matches += 1
    return matches == 1


def _render_pdf_crop(
    *,
    page: Any,
    bbox: tuple[float, float, float, float],
    scale: float,
    max_pixels: int,
    resource_budget: _PdfResourceBudget,
) -> _RenderedPdfPage:
    """Render one bounded, annotation-free visible-page crop."""

    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("render scale must be finite and positive")
    clip = pymupdf.Rect(bbox)
    page_rect = pymupdf.Rect(page.rect)
    if not page_rect.contains(clip):
        raise ValueError("render crop is outside its page")
    matrix = pymupdf.Matrix(scale, scale)
    transformed = clip * matrix
    estimated_width = int(transformed.irect.width)
    estimated_height = int(transformed.irect.height)
    if estimated_width < 1 or estimated_height < 1:
        raise ValueError("rendered crop has invalid dimensions")
    estimated_pixel_count = estimated_width * estimated_height
    if estimated_pixel_count > max_pixels:
        raise ValueError("rendered crop exceeds the pixel policy")
    resource_budget.consume_visual_render_bytes(estimated_pixel_count * 4)
    pixmap = page.get_pixmap(
        matrix=matrix,
        clip=clip,
        alpha=False,
        annots=False,
    )
    width = int(pixmap.width)
    height = int(pixmap.height)
    if width < 1 or height < 1:
        raise ValueError("rendered crop has invalid dimensions")
    pixel_count = width * height
    if pixel_count > max_pixels:
        raise ValueError("rendered crop exceeds the pixel policy")
    if pixel_count > estimated_pixel_count:
        resource_budget.consume_visual_render_bytes(pixel_count * 4)
    image_bytes = bytes(pixmap.tobytes("png"))
    if not image_bytes:
        raise ValueError("rendered crop is empty")
    return _RenderedPdfPage(
        image_bytes=image_bytes,
        image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        width_px=width,
        height_px=height,
        scale=scale,
    )


def _render_pdf_page(
    *,
    page: Any,
    scale: float,
    max_pixels: int | None,
    resource_budget: _PdfResourceBudget,
) -> _RenderedPdfPage:
    """Render one annotation-free page PNG and charge its memory work once."""

    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("render scale must be finite and positive")
    matrix = pymupdf.Matrix(scale, scale)
    transformed_page_rect = pymupdf.Rect(page.rect) * matrix
    estimated_width = int(transformed_page_rect.irect.width)
    estimated_height = int(transformed_page_rect.irect.height)
    if estimated_width < 1 or estimated_height < 1:
        raise ValueError("rendered page has invalid dimensions")
    estimated_pixel_count = estimated_width * estimated_height
    if max_pixels is not None and estimated_pixel_count > max_pixels:
        raise ValueError("rendered page exceeds the pixel policy")
    # Charge the expected uncompressed RGBA upper bound before asking MuPDF to
    # allocate the pixmap. The page image stays in memory and is never written.
    resource_budget.consume_visual_render_bytes(estimated_pixel_count * 4)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False, annots=False)
    width = int(pixmap.width)
    height = int(pixmap.height)
    if width < 1 or height < 1:
        raise ValueError("rendered page has invalid dimensions")
    pixel_count = width * height
    if max_pixels is not None and pixel_count > max_pixels:
        raise ValueError("rendered page exceeds the pixel policy")
    if pixel_count > estimated_pixel_count:
        resource_budget.consume_visual_render_bytes(pixel_count * 4)
    image_bytes = bytes(pixmap.tobytes("png"))
    if not image_bytes:
        raise ValueError("rendered page image is empty")
    return _RenderedPdfPage(
        image_bytes=image_bytes,
        image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        width_px=width,
        height_px=height,
        scale=scale,
    )


def render_pdf_visual_page_input(
    source_path: str | PathLike[str],
    *,
    page_number: int,
    scale: float = _VISUAL_TEXT_RENDER_SCALE,
) -> VisualTextPageInput:
    """Render exactly one source PDF page for a neutral visual sidecar.

    This is the same bounded, annotation-free renderer used by the PDF
    adapter, exposed so resumable source capture does not reparse every page
    into a second SourceIR for each OCR request.
    """

    if pymupdf is None:
        raise RuntimeError("PyMuPDF is required for PDF page rendering")
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ValueError("page_number must be a positive integer")
    source_bytes = Path(source_path).read_bytes()
    if not source_bytes:
        raise ValueError("source PDF is empty")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    try:
        document = pymupdf.open(stream=source_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError("source PDF could not be opened") from exc
    try:
        if page_number > int(document.page_count):
            raise ValueError("page_number exceeds the source PDF")
        page = document.load_page(page_number - 1)
        page_rect = tuple(float(item) for item in page.rect)
        if (
            len(page_rect) != 4
            or page_rect[2] <= page_rect[0]
            or page_rect[3] <= page_rect[1]
        ):
            raise ValueError("source PDF page geometry is invalid")
        rendered = _render_pdf_page(
            page=page,
            scale=scale,
            max_pixels=None,
            resource_budget=_PdfResourceBudget(),
        )
    finally:
        document.close()
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=source_sha256,
        page_number=page_number,
        page_bbox=page_rect,
        page_image_bytes=rendered.image_bytes,
        page_image_sha256=rendered.image_sha256,
        page_image_width_px=rendered.width_px,
        page_image_height_px=rendered.height_px,
    )


def _visual_text_snapshot_from_page(
    *,
    page: Any,
    page_number: int,
    page_locator: NativeLocator | None,
    source_bytes: bytes,
    source_sha256: str,
    provider: VisualTextProviderPort | None,
    resource_budget: _PdfResourceBudget,
    rendered_page: _RenderedPdfPage | None = None,
) -> tuple[VisualTextSnapshot | None, list[SourceDiagnostic]]:
    if provider is None:
        return (
            None,
            [
                _diagnostic(
                    "pdf_visual_text_provider_not_configured",
                    (
                        f"Page {page_number} requires visual reconciliation, but optional "
                        "visual text recognition was not configured for this shadow run."
                    ),
                    native_locator=page_locator,
                )
            ],
        )
    if page_locator is None or page_locator.bbox is None:
        return (
            None,
            [
                _diagnostic(
                    "pdf_visual_page_locator_unavailable",
                    (
                        f"Page {page_number} could not be rendered for visual text "
                        "recognition because its native page geometry is unavailable."
                    ),
                )
            ],
        )

    if rendered_page is None:
        try:
            rendered_page = _render_pdf_page(
                page=page,
                scale=_VISUAL_TEXT_RENDER_SCALE,
                max_pixels=None,
                resource_budget=resource_budget,
            )
        except _PdfResourceBudgetExceeded:
            raise
        except MemoryError as exc:
            raise _PdfResourceBudgetExceeded from exc
        except Exception:
            return (
                None,
                [
                    _diagnostic(
                        "pdf_visual_page_render_failed",
                        (
                            f"Page {page_number} could not be rendered in memory for "
                            "visual text recognition."
                        ),
                        native_locator=page_locator,
                    )
                ],
            )

    page_input = VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=source_sha256,
        page_number=page_number,
        page_bbox=page_locator.bbox,
        page_image_bytes=rendered_page.image_bytes,
        page_image_sha256=rendered_page.image_sha256,
        page_image_width_px=rendered_page.width_px,
        page_image_height_px=rendered_page.height_px,
    )
    try:
        snapshot = provider.recognize_page(page_input)
    except VisualTextCaptureCancelled:
        raise
    except VisualTextProviderRejected as exc:
        return (
            None,
            [
                _diagnostic(
                    f"pdf_visual_text_{exc.code.value}",
                    (
                        f"Visual text recognition was rejected for page {page_number} "
                        f"with reason {exc.code.value}."
                    ),
                    native_locator=page_locator,
                )
            ],
        )
    except Exception:
        return (
            None,
            [
                _diagnostic(
                    "pdf_visual_text_provider_failed",
                    (
                        f"The visual text provider failed for page {page_number} "
                        "without producing trusted observations."
                    ),
                    native_locator=page_locator,
                )
            ],
        )

    if not _visual_snapshot_matches_input(snapshot, page_input, provider):
        return (
            None,
            [
                _diagnostic(
                    (
                        "pdf_visual_text_"
                        f"{VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID.value}"
                    ),
                    (
                        f"The visual text provider returned observations for page "
                        f"{page_number} with invalid source or page-image grounding."
                    ),
                    native_locator=page_locator,
                )
            ],
        )

    if not snapshot.observations:
        return (
            snapshot,
            [
                _diagnostic(
                    "pdf_visual_text_no_observations",
                    (
                        f"Visual text recognition completed for page {page_number} "
                        "without grounded text observations."
                    ),
                    native_locator=page_locator,
                )
            ],
        )
    return (
        snapshot,
        [
            _diagnostic(
                "pdf_visual_text_observations_captured",
                (
                    f"Visual text recognition captured {len(snapshot.observations)} "
                    f"grounded observation(s) for page {page_number}; native raw text "
                    "was not replaced."
                ),
                severity=SourceDiagnosticSeverity.INFO,
                native_locator=page_locator,
            )
        ],
    )


def _visual_snapshot_matches_input(
    snapshot: object,
    page_input: VisualTextPageInput,
    provider: VisualTextProviderPort,
) -> bool:
    if not isinstance(snapshot, VisualTextSnapshot):
        return False
    version_for_page = getattr(provider, "provider_version_for_page", None)
    expected_provider_version = (
        version_for_page(page_input.page_number)
        if callable(version_for_page)
        else getattr(provider, "provider_version", None)
    )
    return (
        snapshot.source_sha256 == page_input.source_sha256
        and snapshot.page_number == page_input.page_number
        and snapshot.page_bbox == page_input.page_bbox
        and snapshot.page_image_sha256 == page_input.page_image_sha256
        and snapshot.page_image_media_type == page_input.page_image_media_type
        and snapshot.page_image_width_px == page_input.page_image_width_px
        and snapshot.page_image_height_px == page_input.page_image_height_px
        and snapshot.provider_id == getattr(provider, "provider_id", None)
        and snapshot.provider_version == expected_provider_version
    )


def _visual_table_snapshot_from_native_table(
    *,
    page: Any,
    page_number: int,
    page_locator: NativeLocator | None,
    table_node: SourceNode,
    source_bytes: bytes,
    source_sha256: str,
    provider: VisualTableProviderPort | None,
    resource_budget: _PdfResourceBudget,
) -> tuple[VisualTableSnapshot | None, list[SourceDiagnostic]]:
    """Capture one exact 2x native-table crop; never render annotations."""

    locator = table_node.native_locator
    table = table_node.table_payload
    if provider is None:
        return (
            None,
            [
                _diagnostic(
                    "pdf_visual_table_provider_not_configured",
                    (
                        f"Table {locator.table_index} on page {page_number} is in the "
                        "authorized visual route, but that provider has no visual-table "
                        "adapter; no implicit fallback was attempted."
                    ),
                    severity=SourceDiagnosticSeverity.ERROR,
                    source_node_id=table_node.source_node_id,
                    native_locator=locator,
                )
            ],
        )
    if (
        page_locator is None
        or page_locator.bbox is None
        or locator.page_number != page_number
        or locator.table_index is None
        or locator.bbox is None
        or table is None
    ):
        return (
            None,
            [
                _diagnostic(
                    "pdf_visual_table_geometry_unavailable",
                    (
                        f"Table {locator.table_index} on page {page_number} lacks the "
                        "native geometry required for an exact visual crop."
                    ),
                    severity=SourceDiagnosticSeverity.ERROR,
                    source_node_id=table_node.source_node_id,
                    native_locator=locator,
                )
            ],
        )

    try:
        matrix = pymupdf.Matrix(_VISUAL_TABLE_RENDER_SCALE, _VISUAL_TABLE_RENDER_SCALE)
        clip = pymupdf.Rect(locator.bbox)
        page_rect = pymupdf.Rect(page_locator.bbox)
        if not page_rect.contains(clip):
            raise ValueError("table crop is outside its page")
        transformed = clip * matrix
        estimated_width = int(transformed.irect.width)
        estimated_height = int(transformed.irect.height)
        estimated_pixels = estimated_width * estimated_height
        if (
            estimated_width <= 10
            or estimated_height <= 10
            or estimated_pixels > 8_388_608
        ):
            raise ValueError("table crop dimensions are outside policy")
        resource_budget.consume_visual_render_bytes(estimated_pixels * 4)
        pixmap = page.get_pixmap(
            matrix=matrix,
            clip=clip,
            alpha=False,
            annots=False,
        )
        width = int(pixmap.width)
        height = int(pixmap.height)
        pixels = width * height
        if width <= 10 or height <= 10 or pixels > 8_388_608:
            raise ValueError("rendered table crop dimensions are outside policy")
        if pixels > estimated_pixels:
            resource_budget.consume_visual_render_bytes(pixels * 4)
        image_bytes = bytes(pixmap.tobytes("png"))
        if not image_bytes:
            raise ValueError("rendered table crop is empty")
        crop_input = VisualTableCropInput(
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            page_number=page_number,
            page_bbox=page_locator.bbox,
            table_source_node_id=table_node.source_node_id,
            table_id=table.table_id,
            table_index=locator.table_index,
            table_bbox=locator.bbox,
            crop_image_bytes=image_bytes,
            crop_image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            crop_image_width_px=width,
            crop_image_height_px=height,
        )
    except _PdfResourceBudgetExceeded:
        raise
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return (
            None,
            [
                _diagnostic(
                    "pdf_visual_table_crop_render_failed",
                    (
                        f"Table {locator.table_index} on page {page_number} could not be "
                        "rendered as a bounded annotation-free 2x crop."
                    ),
                    severity=SourceDiagnosticSeverity.ERROR,
                    source_node_id=table_node.source_node_id,
                    native_locator=locator,
                )
            ],
        )

    try:
        snapshot = provider.parse_table(crop_input)
    except VisualTableProviderRejected as exc:
        return (
            None,
            [
                _diagnostic(
                    f"pdf_visual_table_{exc.code.value}",
                    (
                        f"Visual table parsing was rejected for table "
                        f"{locator.table_index} on page {page_number} with reason "
                        f"{exc.code.value}."
                    ),
                    severity=SourceDiagnosticSeverity.ERROR,
                    source_node_id=table_node.source_node_id,
                    native_locator=locator,
                    evidence_sha256=crop_input.crop_image_sha256,
                )
            ],
        )
    except Exception:
        return (
            None,
            [
                _diagnostic(
                    "pdf_visual_table_provider_failed",
                    (
                        f"The visual table provider failed for table "
                        f"{locator.table_index} on page {page_number}."
                    ),
                    severity=SourceDiagnosticSeverity.ERROR,
                    source_node_id=table_node.source_node_id,
                    native_locator=locator,
                    evidence_sha256=crop_input.crop_image_sha256,
                )
            ],
        )
    if not _visual_table_snapshot_matches_input(snapshot, crop_input, provider):
        return (
            None,
            [
                _diagnostic(
                    (
                        "pdf_visual_table_"
                        f"{VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID.value}"
                    ),
                    (
                        f"The visual table provider returned invalid source, crop, table, "
                        f"or provider grounding for table {locator.table_index} on page "
                        f"{page_number}."
                    ),
                    severity=SourceDiagnosticSeverity.ERROR,
                    source_node_id=table_node.source_node_id,
                    native_locator=locator,
                    evidence_sha256=crop_input.crop_image_sha256,
                )
            ],
        )
    return snapshot, []


def _visual_table_snapshot_matches_input(
    snapshot: object,
    crop_input: VisualTableCropInput,
    provider: VisualTableProviderPort,
) -> bool:
    return isinstance(snapshot, VisualTableSnapshot) and (
        snapshot.source_sha256 == crop_input.source_sha256
        and snapshot.page_number == crop_input.page_number
        and snapshot.page_bbox == crop_input.page_bbox
        and snapshot.table_source_node_id == crop_input.table_source_node_id
        and snapshot.table_id == crop_input.table_id
        and snapshot.table_index == crop_input.table_index
        and snapshot.table_bbox == crop_input.table_bbox
        and snapshot.crop_image_sha256 == crop_input.crop_image_sha256
        and snapshot.crop_image_media_type == crop_input.crop_image_media_type
        and snapshot.crop_image_width_px == crop_input.crop_image_width_px
        and snapshot.crop_image_height_px == crop_input.crop_image_height_px
        and snapshot.render_scale == crop_input.render_scale
        and snapshot.annotations_included == crop_input.annotations_included
        and snapshot.provider_id == getattr(provider, "provider_id", None)
        and snapshot.provider_version == getattr(provider, "provider_version", None)
    )


def _table_nodes_from_page(
    *,
    page: Any,
    raw_page: Any,
    page_rotation_matrix: object,
    page_number: int,
    page_locator: NativeLocator | None,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    first_source_order: int,
    resource_budget: _PdfResourceBudget,
) -> tuple[list[SourceNode], list[SourceDiagnostic], bool]:
    """Return deterministic structural table candidates for one open page.

    Table detection is deliberately additive: callers have already appended
    raw text/image blocks, and this helper neither removes nor rewrites them.
    PyMuPDF cannot reliably prove semantic headers or merged regions for every
    PDF, so those fields remain empty instead of asserting invented structure.
    """

    try:
        find_tables = getattr(page, "find_tables", None)
    except Exception as exc:
        return (
            [],
            [
                _diagnostic(
                    "pdf_table_detection_failed",
                    (
                        f"Native table detection could not be initialized on page {page_number} "
                        f"({type(exc).__name__})."
                    ),
                    native_locator=page_locator,
                )
            ],
            True,
        )
    if not callable(find_tables):
        return (
            [],
            [
                _diagnostic(
                    "pdf_table_detector_unavailable",
                    (
                        f"Page {page_number} could not be inspected for native table "
                        "candidates because this PyMuPDF runtime has no table detector."
                    ),
                    native_locator=page_locator,
                )
            ],
            True,
        )

    try:
        finder = find_tables()
    except Exception as exc:
        return (
            [],
            [
                _diagnostic(
                    "pdf_table_detection_failed",
                    (
                        f"Native table detection failed on page {page_number} "
                        f"({type(exc).__name__})."
                    ),
                    native_locator=page_locator,
                )
            ],
            True,
        )

    try:
        default_candidates = getattr(finder, "tables", None)
    except Exception as exc:
        return (
            [],
            [
                _diagnostic(
                    "pdf_table_candidates_unavailable",
                    (
                        f"Page {page_number} did not expose native table candidates "
                        f"({type(exc).__name__})."
                    ),
                    native_locator=page_locator,
                )
            ],
            True,
        )
    if not isinstance(default_candidates, (list, tuple)):
        return (
            [],
            [
                _diagnostic(
                    "pdf_table_candidates_unavailable",
                    f"Page {page_number} did not expose a native table candidate sequence.",
                    native_locator=page_locator,
                )
            ],
            True,
        )

    raw_strict_candidates = _lines_strict_table_candidates(find_tables)
    resource_budget.consume_table_candidates(
        len(default_candidates) + len(raw_strict_candidates)
    )
    page_bboxlog = _safe_page_bboxlog(page)
    prepared_default_candidates = tuple(
        _prepare_table_candidate(candidate, page_bboxlog=page_bboxlog)
        for candidate in default_candidates
    )
    prepared_strict_candidates = tuple(
        _prepare_table_candidate(candidate, page_bboxlog=page_bboxlog)
        for candidate in raw_strict_candidates
    )
    (
        prepared_default_candidates,
        default_decomposition_diagnostics,
    ) = _decompose_mixed_layout_table_candidates(
        candidates=prepared_default_candidates,
        page_number=page_number,
        page_bbox=page_locator.bbox if page_locator is not None else None,
        source_sha256=source_sha256,
    )
    (
        prepared_strict_candidates,
        strict_decomposition_diagnostics,
    ) = _decompose_mixed_layout_table_candidates(
        candidates=prepared_strict_candidates,
        page_number=page_number,
        page_bbox=page_locator.bbox if page_locator is not None else None,
        source_sha256=source_sha256,
    )
    prepared_default_candidates, suppression_diagnostics = (
        _suppress_default_table_candidates(
            default_candidates=prepared_default_candidates,
            raw_strict_candidates=raw_strict_candidates,
            raw_page=raw_page,
            page_rotation_matrix=page_rotation_matrix,
            page_number=page_number,
            page_bbox=page_locator.bbox if page_locator is not None else None,
            source_sha256=source_sha256,
        )
    )
    candidates, strict_added, strict_replaced = _select_table_candidates(
        default_candidates=prepared_default_candidates,
        strict_candidates=prepared_strict_candidates,
        page_bbox=page_locator.bbox if page_locator is not None else None,
        page_bboxlog=page_bboxlog,
    )
    selection_diagnostics: list[SourceDiagnostic] = []
    decomposition_diagnostic_keys: set[tuple[str, str | None]] = set()
    for diagnostic in (
        *default_decomposition_diagnostics,
        *strict_decomposition_diagnostics,
    ):
        key = (diagnostic.code, diagnostic.evidence_sha256)
        if key in decomposition_diagnostic_keys:
            continue
        decomposition_diagnostic_keys.add(key)
        selection_diagnostics.append(diagnostic)
    selection_diagnostics.extend(suppression_diagnostics)
    if strict_added or strict_replaced:
        selection_diagnostics.append(
            _diagnostic(
                "pdf_lines_strict_table_enhancement_applied",
                (
                    f"Page {page_number} selected {strict_added} additional and "
                    f"{strict_replaced} geometry-proven ruled-grid table candidate(s) "
                    "from the lines_strict detector."
                ),
                severity=SourceDiagnosticSeverity.INFO,
                native_locator=page_locator,
            )
        )
    if not candidates:
        # Both detectors ran successfully.  A page with no controlled table
        # candidate is a valid observation, not evidence of partial extraction.
        return [], selection_diagnostics, False

    ordered_candidates = sorted(
        enumerate(candidates),
        key=lambda item: _table_candidate_sort_key(item[1], item[0]),
    )
    nodes: list[SourceNode] = []
    diagnostics: list[SourceDiagnostic] = list(selection_diagnostics)
    partial = False
    cell_text_index = _PdfTableCellTextIndex(raw_page, page_rotation_matrix)
    for table_index, (_, candidate) in enumerate(ordered_candidates):
        fallback_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=table_index,
        )
        try:
            node, local_diagnostics, candidate_partial = _table_node_from_candidate(
                candidate=candidate,
                raw_page=raw_page,
                page_rotation_matrix=page_rotation_matrix,
                page_number=page_number,
                table_index=table_index,
                source_sha256=source_sha256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                source_order=first_source_order + len(nodes),
                resource_budget=resource_budget,
                cell_text_index=cell_text_index,
            )
        except _PdfResourceBudgetExceeded:
            raise
        except Exception as exc:
            partial = True
            diagnostics.append(
                _diagnostic(
                    "pdf_table_candidate_failed",
                    (
                        f"Native table candidate {table_index} on page {page_number} could not "
                        f"be represented ({type(exc).__name__})."
                    ),
                    native_locator=fallback_locator,
                )
            )
            continue
        if node is None:
            partial = True
            diagnostics.extend(local_diagnostics)
            continue
        nodes.append(node)
        diagnostics.extend(local_diagnostics)
        partial = partial or candidate_partial
    return nodes, diagnostics, partial


def _lines_strict_table_candidates(find_tables: Any) -> tuple[Any, ...]:
    """Return optional ruled-grid candidates without weakening the default path.

    ``lines_strict`` is a controlled enhancement in the pinned PyMuPDF
    runtime.  Older compatible runtimes or page doubles may not accept the
    strategy keyword; in that case the already-successful default detector
    remains authoritative.
    """

    try:
        finder = find_tables(strategy="lines_strict")
    except (TypeError, ValueError):
        return ()
    except Exception:
        return ()
    try:
        candidates = getattr(finder, "tables", None)
    except Exception:
        return ()
    return tuple(candidates) if isinstance(candidates, (list, tuple)) else ()


def _safe_page_bboxlog(page: Any) -> tuple[Any, ...]:
    try:
        get_bboxlog = getattr(page, "get_bboxlog", None)
    except Exception:
        return ()
    if not callable(get_bboxlog):
        return ()
    try:
        entries = get_bboxlog()
    except Exception:
        return ()
    return tuple(entries) if isinstance(entries, (list, tuple)) else ()


def _decompose_mixed_layout_table_candidates(
    *,
    candidates: tuple[Any, ...],
    page_number: int,
    page_bbox: tuple[float, float, float, float] | None,
    source_sha256: str,
) -> tuple[tuple[Any, ...], tuple[SourceDiagnostic, ...]]:
    """Replace one proven page-layout grid with its only dense ruled child.

    Some authoring tools draw long page separators that cause the native table
    detector to wrap paragraphs and a real table in one page-sized grid.  This
    disposition is deliberately narrow: the parent must be geometry-proven,
    mostly single-cell paragraph rows, contain one giant layout row, expose
    exactly one dense multi-column run, and coexist with a separately detected
    contained table. Any missing or ambiguous fact keeps the original
    candidate.
    """

    if page_bbox is None or _bbox_area(page_bbox) <= 0.0:
        return candidates, ()
    retained: list[Any] = []
    diagnostics: list[SourceDiagnostic] = []
    sibling_bboxes = tuple(_candidate_bbox(candidate) for candidate in candidates)
    for candidate_index, candidate in enumerate(candidates):
        decomposition = _mixed_layout_table_dense_child(
            candidate=candidate,
            candidate_index=candidate_index,
            sibling_bboxes=sibling_bboxes,
            page_bbox=page_bbox,
        )
        if decomposition is None:
            retained.append(candidate)
            continue
        child, proof = decomposition
        retained.append(child)
        evidence_payload = {
            "schema": "pdf-mixed-layout-table-decomposition-proof/v1",
            "source_sha256": source_sha256,
            "page_number": page_number,
            "candidate_native_index": candidate_index,
            "parent_bbox": _rounded_bbox(proof["parent_bbox"]),
            "parent_dimensions": list(proof["parent_dimensions"]),
            "dense_row_indexes": list(proof["dense_row_indexes"]),
            "kept_column_indexes": list(proof["kept_column_indexes"]),
            "child_bbox": _rounded_bbox(proof["child_bbox"]),
            "contained_sibling_bboxes": [
                _rounded_bbox(bbox) for bbox in proof["contained_sibling_bboxes"]
            ],
            "disposition": "REPLACE_LAYOUT_PARENT_WITH_DENSE_TABLE_CHILD",
        }
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                evidence_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        diagnostics.append(
            _diagnostic(
                "pdf_mixed_layout_table_candidate_decomposed",
                (
                    f"Page {page_number} replaced one geometry-proven "
                    f"{proof['parent_dimensions'][0]}x"
                    f"{proof['parent_dimensions'][1]} page-layout TABLE "
                    "candidate with its unique dense ruled-grid child; native "
                    "paragraph/image facts and separately detected contained "
                    "tables were preserved."
                ),
                severity=SourceDiagnosticSeverity.INFO,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    bbox=proof["parent_bbox"],
                ),
                evidence_sha256=evidence_sha256,
            )
        )
    return tuple(retained), tuple(diagnostics)


def _mixed_layout_table_dense_child(
    *,
    candidate: Any,
    candidate_index: int,
    sibling_bboxes: tuple[tuple[float, float, float, float] | None, ...],
    page_bbox: tuple[float, float, float, float],
) -> tuple[_PreparedTableCandidate, dict[str, Any]] | None:
    parent_bbox = _candidate_bbox(candidate)
    dimensions = _candidate_dimensions(candidate)
    page_area = _bbox_area(page_bbox)
    if parent_bbox is None or dimensions is None or page_area <= 0.0:
        return None
    row_count, column_count = dimensions
    parent_area = _bbox_area(parent_bbox)
    parent_height = parent_bbox[3] - parent_bbox[1]
    if (
        row_count < _MIXED_LAYOUT_TABLE_MIN_ROWS
        or column_count < _MIXED_LAYOUT_TABLE_MIN_COLUMNS
        or row_count * column_count > _MAX_PDF_TABLE_CELLS
        or parent_area <= 0.0
        or parent_height <= 0.0
        or parent_area / page_area < _MIXED_LAYOUT_TABLE_MIN_PAGE_AREA_RATIO
        or parent_area / page_area >= _MIXED_LAYOUT_TABLE_MAX_PAGE_AREA_RATIO
    ):
        return None

    geometry_observation = _candidate_geometry_observation(candidate)
    if geometry_observation is None or geometry_observation[0] is None:
        return None
    try:
        extracted_rows = candidate.extract()
        native_rows = candidate.rows
    except Exception:
        return None
    if (
        not _complete_table_text_matrix(
            extracted_rows,
            row_count=row_count,
            column_count=column_count,
        )
        or not isinstance(native_rows, (list, tuple))
        or len(native_rows) != row_count
    ):
        return None

    row_nonempty_counts = tuple(
        sum(bool((value or "").strip()) for value in row) for row in extracted_rows
    )
    sparse_row_count = sum(count <= 1 for count in row_nonempty_counts)
    if sparse_row_count < math.ceil(
        row_count * _MIXED_LAYOUT_TABLE_MIN_SPARSE_ROW_RATIO
    ):
        return None

    row_bboxes: list[tuple[float, float, float, float]] = []
    for native_row in native_rows:
        bbox = _native_bbox(getattr(native_row, "bbox", None))
        if bbox is None:
            return None
        row_bboxes.append(bbox)
    if (
        max(bbox[3] - bbox[1] for bbox in row_bboxes)
        < parent_height * _MIXED_LAYOUT_TABLE_MIN_GIANT_ROW_HEIGHT_RATIO
    ):
        return None

    minimum_dense_columns = max(
        3,
        math.ceil(column_count * _MIXED_LAYOUT_TABLE_MIN_DENSE_COLUMN_RATIO),
    )
    dense_runs: list[tuple[int, ...]] = []
    current_run: list[int] = []
    for row_index, nonempty_count in enumerate(row_nonempty_counts):
        if nonempty_count >= minimum_dense_columns:
            current_run.append(row_index)
            continue
        if current_run:
            dense_runs.append(tuple(current_run))
            current_run = []
    if current_run:
        dense_runs.append(tuple(current_run))
    if len(dense_runs) != 1 or len(dense_runs[0]) < _MIXED_LAYOUT_TABLE_MIN_DENSE_ROWS:
        return None
    dense_rows = dense_runs[0]

    contained_sibling_bboxes: list[tuple[float, float, float, float]] = []
    for sibling_index, sibling_bbox in enumerate(sibling_bboxes):
        if sibling_index == candidate_index or sibling_bbox is None:
            continue
        sibling_area = _bbox_area(sibling_bbox)
        intersection = _positive_bbox_intersection(sibling_bbox, parent_bbox)
        if (
            sibling_area > 0.0
            and sibling_area / parent_area
            <= _MIXED_LAYOUT_TABLE_MAX_NESTED_CHILD_AREA_RATIO
            and intersection is not None
            and _bbox_area(intersection) / sibling_area >= 0.98
        ):
            contained_sibling_bboxes.append(sibling_bbox)
    if not contained_sibling_bboxes:
        return None

    populated_columns = tuple(
        any(
            bool((extracted_rows[row_index][column_index] or "").strip())
            for row_index in dense_rows
        )
        for column_index in range(column_count)
    )
    first_column = next(
        (index for index, populated in enumerate(populated_columns) if populated),
        None,
    )
    last_column = next(
        (
            index
            for index in range(column_count - 1, -1, -1)
            if populated_columns[index]
        ),
        None,
    )
    if (
        first_column is None
        or last_column is None
        or (first_column == 0 and last_column == column_count - 1)
        or last_column - first_column + 1 < 3
        or not all(populated_columns[first_column : last_column + 1])
    ):
        return None

    child_rows: list[_PreparedTableRow] = []
    child_matrix: list[tuple[str, ...]] = []
    for row_index in dense_rows:
        native_cells = getattr(native_rows[row_index], "cells", None)
        if (
            not isinstance(native_cells, (list, tuple))
            or len(native_cells) != column_count
        ):
            return None
        selected_cells = tuple(
            _native_bbox(native_cells[column_index])
            for column_index in range(first_column, last_column + 1)
        )
        if any(cell is None for cell in selected_cells):
            return None
        material_cells = tuple(cell for cell in selected_cells if cell is not None)
        row_bbox = _bbox_union(material_cells)
        if row_bbox is None:
            return None
        child_rows.append(_PreparedTableRow(bbox=row_bbox, cells=selected_cells))
        child_matrix.append(
            tuple(
                extracted_rows[row_index][column_index] or ""
                for column_index in range(first_column, last_column + 1)
            )
        )
    child_bbox = _bbox_union(tuple(row.bbox for row in child_rows))
    if child_bbox is None:
        return None
    child = _PreparedTableCandidate(
        bbox=child_bbox,
        row_count=len(child_rows),
        col_count=last_column - first_column + 1,
        rows=tuple(child_rows),
        matrix=tuple(child_matrix),
    )
    child_observation = _candidate_geometry_observation(child)
    if child_observation is None or child_observation[0] is None:
        return None
    return child, {
        "parent_bbox": parent_bbox,
        "parent_dimensions": dimensions,
        "dense_row_indexes": dense_rows,
        "kept_column_indexes": tuple(range(first_column, last_column + 1)),
        "child_bbox": child_bbox,
        "contained_sibling_bboxes": tuple(
            sorted(
                contained_sibling_bboxes,
                key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
            )
        ),
    }


def _prepare_table_candidate(
    candidate: Any,
    *,
    page_bboxlog: tuple[Any, ...],
) -> Any:
    """Normalize only geometry-proven ruled-grid artifacts.

    Native detector candidates can contain a caption row or a spurious
    boundary contributed by vector glyph strokes.  Preparation removes a
    caption only when its full-width row has a single visual text cluster that
    does not occupy every grid column and native paths do not prove that the
    row is enclosed by complete horizontal rules.  It merges an unsupported
    boundary only when both adjacent native-text columns are empty, which
    avoids treating a missing Unicode map as proof that an actual column is
    empty.
    """

    candidate_bbox = _candidate_bbox(candidate)
    dimensions = _candidate_dimensions(candidate)
    if candidate_bbox is None or dimensions is None:
        return candidate
    row_count, column_count = dimensions
    try:
        extractor = getattr(candidate, "extract", None)
        extracted_rows = extractor() if callable(extractor) else None
        native_rows = getattr(candidate, "rows", None)
    except Exception:
        return candidate
    if (
        not isinstance(extracted_rows, (list, tuple))
        or len(extracted_rows) != row_count
        or not isinstance(native_rows, (list, tuple))
        or len(native_rows) != row_count
    ):
        return candidate

    matrix: list[list[str]] = []
    row_bboxes: list[tuple[float, float, float, float]] = []
    native_cell_rows: list[tuple[tuple[float, float, float, float] | None, ...]] = []
    for row_index in range(row_count):
        extracted_row = extracted_rows[row_index]
        native_row = native_rows[row_index]
        if (
            not isinstance(extracted_row, (list, tuple))
            or len(extracted_row) != column_count
        ):
            return candidate
        normalized_row: list[str] = []
        for value in extracted_row:
            if value is None:
                normalized_row.append("")
            elif isinstance(value, str):
                normalized_row.append(value)
            else:
                return candidate
        row_bbox = _native_bbox(getattr(native_row, "bbox", None))
        native_cells = getattr(native_row, "cells", None)
        if row_bbox is None or not isinstance(native_cells, (list, tuple)):
            return candidate
        normalized_cells = tuple(
            _native_bbox(native_cells[column_index])
            if column_index < len(native_cells)
            else None
            for column_index in range(column_count)
        )
        matrix.append(normalized_row)
        row_bboxes.append(row_bbox)
        native_cell_rows.append(normalized_cells)

    grid_row = max(
        native_cell_rows,
        key=lambda cells: sum(cell is not None for cell in cells),
    )
    grid_cells = tuple(cell for cell in grid_row if cell is not None)
    if len(grid_cells) != column_count:
        return candidate
    original_boundaries = [grid_cells[0][0]] + [cell[2] for cell in grid_cells]
    if any(
        right <= left
        for left, right in zip(original_boundaries, original_boundaries[1:])
    ):
        return candidate

    column_nonempty_counts = [
        sum(bool(row[column_index].strip()) for row in matrix)
        for column_index in range(column_count)
    ]
    median_row_height = _median_positive(
        row_bbox[3] - row_bbox[1] for row_bbox in row_bboxes
    )
    groups: list[list[int]] = [[0]]
    for boundary_index, boundary in enumerate(original_boundaries[1:-1]):
        unsupported_empty_boundary = (
            column_nonempty_counts[boundary_index] == 0
            and column_nonempty_counts[boundary_index + 1] == 0
            and not _vertical_boundary_has_rule_support(
                boundary,
                table_bbox=candidate_bbox,
                minimum_height=max(6.0, min(12.0, median_row_height * 0.5)),
                page_bboxlog=page_bboxlog,
            )
        )
        if unsupported_empty_boundary:
            groups[-1].append(boundary_index + 1)
        else:
            groups.append([boundary_index + 1])

    column_intervals = tuple(
        (
            original_boundaries[group[0]],
            original_boundaries[group[-1] + 1],
        )
        for group in groups
    )
    prepared_matrix = [
        [
            "\n".join(
                value for column_index in group if (value := row[column_index]) != ""
            )
            for group in groups
        ]
        for row in matrix
    ]

    first_row = 0
    last_row = row_count
    if row_count >= 3 and _row_has_full_width_cell(
        native_cell_rows[0],
        candidate_bbox=candidate_bbox,
    ):
        first_row_height = row_bboxes[0][3] - row_bboxes[0][1]
        first_nonempty_count = sum(bool(value.strip()) for value in prepared_matrix[0])
        occupied_columns, _ = _row_vector_glyph_coverage(
            row_bboxes[0],
            column_intervals=column_intervals,
            page_bboxlog=page_bboxlog,
        )
        ruled_caption_before_giant_panel = (
            row_count >= 4
            and _row_has_full_width_cell(
                native_cell_rows[1],
                candidate_bbox=candidate_bbox,
            )
            and row_bboxes[1][3] - row_bboxes[1][1] >= max(48.0, first_row_height * 4.0)
            and any(
                sum(cell is not None for cell in cells) == column_count
                for cells in native_cell_rows[2:]
            )
        )
        if (
            first_row_height <= max(48.0, median_row_height * 2.0)
            and first_nonempty_count <= 1
            and 0 < occupied_columns < len(column_intervals)
            and (
                ruled_caption_before_giant_panel
                or not _row_has_complete_horizontal_rule_support(
                    row_bboxes[0],
                    table_bbox=candidate_bbox,
                    page_bboxlog=page_bboxlog,
                )
            )
        ):
            first_row = 1

    if last_row - first_row >= 3 and not any(
        value.strip() for value in prepared_matrix[last_row - 1]
    ):
        prior_heights = (
            row_bboxes[index][3] - row_bboxes[index][1]
            for index in range(first_row, last_row - 1)
        )
        prior_median_height = _median_positive(prior_heights)
        trailing_height = row_bboxes[last_row - 1][3] - row_bboxes[last_row - 1][1]
        _, trailing_glyph_count = _row_vector_glyph_coverage(
            row_bboxes[last_row - 1],
            column_intervals=column_intervals,
            page_bboxlog=page_bboxlog,
        )
        if trailing_height <= prior_median_height * 0.5 and trailing_glyph_count <= 1:
            last_row -= 1

    if first_row == 0 and last_row == row_count and len(groups) == column_count:
        return candidate

    selected_row_bboxes = row_bboxes[first_row:last_row]
    selected_matrix = prepared_matrix[first_row:last_row]
    if not selected_row_bboxes or not column_intervals:
        return candidate
    if len(groups) == column_count:
        # Caption/trailing-row cleanup must not erase native merge topology.
        # PyMuPDF represents a merged anchor with its full rectangle and the
        # covered logical slots as ``None``. Rebuilding every selected row
        # from column intervals would silently turn those covered slots into
        # independent cells.
        prepared_rows = tuple(
            _PreparedTableRow(
                bbox=row_bbox,
                cells=native_cells,
            )
            for row_bbox, native_cells in zip(
                selected_row_bboxes,
                native_cell_rows[first_row:last_row],
                strict=True,
            )
        )
    else:
        prepared_rows = tuple(
            _PreparedTableRow(
                bbox=row_bbox,
                cells=tuple(
                    (x0, row_bbox[1], x1, row_bbox[3]) for x0, x1 in column_intervals
                ),
            )
            for row_bbox in selected_row_bboxes
        )
    return _PreparedTableCandidate(
        bbox=(
            candidate_bbox[0],
            selected_row_bboxes[0][1],
            candidate_bbox[2],
            selected_row_bboxes[-1][3],
        ),
        row_count=len(selected_row_bboxes),
        col_count=len(column_intervals),
        rows=prepared_rows,
        matrix=tuple(tuple(row) for row in selected_matrix),
    )


def _row_has_full_width_cell(
    cells: tuple[tuple[float, float, float, float] | None, ...],
    *,
    candidate_bbox: tuple[float, float, float, float],
) -> bool:
    present = tuple(cell for cell in cells if cell is not None)
    if len(present) != 1:
        return False
    table_width = candidate_bbox[2] - candidate_bbox[0]
    return table_width > 0.0 and (present[0][2] - present[0][0]) / table_width >= 0.98


def _row_has_complete_horizontal_rule_support(
    row_bbox: tuple[float, float, float, float],
    *,
    table_bbox: tuple[float, float, float, float],
    page_bboxlog: tuple[Any, ...],
) -> bool:
    return all(
        _horizontal_boundary_rule_coverage(
            boundary_y,
            table_bbox=table_bbox,
            page_bboxlog=page_bboxlog,
        )
        >= _TABLE_RULE_MIN_WIDTH_COVERAGE
        for boundary_y in (row_bbox[1], row_bbox[3])
    )


def _horizontal_boundary_rule_coverage(
    boundary_y: float,
    *,
    table_bbox: tuple[float, float, float, float],
    page_bboxlog: tuple[Any, ...],
) -> float:
    """Return native thin-path coverage across one horizontal table boundary."""

    table_width = table_bbox[2] - table_bbox[0]
    if table_width <= 0.0:
        return 0.0
    intervals: list[tuple[float, float]] = []
    for entry in page_bboxlog:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) < 2
            or entry[0] not in {"fill-path", "stroke-path"}
        ):
            continue
        bbox = _native_bbox(entry[1])
        if bbox is None:
            continue
        height = bbox[3] - bbox[1]
        center_y = (bbox[1] + bbox[3]) / 2.0
        if (
            height <= 0.0
            or height > _TABLE_HORIZONTAL_RULE_MAX_THICKNESS_POINTS
            or abs(center_y - boundary_y) > _TABLE_RULE_BOUNDARY_TOLERANCE_POINTS
        ):
            continue
        clipped_left = max(table_bbox[0], bbox[0])
        clipped_right = min(table_bbox[2], bbox[2])
        if clipped_right > clipped_left:
            intervals.append((clipped_left, clipped_right))
    if not intervals:
        return 0.0

    merged: list[tuple[float, float]] = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1] + _TABLE_RULE_INTERVAL_GAP_TOLERANCE_POINTS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    covered_width = sum(right - left for left, right in merged)
    return min(1.0, covered_width / table_width)


def _vertical_boundary_has_rule_support(
    boundary_x: float,
    *,
    table_bbox: tuple[float, float, float, float],
    minimum_height: float,
    page_bboxlog: tuple[Any, ...],
) -> bool:
    for entry in page_bboxlog:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) < 2
            or entry[0] not in {"fill-path", "stroke-path"}
        ):
            continue
        bbox = _native_bbox(entry[1])
        if bbox is None:
            continue
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        center_x = (bbox[0] + bbox[2]) / 2.0
        if (
            0.0 < width <= 3.0
            and height >= minimum_height
            and abs(center_x - boundary_x) <= 2.0
            and bbox[3] >= table_bbox[1]
            and bbox[1] <= table_bbox[3]
        ):
            return True
    return False


def _row_vector_glyph_coverage(
    row_bbox: tuple[float, float, float, float],
    *,
    column_intervals: tuple[tuple[float, float], ...],
    page_bboxlog: tuple[Any, ...],
) -> tuple[int, int]:
    occupied_columns: set[int] = set()
    glyph_count = 0
    for entry in page_bboxlog:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) < 2
            or entry[0] not in {"fill-path", "stroke-path", "fill-text", "stroke-text"}
        ):
            continue
        bbox = _native_bbox(entry[1])
        if bbox is None:
            continue
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width < 0.5 or height < 0.5 or width > 18.0 or height > 18.0:
            continue
        if max(width / height, height / width) > 4.0:
            continue
        center_x = (bbox[0] + bbox[2]) / 2.0
        center_y = (bbox[1] + bbox[3]) / 2.0
        if not (
            row_bbox[0] <= center_x <= row_bbox[2]
            and row_bbox[1] <= center_y <= row_bbox[3]
        ):
            continue
        glyph_count += 1
        for column_index, (x0, x1) in enumerate(column_intervals):
            if x0 <= center_x <= x1:
                occupied_columns.add(column_index)
                break
    return len(occupied_columns), glyph_count


def _median_positive(values: Any) -> float:
    ordered = sorted(float(value) for value in values if float(value) > 0.0)
    if not ordered:
        return 0.0
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _suppress_default_table_candidates(
    *,
    default_candidates: tuple[Any, ...],
    raw_strict_candidates: tuple[Any, ...],
    raw_page: Any,
    page_rotation_matrix: object,
    page_number: int,
    page_bbox: tuple[float, float, float, float] | None,
    source_sha256: str,
) -> tuple[tuple[Any, ...], tuple[SourceDiagnostic, ...]]:
    """Drop three narrowly proven default-detector layout artifacts.

    Raw text and watermark blocks remain authoritative SourceIR facts.  This
    only suppresses a duplicate TABLE view when page geometry, detector
    provenance, block shape, and a 90% native-character multiset comparison
    all agree.  Any malformed or incomplete observation keeps the candidate.
    """

    if page_bbox is None or _bbox_area(page_bbox) <= 0.0:
        return default_candidates, ()
    retained: list[Any] = []
    diagnostics: list[SourceDiagnostic] = []
    for candidate_native_index, candidate in enumerate(default_candidates):
        if not _default_table_candidate_is_controlled(candidate):
            retained.append(candidate)
            continue
        suppression = _default_table_candidate_suppression_observation(
            candidate=candidate,
            candidate_native_index=candidate_native_index,
            raw_strict_candidates=raw_strict_candidates,
            raw_page=raw_page,
            page_rotation_matrix=page_rotation_matrix,
            page_bbox=page_bbox,
        )
        if suppression is None:
            retained.append(candidate)
            continue
        rule_id, evidence_payload, message = suppression
        evidence_payload.update(
            {
                "schema": "pdf-table-suppression-evidence/v1",
                "rule_id": rule_id,
                "rule_version": 1,
                "source_sha256": source_sha256,
                "page_number": page_number,
                "candidate_native_index": candidate_native_index,
                "disposition": "DROP_TABLE_KEEP_NATIVE_TEXT",
            }
        )
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                evidence_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        candidate_bbox = _candidate_bbox(candidate)
        diagnostics.append(
            _diagnostic(
                "pdf_default_table_candidate_suppressed",
                message,
                severity=SourceDiagnosticSeverity.INFO,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    bbox=candidate_bbox,
                ),
                evidence_sha256=evidence_sha256,
            )
        )
    return tuple(retained), tuple(diagnostics)


def _default_table_candidate_suppression_observation(
    *,
    candidate: Any,
    candidate_native_index: int,
    raw_strict_candidates: tuple[Any, ...],
    raw_page: Any,
    page_rotation_matrix: object,
    page_bbox: tuple[float, float, float, float],
) -> tuple[str, dict[str, Any], str] | None:
    candidate_bbox = _candidate_bbox(candidate)
    dimensions = _candidate_dimensions(candidate)
    candidate_text = _table_suppression_candidate_text(candidate)
    if candidate_bbox is None or dimensions is None or candidate_text is None:
        return None
    candidate_area = _bbox_area(candidate_bbox)
    page_area = _bbox_area(page_bbox)
    candidate_width = candidate_bbox[2] - candidate_bbox[0]
    if candidate_area <= 0.0 or page_area <= 0.0 or candidate_width <= 0.0:
        return None
    row_count, column_count = dimensions
    normalized_candidate_text, nonempty_cell_count = candidate_text
    candidate_counter = Counter(normalized_candidate_text)
    candidate_character_count = candidate_counter.total()

    ordinary_blocks, watermark_blocks = _table_suppression_text_blocks(
        raw_page=raw_page,
        page_rotation_matrix=page_rotation_matrix,
        candidate_bbox=candidate_bbox,
    )
    ordinary_counter = Counter(
        "".join(block.normalized_text for block in ordinary_blocks)
    )
    watermark_counter = Counter(
        "".join(block.normalized_text for block in watermark_blocks)
    )
    ordinary_matched = (candidate_counter & ordinary_counter).total()
    watermark_matched = (candidate_counter & watermark_counter).total()
    coverage_denominator = max(1, candidate_character_count)

    strict_overlaps = _table_suppression_strict_overlaps(
        candidate_bbox=candidate_bbox,
        raw_strict_candidates=raw_strict_candidates,
    )
    area_ratio = candidate_area / page_area
    rule_id: str | None = None
    evidence_blocks: tuple[_TableSuppressionTextBlock, ...] = ()
    matched_character_count = 0
    thresholds: dict[str, Any] = {}
    message = ""

    if (
        area_ratio > _NEAR_FULL_PSEUDO_TABLE_MIN_PAGE_AREA_RATIO
        and not raw_strict_candidates
        and len(ordinary_blocks) >= _NEAR_FULL_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS
        and candidate_character_count >= _NEAR_FULL_PSEUDO_TABLE_MIN_CHARACTER_COUNT
        and ordinary_matched * 100
        >= coverage_denominator * _TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT
    ):
        rule_id = "near_full_unruled_paragraph_layout"
        evidence_blocks = ordinary_blocks
        matched_character_count = ordinary_matched
        thresholds = {
            "area_ratio": {
                "operator": ">",
                "value": _NEAR_FULL_PSEUDO_TABLE_MIN_PAGE_AREA_RATIO,
            },
            "raw_strict_count": {"operator": "=", "value": 0},
            "ordinary_block_count": {
                "operator": ">=",
                "value": _NEAR_FULL_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS,
            },
            "candidate_character_count": {
                "operator": ">=",
                "value": _NEAR_FULL_PSEUDO_TABLE_MIN_CHARACTER_COUNT,
            },
            "character_coverage_percent": {
                "operator": ">=",
                "value": _TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT,
            },
        }
        message = (
            "Rule near_full_unruled_paragraph_layout suppressed 1 TABLE "
            f"candidate: area {candidate_area:.6f}/{page_area:.6f} > "
            f"{_NEAR_FULL_PSEUDO_TABLE_MIN_PAGE_AREA_RATIO:.2f}; raw strict "
            f"count {len(raw_strict_candidates)} = 0; ordinary block count "
            f"{len(ordinary_blocks)} >= "
            f"{_NEAR_FULL_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS}; normalized "
            f"character count {candidate_character_count} >= "
            f"{_NEAR_FULL_PSEUDO_TABLE_MIN_CHARACTER_COUNT}; matched "
            f"characters {ordinary_matched}/{coverage_denominator} >= "
            f"{_TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT}%."
        )
    elif (
        area_ratio <= _WATERMARK_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO
        and row_count <= _WATERMARK_PSEUDO_TABLE_MAX_ROWS
        and column_count <= _WATERMARK_PSEUDO_TABLE_MAX_COLUMNS
        and nonempty_cell_count <= _WATERMARK_PSEUDO_TABLE_MAX_NONEMPTY_CELLS
        and candidate_character_count <= _WATERMARK_PSEUDO_TABLE_MAX_CHARACTER_COUNT
        and not strict_overlaps
        and not ordinary_blocks
        and watermark_blocks
        and watermark_matched * 100
        >= coverage_denominator * _TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT
    ):
        rule_id = "watermark_only_tiny_layout"
        evidence_blocks = watermark_blocks
        matched_character_count = watermark_matched
        thresholds = {
            "area_ratio": {
                "operator": "<=",
                "value": _WATERMARK_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO,
            },
            "row_count": {
                "operator": "<=",
                "value": _WATERMARK_PSEUDO_TABLE_MAX_ROWS,
            },
            "column_count": {
                "operator": "<=",
                "value": _WATERMARK_PSEUDO_TABLE_MAX_COLUMNS,
            },
            "nonempty_cell_count": {
                "operator": "<=",
                "value": _WATERMARK_PSEUDO_TABLE_MAX_NONEMPTY_CELLS,
            },
            "candidate_character_count": {
                "operator": "<=",
                "value": _WATERMARK_PSEUDO_TABLE_MAX_CHARACTER_COUNT,
            },
            "strict_overlap_count": {"operator": "=", "value": 0},
            "ordinary_block_count": {"operator": "=", "value": 0},
            "watermark_block_count": {"operator": ">=", "value": 1},
            "character_coverage_percent": {
                "operator": ">=",
                "value": _TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT,
            },
        }
        message = (
            "Rule watermark_only_tiny_layout suppressed 1 TABLE candidate: "
            f"area {candidate_area:.6f}/{page_area:.6f} <= "
            f"{_WATERMARK_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO:.2f}; rows "
            f"{row_count} <= {_WATERMARK_PSEUDO_TABLE_MAX_ROWS}; columns "
            f"{column_count} <= {_WATERMARK_PSEUDO_TABLE_MAX_COLUMNS}; "
            f"nonempty cells {nonempty_cell_count} <= "
            f"{_WATERMARK_PSEUDO_TABLE_MAX_NONEMPTY_CELLS}; normalized "
            f"character count {candidate_character_count} <= "
            f"{_WATERMARK_PSEUDO_TABLE_MAX_CHARACTER_COUNT}; strict overlap "
            f"count {len(strict_overlaps)} = 0; ordinary block count "
            f"{len(ordinary_blocks)} = 0; watermark block count "
            f"{len(watermark_blocks)} >= 1; matched characters "
            f"{watermark_matched}/{coverage_denominator} >= "
            f"{_TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT}%."
        )
    elif (
        area_ratio <= _SHALLOW_WIDE_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO
        and row_count <= _SHALLOW_WIDE_PSEUDO_TABLE_MAX_ROWS
        and column_count >= _SHALLOW_WIDE_PSEUDO_TABLE_MIN_COLUMNS
        and not strict_overlaps
        and _SHALLOW_WIDE_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS
        <= len(ordinary_blocks)
        <= _SHALLOW_WIDE_PSEUDO_TABLE_MAX_ORDINARY_BLOCKS
        and all(
            (block.bbox[2] - block.bbox[0])
            >= candidate_width * _SHALLOW_WIDE_PSEUDO_TABLE_MIN_BLOCK_WIDTH_RATIO
            for block in ordinary_blocks
        )
        and ordinary_matched * 100
        >= coverage_denominator * _TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT
    ):
        rule_id = "shallow_wide_paragraph_conflict"
        evidence_blocks = ordinary_blocks
        matched_character_count = ordinary_matched
        thresholds = {
            "area_ratio": {
                "operator": "<=",
                "value": _SHALLOW_WIDE_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO,
            },
            "row_count": {
                "operator": "<=",
                "value": _SHALLOW_WIDE_PSEUDO_TABLE_MAX_ROWS,
            },
            "column_count": {
                "operator": ">=",
                "value": _SHALLOW_WIDE_PSEUDO_TABLE_MIN_COLUMNS,
            },
            "strict_overlap_count": {"operator": "=", "value": 0},
            "ordinary_block_count": {
                "operator": "between_inclusive",
                "minimum": _SHALLOW_WIDE_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS,
                "maximum": _SHALLOW_WIDE_PSEUDO_TABLE_MAX_ORDINARY_BLOCKS,
            },
            "ordinary_block_width_ratio": {
                "operator": ">=",
                "value": _SHALLOW_WIDE_PSEUDO_TABLE_MIN_BLOCK_WIDTH_RATIO,
            },
            "character_coverage_percent": {
                "operator": ">=",
                "value": _TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT,
            },
        }
        minimum_block_width = min(
            block.bbox[2] - block.bbox[0] for block in ordinary_blocks
        )
        message = (
            "Rule shallow_wide_paragraph_conflict suppressed 1 TABLE "
            f"candidate: area {candidate_area:.6f}/{page_area:.6f} <= "
            f"{_SHALLOW_WIDE_PSEUDO_TABLE_MAX_PAGE_AREA_RATIO:.2f}; rows "
            f"{row_count} <= {_SHALLOW_WIDE_PSEUDO_TABLE_MAX_ROWS}; columns "
            f"{column_count} >= {_SHALLOW_WIDE_PSEUDO_TABLE_MIN_COLUMNS}; "
            f"strict overlap count {len(strict_overlaps)} = 0; ordinary block "
            f"count {len(ordinary_blocks)} within "
            f"{_SHALLOW_WIDE_PSEUDO_TABLE_MIN_ORDINARY_BLOCKS}.."
            f"{_SHALLOW_WIDE_PSEUDO_TABLE_MAX_ORDINARY_BLOCKS}; minimum block "
            f"width {minimum_block_width:.6f}/{candidate_width:.6f} >= "
            f"{_SHALLOW_WIDE_PSEUDO_TABLE_MIN_BLOCK_WIDTH_RATIO:.2f}; matched "
            f"characters {ordinary_matched}/{coverage_denominator} >= "
            f"{_TABLE_SUPPRESSION_MIN_CHARACTER_COVERAGE_PERCENT}%."
        )

    if rule_id is None:
        return None
    strict_overlap_payload = []
    for strict_native_index, strict_candidate in strict_overlaps:
        strict_bbox = _candidate_bbox(strict_candidate)
        if strict_bbox is None:
            continue
        strict_dimensions = _candidate_dimensions(strict_candidate)
        strict_overlap_payload.append(
            {
                "native_index": strict_native_index,
                "bbox": _rounded_bbox(strict_bbox),
                "rows": (
                    strict_dimensions[0] if strict_dimensions is not None else None
                ),
                "columns": (
                    strict_dimensions[1] if strict_dimensions is not None else None
                ),
                "intersection_area": round(
                    _bbox_area(_bbox_intersection(candidate_bbox, strict_bbox)),
                    6,
                ),
                "smaller_area": round(
                    min(candidate_area, _bbox_area(strict_bbox)),
                    6,
                ),
            }
        )
    evidence_payload: dict[str, Any] = {
        "candidate_bbox": _rounded_bbox(candidate_bbox),
        "page_bbox": _rounded_bbox(page_bbox),
        "rows": row_count,
        "columns": column_count,
        "cell_count": row_count * column_count,
        "nonempty_cell_count": nonempty_cell_count,
        "candidate_character_count": candidate_character_count,
        "candidate_text_sha256": hashlib.sha256(
            normalized_candidate_text.encode("utf-8")
        ).hexdigest(),
        "candidate_char_counter_sha256": _table_suppression_counter_sha256(
            candidate_counter
        ),
        "area_ratio_numerator": round(candidate_area, 6),
        "area_ratio_denominator": round(page_area, 6),
        "raw_strict_count": len(raw_strict_candidates),
        "strict_overlap_count": len(strict_overlaps),
        "strict_overlaps": strict_overlap_payload,
        "ordinary_blocks": [
            _table_suppression_block_evidence(block) for block in ordinary_blocks
        ],
        "watermark_blocks": [
            _table_suppression_block_evidence(block) for block in watermark_blocks
        ],
        "evidence_block_count": len(evidence_blocks),
        "matched_character_count": matched_character_count,
        "coverage_denominator": coverage_denominator,
        "thresholds": thresholds,
        "candidate_native_index_observed": candidate_native_index,
    }
    return rule_id, evidence_payload, message


def _table_suppression_candidate_text(
    candidate: Any,
) -> tuple[str, int] | None:
    dimensions = _candidate_dimensions(candidate)
    if dimensions is None:
        return None
    row_count, column_count = dimensions
    try:
        extractor = getattr(candidate, "extract", None)
        extracted_rows = extractor() if callable(extractor) else None
    except MemoryError:
        raise
    except Exception:
        return None
    if not _complete_table_text_matrix(
        extracted_rows,
        row_count=row_count,
        column_count=column_count,
    ):
        return None
    fragments = tuple(value or "" for row in extracted_rows for value in row)
    return (
        _table_suppression_normalized_text("".join(fragments)),
        sum(
            1 for fragment in fragments if _table_suppression_normalized_text(fragment)
        ),
    )


def _table_suppression_text_blocks(
    *,
    raw_page: Any,
    page_rotation_matrix: object,
    candidate_bbox: tuple[float, float, float, float],
) -> tuple[
    tuple[_TableSuppressionTextBlock, ...],
    tuple[_TableSuppressionTextBlock, ...],
]:
    blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
    if not isinstance(blocks, (list, tuple)):
        return (), ()
    ordinary: list[_TableSuppressionTextBlock] = []
    watermarks: list[_TableSuppressionTextBlock] = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        block_bbox = _pdf_visual_bbox(
            _native_bbox(block.get("bbox")),
            rotation_matrix=page_rotation_matrix,
        )
        if (
            block_bbox is None
            or _bbox_area(_bbox_intersection(block_bbox, candidate_bbox)) <= 0.0
        ):
            continue
        normalized_text = _table_suppression_normalized_text(
            _raw_text_from_block(block)
        )
        if not normalized_text:
            continue
        observation = _TableSuppressionTextBlock(
            block_index=block_index,
            kind=_native_pdf_text_kind(
                block,
                fallback=SourceNodeKind.PARAGRAPH,
            ),
            bbox=block_bbox,
            normalized_text=normalized_text,
        )
        if observation.kind is SourceNodeKind.WATERMARK:
            watermarks.append(observation)
        else:
            ordinary.append(observation)
    return tuple(ordinary), tuple(watermarks)


def _table_suppression_strict_overlaps(
    *,
    candidate_bbox: tuple[float, float, float, float],
    raw_strict_candidates: tuple[Any, ...],
) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (native_index, strict_candidate)
        for native_index, strict_candidate in enumerate(raw_strict_candidates)
        if (
            (strict_bbox := _candidate_bbox(strict_candidate)) is not None
            and _bbox_overlap_ratio(candidate_bbox, strict_bbox)
            >= _TABLE_CANDIDATE_OVERLAP_RATIO
        )
    )


def _table_suppression_normalized_text(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def _table_suppression_counter_sha256(counter: Counter[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            sorted(counter.items()),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _table_suppression_block_evidence(
    block: _TableSuppressionTextBlock,
) -> dict[str, Any]:
    return {
        "block_index": block.block_index,
        "kind": block.kind.value,
        "bbox": _rounded_bbox(block.bbox),
        "character_count": len(block.normalized_text),
        "text_sha256": hashlib.sha256(
            block.normalized_text.encode("utf-8")
        ).hexdigest(),
    }


def _rounded_bbox(
    bbox: tuple[float, float, float, float],
) -> list[float]:
    return [round(value, 6) for value in bbox]


def _select_table_candidates(
    *,
    default_candidates: tuple[Any, ...],
    strict_candidates: tuple[Any, ...],
    page_bbox: tuple[float, float, float, float] | None,
    page_bboxlog: tuple[Any, ...] = (),
) -> tuple[tuple[Any, ...], int, int]:
    """Combine detector results by geometry without counting one table twice."""

    selected = [
        candidate
        for candidate in default_candidates
        if _default_table_candidate_is_controlled(candidate)
    ]
    strict_added = 0
    strict_replaced = 0
    ordered_strict = sorted(
        enumerate(strict_candidates),
        key=lambda item: _table_candidate_sort_key(item[1], item[0]),
    )
    for _, strict_candidate in ordered_strict:
        strict_bbox = _candidate_bbox(strict_candidate)
        if strict_bbox is None:
            continue
        overlapping_indexes = [
            index
            for index, existing in enumerate(selected)
            if (
                (existing_bbox := _candidate_bbox(existing)) is not None
                and _bbox_overlap_ratio(strict_bbox, existing_bbox)
                >= _TABLE_CANDIDATE_OVERLAP_RATIO
            )
        ]
        strict_is_controlled = _strict_table_candidate_is_controlled(
            strict_candidate,
            page_bbox=page_bbox,
        ) or _strict_two_row_table_candidate_is_controlled(
            strict_candidate,
            page_bbox=page_bbox,
            page_bboxlog=page_bboxlog,
        )
        if not strict_is_controlled and not (
            len(overlapping_indexes) == 1
            and _strict_near_full_replacement_candidate_is_controlled(
                strict_candidate,
                page_bbox=page_bbox,
            )
        ):
            continue
        if not overlapping_indexes:
            selected.append(strict_candidate)
            strict_added += 1
            continue
        if len(overlapping_indexes) != 1:
            # One broad strict grid that merges several default tables is not
            # safe to select automatically.
            continue
        existing_index = overlapping_indexes[0]
        if (
            _strict_candidate_resolves_ambiguous_default(
                strict_candidate,
                existing=selected[existing_index],
            )
            or _strict_candidate_deduplicates_ambiguous_default(
                strict_candidate,
                existing=selected[existing_index],
            )
            or _strict_candidate_is_more_complete(
                strict_candidate,
                existing=selected[existing_index],
            )
            or _strict_candidate_resolves_ruled_axis_tradeoff(
                strict_candidate,
                existing=selected[existing_index],
                page_bboxlog=page_bboxlog,
            )
        ):
            selected[existing_index] = strict_candidate
            strict_replaced += 1
    return tuple(selected), strict_added, strict_replaced


def _strict_near_full_replacement_candidate_is_controlled(
    candidate: Any,
    *,
    page_bbox: tuple[float, float, float, float] | None,
) -> bool:
    """Allow a near-full ruled grid only as a one-to-one replacement.

    Full-page text-layout false positives remain excluded from the normal
    ``lines_strict`` addition path.  This narrow exception only lets the
    subsequent proof-and-text comparison consider a candidate that overlaps
    exactly one default detector result.
    """

    candidate_bbox = _candidate_bbox(candidate)
    dimensions = _candidate_dimensions(candidate)
    if candidate_bbox is None or page_bbox is None or dimensions is None:
        return False
    row_count, column_count = dimensions
    if row_count < 3 or column_count < 2:
        return False
    candidate_area = _bbox_area(candidate_bbox)
    page_area = _bbox_area(page_bbox)
    if candidate_area <= 0.0 or page_area <= 0.0:
        return False
    if candidate_area / page_area > _STRICT_TABLE_MAX_NEAR_FULL_REPLACEMENT_AREA_RATIO:
        return False
    intersection = _bbox_intersection(candidate_bbox, page_bbox)
    return _bbox_area(intersection) / candidate_area >= 0.98


def _default_table_candidate_is_controlled(candidate: Any) -> bool:
    dimensions = _candidate_dimensions(candidate)
    if dimensions is None:
        return False
    row_count, column_count = dimensions
    if column_count < 2:
        return False
    if row_count >= 2:
        return True
    try:
        native_rows = getattr(candidate, "rows", None)
    except Exception:
        return False
    return (
        _prove_pdf_table_cell_geometry(
            native_rows=native_rows,
            table_bbox=_candidate_bbox(candidate),
            row_count=row_count,
            column_count=column_count,
        )
        is not None
    )


def _strict_table_candidate_is_controlled(
    candidate: Any,
    *,
    page_bbox: tuple[float, float, float, float] | None,
) -> bool:
    candidate_bbox = _candidate_bbox(candidate)
    dimensions = _candidate_dimensions(candidate)
    if candidate_bbox is None or page_bbox is None or dimensions is None:
        return False
    row_count, column_count = dimensions
    if row_count < 3 or column_count < 2:
        return False
    candidate_area = _bbox_area(candidate_bbox)
    page_area = _bbox_area(page_bbox)
    if candidate_area <= 0.0 or page_area <= 0.0:
        return False
    if candidate_area / page_area > _STRICT_TABLE_MAX_PAGE_AREA_RATIO:
        # Prevent an all-page text layout from becoming a synthetic table.
        return False
    intersection = _bbox_intersection(candidate_bbox, page_bbox)
    return _bbox_area(intersection) / candidate_area >= 0.98


def _strict_two_row_table_candidate_is_controlled(
    candidate: Any,
    *,
    page_bbox: tuple[float, float, float, float] | None,
    page_bboxlog: tuple[Any, ...],
) -> bool:
    """Admit a two-row strict grid only with direct native ruled evidence."""

    candidate_bbox = _candidate_bbox(candidate)
    dimensions = _candidate_dimensions(candidate)
    if candidate_bbox is None or page_bbox is None or dimensions is None:
        return False
    row_count, column_count = dimensions
    if row_count != 2 or column_count < 2:
        return False
    candidate_area = _bbox_area(candidate_bbox)
    page_area = _bbox_area(page_bbox)
    if (
        candidate_area <= 0.0
        or page_area <= 0.0
        or candidate_area / page_area > _STRICT_TABLE_MAX_PAGE_AREA_RATIO
    ):
        return False
    intersection = _bbox_intersection(candidate_bbox, page_bbox)
    if _bbox_area(intersection) / candidate_area < 0.98:
        return False

    observation = _candidate_geometry_observation(candidate)
    if observation is None or observation[0] is None:
        return False
    try:
        extracted_rows = candidate.extract()
        native_rows = candidate.rows
    except Exception:
        return False
    if (
        not _complete_table_text_matrix(
            extracted_rows,
            row_count=row_count,
            column_count=column_count,
        )
        or not isinstance(native_rows, (list, tuple))
        or len(native_rows) != row_count
    ):
        return False
    if (
        sum(
            bool((value or "").strip())
            for extracted_row in extracted_rows
            for value in extracted_row
        )
        < 2
    ):
        return False

    row_bboxes: list[tuple[float, float, float, float]] = []
    fully_rectangular_rows = 0
    for native_row in native_rows:
        row_bbox = _native_bbox(getattr(native_row, "bbox", None))
        native_cells = getattr(native_row, "cells", None)
        if (
            row_bbox is None
            or not isinstance(native_cells, (list, tuple))
            or len(native_cells) != column_count
        ):
            return False
        row_bboxes.append(row_bbox)
        if all(_native_bbox(cell) is not None for cell in native_cells):
            fully_rectangular_rows += 1
    if fully_rectangular_rows < 1:
        return False
    if (
        abs(row_bboxes[0][1] - candidate_bbox[1])
        > _TABLE_RULE_BOUNDARY_TOLERANCE_POINTS
        or abs(row_bboxes[0][3] - row_bboxes[1][1])
        > _TABLE_RULE_BOUNDARY_TOLERANCE_POINTS
        or abs(row_bboxes[1][3] - candidate_bbox[3])
        > _TABLE_RULE_BOUNDARY_TOLERANCE_POINTS
    ):
        return False
    boundaries = (
        row_bboxes[0][1],
        (row_bboxes[0][3] + row_bboxes[1][1]) / 2.0,
        row_bboxes[1][3],
    )
    return all(
        _horizontal_boundary_rule_coverage(
            boundary_y,
            table_bbox=candidate_bbox,
            page_bboxlog=page_bboxlog,
        )
        >= _TABLE_RULE_MIN_WIDTH_COVERAGE
        for boundary_y in boundaries
    )


def _strict_candidate_is_more_complete(candidate: Any, *, existing: Any) -> bool:
    strict_dimensions = _candidate_dimensions(candidate)
    existing_dimensions = _candidate_dimensions(existing)
    strict_bbox = _candidate_bbox(candidate)
    existing_bbox = _candidate_bbox(existing)
    if (
        strict_dimensions is None
        or existing_dimensions is None
        or strict_bbox is None
        or existing_bbox is None
    ):
        return False
    strict_rows, strict_columns = strict_dimensions
    existing_rows, existing_columns = existing_dimensions
    if strict_rows < existing_rows or strict_columns < existing_columns:
        return False
    row_expands_significantly = strict_rows >= math.ceil(existing_rows * 1.50)
    column_expands_significantly = strict_columns >= math.ceil(existing_columns * 1.50)
    if not (row_expands_significantly or column_expands_significantly):
        return False
    if strict_rows == existing_rows and strict_columns == existing_columns:
        return False
    strict_cells = strict_rows * strict_columns
    existing_cells = existing_rows * existing_columns
    if strict_cells < math.ceil(
        existing_cells * _STRICT_TABLE_MIN_COMPLETENESS_MULTIPLIER
    ):
        return False
    strict_area = _bbox_area(strict_bbox)
    existing_area = _bbox_area(existing_bbox)
    if strict_area <= 0.0 or existing_area <= 0.0:
        return False
    return (
        max(strict_area, existing_area) / min(strict_area, existing_area)
        <= _STRICT_TABLE_MAX_REPLACEMENT_AREA_RATIO
    )


def _strict_candidate_resolves_ruled_axis_tradeoff(
    candidate: Any,
    *,
    existing: Any,
    page_bboxlog: tuple[Any, ...],
) -> bool:
    """Prefer one coextensive ruled grid that recovers a missing table axis."""

    strict_dimensions = _candidate_dimensions(candidate)
    existing_dimensions = _candidate_dimensions(existing)
    strict_bbox = _candidate_bbox(candidate)
    existing_bbox = _candidate_bbox(existing)
    if (
        strict_dimensions is None
        or existing_dimensions is None
        or strict_bbox is None
        or existing_bbox is None
    ):
        return False
    strict_rows, strict_columns = strict_dimensions
    existing_rows, existing_columns = existing_dimensions
    if not (
        strict_rows < existing_rows
        and strict_columns > existing_columns
        and strict_rows * strict_columns == existing_rows * existing_columns
    ):
        return False

    strict_area = _bbox_area(strict_bbox)
    existing_area = _bbox_area(existing_bbox)
    intersection_area = _bbox_area(_bbox_intersection(strict_bbox, existing_bbox))
    if (
        strict_area <= 0.0
        or existing_area <= 0.0
        or _bbox_overlap_ratio(strict_bbox, existing_bbox) < 0.98
        or intersection_area / existing_area < 0.98
        or strict_area / existing_area > _STRICT_TABLE_MAX_REPLACEMENT_AREA_RATIO
    ):
        return False
    try:
        native_rows = candidate.rows
    except Exception:
        return False
    if not isinstance(native_rows, (list, tuple)) or len(native_rows) != strict_rows:
        return False
    first_row_bbox = _native_bbox(getattr(native_rows[0], "bbox", None))
    first_row_cells = getattr(native_rows[0], "cells", None)
    if (
        first_row_bbox is None
        or not isinstance(first_row_cells, (list, tuple))
        or len(first_row_cells) != strict_columns
    ):
        return False
    normalized_first_row_cells = tuple(_native_bbox(cell) for cell in first_row_cells)
    if not _row_has_full_width_cell(
        normalized_first_row_cells,
        candidate_bbox=strict_bbox,
    ) or not _row_has_complete_horizontal_rule_support(
        first_row_bbox,
        table_bbox=strict_bbox,
        page_bboxlog=page_bboxlog,
    ):
        return False

    strict_observation = _candidate_geometry_observation(candidate)
    existing_observation = _candidate_geometry_observation(existing)
    if strict_observation is None or existing_observation is None:
        return False
    strict_proven, strict_text, strict_tokens = strict_observation
    existing_proven, existing_text, existing_tokens = existing_observation
    if (
        strict_proven is None
        or existing_proven is None
        or not strict_tokens
        or strict_tokens != existing_tokens
    ):
        return False
    strict_characters = Counter(
        character for character in strict_text if not character.isspace()
    )
    existing_characters = Counter(
        character for character in existing_text if not character.isspace()
    )
    return not (existing_characters - strict_characters) and bool(
        strict_characters - existing_characters
    )


def _strict_candidate_resolves_ambiguous_default(
    candidate: Any,
    *,
    existing: Any,
) -> bool:
    """Prefer a smaller ruled grid only when it resolves proven ambiguity.

    PyMuPDF's default detector can occasionally expose nested, overlapping
    logical cells around one merged row.  A smaller ``lines_strict`` grid is
    safer only when the default geometry fails proof, the strict geometry
    passes proof, both candidates retain the same unique text tokens, and the
    strict candidate remains close in rows, area, cell count, and total text.
    """

    strict_dimensions = _candidate_dimensions(candidate)
    existing_dimensions = _candidate_dimensions(existing)
    strict_bbox = _candidate_bbox(candidate)
    existing_bbox = _candidate_bbox(existing)
    if (
        strict_dimensions is None
        or existing_dimensions is None
        or strict_bbox is None
        or existing_bbox is None
    ):
        return False
    strict_rows, strict_columns = strict_dimensions
    existing_rows, existing_columns = existing_dimensions
    if (
        strict_rows > existing_rows
        or strict_columns > existing_columns
        or (strict_rows, strict_columns) == (existing_rows, existing_columns)
        or strict_rows
        < math.ceil(existing_rows * _STRICT_TABLE_MIN_AMBIGUITY_ROW_RATIO)
    ):
        return False

    strict_cells = strict_rows * strict_columns
    existing_cells = existing_rows * existing_columns
    if strict_cells < math.ceil(
        existing_cells * _STRICT_TABLE_MIN_AMBIGUITY_CELL_RATIO
    ):
        return False
    strict_area = _bbox_area(strict_bbox)
    existing_area = _bbox_area(existing_bbox)
    if (
        strict_area <= 0.0
        or existing_area <= 0.0
        or max(strict_area, existing_area) / min(strict_area, existing_area)
        > _STRICT_TABLE_MAX_REPLACEMENT_AREA_RATIO
    ):
        return False

    strict_observation = _candidate_geometry_observation(candidate)
    existing_observation = _candidate_geometry_observation(existing)
    if strict_observation is None or existing_observation is None:
        return False
    strict_proven, strict_text, strict_tokens = strict_observation
    existing_proven, existing_text, existing_tokens = existing_observation
    if (
        strict_proven is None
        or existing_proven is not None
        or not strict_tokens
        or strict_tokens != existing_tokens
        or len(strict_text)
        < math.ceil(len(existing_text) * _STRICT_TABLE_MIN_AMBIGUITY_TEXT_RATIO)
    ):
        return False
    return True


def _strict_candidate_deduplicates_ambiguous_default(
    candidate: Any,
    *,
    existing: Any,
) -> bool:
    """Prefer a proven grid when the default only repeats native characters.

    Some authored page-layout tables expose dozens of phantom logical
    rows/columns through the default detector.  ``lines_strict`` may recover
    the actual merged grid, but its token boundaries differ enough that token
    equality is too strong.  Replacement remains fail-closed: the strict grid
    must be smaller, geometry-proven, nearly coextensive, and preserve at least
    95% of the default's non-whitespace character multiset while introducing
    no character the default did not already expose.
    """

    strict_dimensions = _candidate_dimensions(candidate)
    existing_dimensions = _candidate_dimensions(existing)
    strict_bbox = _candidate_bbox(candidate)
    existing_bbox = _candidate_bbox(existing)
    if (
        strict_dimensions is None
        or existing_dimensions is None
        or strict_bbox is None
        or existing_bbox is None
    ):
        return False
    strict_rows, strict_columns = strict_dimensions
    existing_rows, existing_columns = existing_dimensions
    if (
        strict_rows > existing_rows
        or strict_columns > existing_columns
        or (strict_rows, strict_columns) == (existing_rows, existing_columns)
    ):
        return False
    strict_area = _bbox_area(strict_bbox)
    existing_area = _bbox_area(existing_bbox)
    if (
        strict_area <= 0.0
        or existing_area <= 0.0
        or max(strict_area, existing_area) / min(strict_area, existing_area) > 1.05
    ):
        return False

    strict_observation = _candidate_geometry_observation(candidate)
    existing_observation = _candidate_geometry_observation(existing)
    if strict_observation is None or existing_observation is None:
        return False
    strict_proven, strict_text, _ = strict_observation
    existing_proven, existing_text, _ = existing_observation
    if strict_proven is None or existing_proven is not None:
        return False

    strict_characters = Counter(
        character for character in strict_text if not character.isspace()
    )
    existing_characters = Counter(
        character for character in existing_text if not character.isspace()
    )
    strict_character_count = strict_characters.total()
    existing_character_count = existing_characters.total()
    if (
        strict_character_count < _STRICT_TABLE_MIN_DEDUPLICATED_CHARACTER_COUNT
        or existing_character_count < strict_character_count
        or strict_characters - existing_characters
    ):
        return False
    if strict_character_count < math.ceil(
        existing_character_count * _STRICT_TABLE_MIN_DEDUPLICATED_CHARACTER_RATIO
    ):
        return False
    excess_character_count = (existing_characters - strict_characters).total()
    return excess_character_count <= math.ceil(
        strict_character_count * _STRICT_TABLE_MAX_DEDUPLICATED_EXCESS_RATIO
    )


def _candidate_geometry_observation(
    candidate: Any,
) -> (
    tuple[
        dict[tuple[int, int], _ProvenTableCellGeometry] | None,
        str,
        frozenset[str],
    ]
    | None
):
    dimensions = _candidate_dimensions(candidate)
    table_bbox = _candidate_bbox(candidate)
    if dimensions is None or table_bbox is None:
        return None
    row_count, column_count = dimensions
    try:
        extractor = getattr(candidate, "extract", None)
        extracted_rows = extractor() if callable(extractor) else None
        native_rows = getattr(candidate, "rows", None)
    except Exception:
        return None
    if not _complete_table_text_matrix(
        extracted_rows,
        row_count=row_count,
        column_count=column_count,
    ):
        return None
    text_fragments = tuple(
        value or "" for extracted_row in extracted_rows for value in extracted_row
    )
    text = "".join(text_fragments)
    tokens = frozenset(
        re.findall(
            r"\w+|[^\w\s]",
            "\n".join(fragment for fragment in text_fragments if fragment),
            flags=re.UNICODE,
        )
    )
    proven = _prove_pdf_table_cell_geometry(
        native_rows=native_rows,
        table_bbox=table_bbox,
        row_count=row_count,
        column_count=column_count,
        extracted_rows=extracted_rows,
    )
    if proven is not None and not _table_text_is_compatible_with_proven_geometry(
        extracted_rows,
        proven_geometry=proven,
        row_count=row_count,
        column_count=column_count,
    ):
        proven = None
    return proven, text, tokens


def _candidate_dimensions(candidate: Any) -> tuple[int, int] | None:
    try:
        row_count = getattr(candidate, "row_count", None)
        column_count = getattr(candidate, "col_count", None)
    except Exception:
        return None
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 1
        or isinstance(column_count, bool)
        or not isinstance(column_count, int)
        or column_count < 1
    ):
        return None
    return row_count, column_count


def _candidate_bbox(candidate: Any) -> tuple[float, float, float, float] | None:
    try:
        return _native_bbox(getattr(candidate, "bbox", None))
    except Exception:
        return None


def _bbox_overlap_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    first_area = _bbox_area(first)
    second_area = _bbox_area(second)
    smaller_area = min(first_area, second_area)
    if smaller_area <= 0.0:
        return 0.0
    return _bbox_area(_bbox_intersection(first, second)) / smaller_area


def _bbox_intersection(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )


def _complete_table_text_matrix(
    extracted_rows: Any,
    *,
    row_count: int,
    column_count: int,
) -> bool:
    if (
        not isinstance(extracted_rows, (list, tuple))
        or len(extracted_rows) != row_count
    ):
        return False
    return all(
        isinstance(row, (list, tuple))
        and len(row) == column_count
        and all(value is None or isinstance(value, str) for value in row)
        for row in extracted_rows
    )


def _cluster_table_grid_coordinates(values: list[float]) -> tuple[float, ...]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if (
            not groups
            or abs(value - (sum(groups[-1]) / len(groups[-1])))
            > _TABLE_GRID_ALIGNMENT_TOLERANCE_POINTS
        ):
            groups.append([value])
        else:
            groups[-1].append(value)
    return tuple(sum(group) / len(group) for group in groups)


def _table_grid_boundary_index(
    boundaries: tuple[float, ...],
    value: float,
) -> int | None:
    matches = tuple(
        index
        for index, boundary in enumerate(boundaries)
        if abs(boundary - value) <= _TABLE_GRID_ALIGNMENT_TOLERANCE_POINTS
    )
    return matches[0] if len(matches) == 1 else None


def _prove_pdf_table_cell_geometry(
    *,
    native_rows: Any,
    table_bbox: tuple[float, float, float, float] | None,
    row_count: int,
    column_count: int,
    extracted_rows: Any = None,
) -> dict[tuple[int, int], _ProvenTableCellGeometry] | None:
    """Prove logical cell spans from native PDF grid coordinates.

    PyMuPDF exposes one rectangle at a merged cell's anchor and ``None`` in
    each covered logical slot.  We preserve that span only when every native
    rectangle aligns to one complete row/column boundary grid and those
    anchors cover every logical slot exactly once.  Any ambiguity fails closed
    to the legacy rectangular representation.
    """

    if (
        table_bbox is None
        or table_bbox[2] <= table_bbox[0]
        or table_bbox[3] <= table_bbox[1]
        or not isinstance(native_rows, (list, tuple))
        or len(native_rows) != row_count
    ):
        return None

    cell_bboxes: list[list[tuple[float, float, float, float] | None]] = []
    x_coordinates = [table_bbox[0], table_bbox[2]]
    y_coordinates = [table_bbox[1], table_bbox[3]]
    for native_row in native_rows:
        native_cells = getattr(native_row, "cells", None)
        if (
            not isinstance(native_cells, (list, tuple))
            or len(native_cells) != column_count
        ):
            return None
        row_bboxes: list[tuple[float, float, float, float] | None] = []
        for native_cell in native_cells:
            if native_cell is None:
                row_bboxes.append(None)
                continue
            bbox = _native_bbox(native_cell)
            if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                return None
            row_bboxes.append(bbox)
            x_coordinates.extend((bbox[0], bbox[2]))
            y_coordinates.extend((bbox[1], bbox[3]))
        cell_bboxes.append(row_bboxes)

    column_boundaries = _cluster_table_grid_coordinates(x_coordinates)
    row_boundaries = _cluster_table_grid_coordinates(y_coordinates)
    if (
        len(column_boundaries) != column_count + 1
        or len(row_boundaries) != row_count + 1
        or _table_grid_boundary_index(column_boundaries, table_bbox[0]) != 0
        or _table_grid_boundary_index(column_boundaries, table_bbox[2]) != column_count
        or _table_grid_boundary_index(row_boundaries, table_bbox[1]) != 0
        or _table_grid_boundary_index(row_boundaries, table_bbox[3]) != row_count
    ):
        return None

    proven: dict[tuple[int, int], _ProvenTableCellGeometry] = {}
    coverage: dict[tuple[int, int], tuple[int, int]] = {}
    for row_index, row_bboxes in enumerate(cell_bboxes):
        for column_index, bbox in enumerate(row_bboxes):
            if bbox is None:
                continue
            column_start = _table_grid_boundary_index(column_boundaries, bbox[0])
            column_end = _table_grid_boundary_index(column_boundaries, bbox[2])
            row_start = _table_grid_boundary_index(row_boundaries, bbox[1])
            row_end = _table_grid_boundary_index(row_boundaries, bbox[3])
            if (
                None in (row_start, row_end, column_start, column_end)
                or row_start != row_index
                or column_start != column_index
                or row_end <= row_start
                or column_end <= column_start
            ):
                return None
            assert row_start is not None
            assert row_end is not None
            assert column_start is not None
            assert column_end is not None
            anchor = (row_index, column_index)
            geometry = _ProvenTableCellGeometry(
                bbox=bbox,
                row_span=row_end - row_start,
                column_span=column_end - column_start,
            )
            for covered_row in range(row_start, row_end):
                for covered_column in range(column_start, column_end):
                    position = (covered_row, covered_column)
                    if position in coverage:
                        return None
                    coverage[position] = anchor
            proven[anchor] = geometry

    if len(coverage) != row_count * column_count:
        if not _complete_table_text_matrix(
            extracted_rows,
            row_count=row_count,
            column_count=column_count,
        ):
            return None
        for row_index in range(row_count):
            for column_index in range(column_count):
                position = (row_index, column_index)
                if position in coverage:
                    continue
                if cell_bboxes[row_index][column_index] is not None or extracted_rows[
                    row_index
                ][column_index] not in (None, ""):
                    return None
                geometry = _ProvenTableCellGeometry(
                    bbox=(
                        column_boundaries[column_index],
                        row_boundaries[row_index],
                        column_boundaries[column_index + 1],
                        row_boundaries[row_index + 1],
                    ),
                    row_span=1,
                    column_span=1,
                )
                proven[position] = geometry
                coverage[position] = position
    for row_index, row_bboxes in enumerate(cell_bboxes):
        for column_index, bbox in enumerate(row_bboxes):
            position = (row_index, column_index)
            if bbox is not None and coverage.get(position) != position:
                return None
            if (
                bbox is None
                and coverage.get(position) == position
                and position not in proven
            ):
                return None
    return proven


def _table_text_is_compatible_with_proven_geometry(
    extracted_rows: Any,
    *,
    proven_geometry: dict[tuple[int, int], _ProvenTableCellGeometry],
    row_count: int,
    column_count: int,
) -> bool:
    if not _complete_table_text_matrix(
        extracted_rows,
        row_count=row_count,
        column_count=column_count,
    ):
        return False
    return all(
        (row_index, column_index) in proven_geometry
        or extracted_rows[row_index][column_index] in (None, "")
        for row_index in range(row_count)
        for column_index in range(column_count)
    )


def _candidate_has_covered_native_cell_slots(
    native_rows: Any,
    *,
    row_count: int,
    column_count: int,
) -> bool:
    if not isinstance(native_rows, (list, tuple)):
        return False
    for row_index in range(min(row_count, len(native_rows))):
        native_cells = getattr(native_rows[row_index], "cells", None)
        if not isinstance(native_cells, (list, tuple)):
            continue
        if any(cell is None for cell in native_cells[:column_count]):
            return True
    return False


class _PdfTableCellTextIndex:
    """Index a page's glyph geometry once for proven-watermark arbitration.

    A page with no proven watermark cannot justify removing any table text.
    Such pages need no per-cell raw-character scan. On a watermarked page,
    coarse vertical bins only narrow the search; exact center containment,
    source-character order and the original line ordering still decide text.
    """

    def __init__(self, raw_page: Any, page_rotation_matrix: object) -> None:
        self._bins: dict[int, list[tuple[int, int, int, bool, str, tuple[float, float, float, float]]]] = defaultdict(list)
        blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
        self.has_watermark = isinstance(blocks, (tuple, list)) and any(
            isinstance(block, dict)
            and block.get("type") == 0
            and _is_proven_oblique_translucent_watermark(block)
            for block in blocks
        )
        if not self.has_watermark:
            return
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict) or block.get("type") != 0:
                continue
            is_watermark = _is_proven_oblique_translucent_watermark(block)
            lines = block.get("lines")
            if not isinstance(lines, (tuple, list)):
                continue
            for line_index, line in enumerate(lines):
                spans = line.get("spans") if isinstance(line, dict) else None
                if not isinstance(spans, (tuple, list)):
                    continue
                ordinal = 0
                for span in spans:
                    chars = span.get("chars") if isinstance(span, dict) else None
                    if not isinstance(chars, (tuple, list)):
                        continue
                    for char in chars:
                        ordinal += 1
                        if not isinstance(char, dict):
                            continue
                        value = char.get("c")
                        if not isinstance(value, str) or not value:
                            continue
                        bbox = _pdf_visual_bbox(_native_bbox(char.get("bbox")), rotation_matrix=page_rotation_matrix)
                        if bbox is None:
                            continue
                        bucket = math.floor(((bbox[1] + bbox[3]) / 2.0) / 16.0)
                        self._bins[bucket].append((block_index, line_index, ordinal, is_watermark, value, bbox))

    def channels(self, cell_bbox: tuple[float, float, float, float]) -> tuple[str, str] | None:
        by_line: dict[tuple[int, int, bool], list[tuple[int, str, tuple[float, float, float, float]]]] = defaultdict(list)
        first = math.floor(cell_bbox[1] / 16.0)
        last = math.floor(cell_bbox[3] / 16.0)
        for bucket, records in self._bins.items():
            if bucket < first or bucket > last:
                continue
            for block, line, ordinal, watermark, value, bbox in records:
                if _bbox_center_is_inside(bbox, container=cell_bbox):
                    by_line[(block, line, watermark)].append((ordinal, value, bbox))
        if not by_line:
            return None
        ordinary_fragments = []
        watermark_fragments = []
        for (block, line, watermark), chars in by_line.items():
            chars.sort(key=lambda item: item[0])
            text = "".join(item[1] for item in chars).strip()
            bbox = _bbox_union(tuple(item[2] for item in chars))
            if text and bbox is not None:
                target = watermark_fragments if watermark else ordinary_fragments
                target.append(((bbox[1], bbox[0], block, line), text))
        return (
            "\n".join(text for _, text in sorted(ordinary_fragments)),
            "\n".join(text for _, text in sorted(watermark_fragments)),
        )


def _raw_page_table_cell_text_channels(
    *,
    raw_page: Any,
    page_rotation_matrix: object,
    cell_bbox: tuple[float, float, float, float],
) -> tuple[str, str] | None:
    """Return ordinary and proven-watermark text grounded inside one cell.

    PyMuPDF's table extractor may mix diagonal watermark glyphs into otherwise
    valid cells. The raw character layer retains per-glyph geometry and the
    conservative watermark proof, so it can be used to separate the channels
    without deleting or rewriting the authoritative page facts.
    """

    blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
    if not isinstance(blocks, (list, tuple)):
        return None
    ordinary_fragments: list[tuple[tuple[float, float, int, int], str]] = []
    watermark_fragments: list[tuple[tuple[float, float, int, int], str]] = []
    observed_character = False
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        kind = _native_pdf_text_kind(
            block,
            fallback=SourceNodeKind.PARAGRAPH,
        )
        lines = block.get("lines")
        if not isinstance(lines, (list, tuple)):
            continue
        for line_index, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            spans = line.get("spans")
            if not isinstance(spans, (list, tuple)):
                continue
            selected: list[str] = []
            selected_boxes: list[tuple[float, float, float, float]] = []
            for span in spans:
                if not isinstance(span, dict):
                    continue
                characters = span.get("chars")
                if not isinstance(characters, (list, tuple)):
                    continue
                for character in characters:
                    if not isinstance(character, dict):
                        continue
                    value = character.get("c")
                    if not isinstance(value, str) or not value:
                        continue
                    character_bbox = _pdf_visual_bbox(
                        _native_bbox(character.get("bbox")),
                        rotation_matrix=page_rotation_matrix,
                    )
                    if character_bbox is None or not _bbox_center_is_inside(
                        character_bbox,
                        container=cell_bbox,
                    ):
                        continue
                    observed_character = True
                    selected.append(value)
                    selected_boxes.append(character_bbox)
            text = "".join(selected).strip()
            line_bbox = _bbox_union(tuple(selected_boxes))
            if not text or line_bbox is None:
                continue
            fragment = (
                (line_bbox[1], line_bbox[0], block_index, line_index),
                text,
            )
            if kind is SourceNodeKind.WATERMARK:
                watermark_fragments.append(fragment)
            else:
                ordinary_fragments.append(fragment)
    if not observed_character:
        return None

    def joined(
        fragments: list[tuple[tuple[float, float, int, int], str]],
    ) -> str:
        return "\n".join(text for _, text in sorted(fragments))

    return joined(ordinary_fragments), joined(watermark_fragments)


def _bbox_center_is_inside(
    bbox: tuple[float, float, float, float],
    *,
    container: tuple[float, float, float, float],
) -> bool:
    center_x = (bbox[0] + bbox[2]) / 2.0
    center_y = (bbox[1] + bbox[3]) / 2.0
    return (
        container[0] <= center_x <= container[2]
        and container[1] <= center_y <= container[3]
    )


def _table_cell_text_without_proven_watermark(
    candidate_text: str,
    *,
    raw_page: Any,
    page_rotation_matrix: object,
    cell_bbox: tuple[float, float, float, float],
    cell_text_index: _PdfTableCellTextIndex | None = None,
) -> tuple[str, int] | None:
    if cell_text_index is not None and not cell_text_index.has_watermark:
        return None
    channels = cell_text_index.channels(cell_bbox) if cell_text_index is not None else _raw_page_table_cell_text_channels(
        raw_page=raw_page,
        page_rotation_matrix=page_rotation_matrix,
        cell_bbox=cell_bbox,
    )
    if channels is None:
        return None
    ordinary_text, watermark_text = channels
    candidate_counter = Counter(_table_suppression_normalized_text(candidate_text))
    ordinary_counter = Counter(_table_suppression_normalized_text(ordinary_text))
    watermark_counter = Counter(_table_suppression_normalized_text(watermark_text))
    if not candidate_counter or not watermark_counter:
        return None
    # Fail closed unless the raw ordinary channel is wholly present in the
    # detector output and every detector-only character occurrence is proven
    # to come from a watermark glyph inside this exact cell.
    if (candidate_counter & ordinary_counter) != ordinary_counter:
        return None
    removed = candidate_counter - ordinary_counter
    if not removed or (removed - watermark_counter):
        return None
    return ordinary_text, removed.total()


def _table_node_from_candidate(
    *,
    candidate: Any,
    raw_page: Any,
    page_rotation_matrix: object,
    page_number: int,
    table_index: int,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    source_order: int,
    resource_budget: _PdfResourceBudget,
    cell_text_index: _PdfTableCellTextIndex | None = None,
) -> tuple[SourceNode | None, list[SourceDiagnostic], bool]:
    table_bbox = _native_bbox(getattr(candidate, "bbox", None))
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=page_number,
        table_index=table_index,
        bbox=table_bbox,
    )
    row_count = getattr(candidate, "row_count", None)
    column_count = getattr(candidate, "col_count", None)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 1
        or isinstance(column_count, bool)
        or not isinstance(column_count, int)
        or column_count < 1
    ):
        return (
            None,
            [
                _diagnostic(
                    "pdf_table_dimensions_invalid",
                    (
                        f"Native table candidate {table_index} on page {page_number} has no "
                        "usable row and column dimensions."
                    ),
                    native_locator=table_locator,
                )
            ],
            True,
        )
    resource_budget.consume_table_cells(row_count * column_count)

    extractor = getattr(candidate, "extract", None)
    if not callable(extractor):
        return (
            None,
            [
                _diagnostic(
                    "pdf_table_content_unavailable",
                    (
                        f"Native table candidate {table_index} on page {page_number} has no "
                        "cell-text extractor."
                    ),
                    native_locator=table_locator,
                )
            ],
            True,
        )
    try:
        extracted_rows = extractor()
    except Exception as exc:
        return (
            None,
            [
                _diagnostic(
                    "pdf_table_content_extraction_failed",
                    (
                        f"Cell text extraction failed for table candidate {table_index} on "
                        f"page {page_number} ({type(exc).__name__})."
                    ),
                    native_locator=table_locator,
                )
            ],
            True,
        )
    if not isinstance(extracted_rows, (list, tuple)):
        return (
            None,
            [
                _diagnostic(
                    "pdf_table_content_unavailable",
                    (
                        f"Native table candidate {table_index} on page {page_number} did not "
                        "expose a cell-text matrix."
                    ),
                    native_locator=table_locator,
                )
            ],
            True,
        )

    table_id = stable_table_id(
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        native_locator=table_locator,
    )
    node_id = stable_node_id(
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        kind=SourceNodeKind.TABLE,
        native_locator=table_locator,
    )
    native_rows = getattr(candidate, "rows", ())
    if not isinstance(native_rows, (list, tuple)):
        native_rows = ()

    shape_complete = _complete_table_text_matrix(
        extracted_rows,
        row_count=row_count,
        column_count=column_count,
    )
    proven_geometry = _prove_pdf_table_cell_geometry(
        native_rows=native_rows,
        table_bbox=table_bbox,
        row_count=row_count,
        column_count=column_count,
        extracted_rows=extracted_rows,
    )
    if (
        proven_geometry is not None
        and not _table_text_is_compatible_with_proven_geometry(
            extracted_rows,
            proven_geometry=proven_geometry,
            row_count=row_count,
            column_count=column_count,
        )
    ):
        proven_geometry = None

    cells: list[TableCellIR] = []
    watermark_cleaned_positions: list[tuple[int, int]] = []
    watermark_removed_character_count = 0
    for row_index in range(row_count):
        extracted_row = (
            extracted_rows[row_index] if row_index < len(extracted_rows) else None
        )
        if not isinstance(extracted_row, (list, tuple)):
            shape_complete = False
            continue
        if len(extracted_row) != column_count:
            shape_complete = False

        native_row = native_rows[row_index] if row_index < len(native_rows) else None
        native_cells = (
            getattr(native_row, "cells", ()) if native_row is not None else ()
        )
        if not isinstance(native_cells, (list, tuple)):
            native_cells = ()

        for column_index in range(column_count):
            if column_index >= len(extracted_row):
                continue
            geometry = (
                proven_geometry.get((row_index, column_index))
                if proven_geometry is not None
                else None
            )
            if proven_geometry is not None and geometry is None:
                continue
            raw_value = extracted_row[column_index]
            if raw_value is None:
                raw_text = ""
            elif isinstance(raw_value, str):
                raw_text = raw_value
            else:
                shape_complete = False
                continue
            cell_bbox = (
                geometry.bbox
                if geometry is not None
                else (
                    _native_bbox(native_cells[column_index])
                    if column_index < len(native_cells)
                    else None
                )
            )
            if cell_bbox is not None and raw_text:
                watermark_cleaning = _table_cell_text_without_proven_watermark(
                    raw_text,
                    raw_page=raw_page,
                    page_rotation_matrix=page_rotation_matrix,
                    cell_bbox=cell_bbox,
                    cell_text_index=cell_text_index,
                )
                if watermark_cleaning is not None:
                    raw_text, removed_character_count = watermark_cleaning
                    watermark_cleaned_positions.append((row_index, column_index))
                    watermark_removed_character_count += removed_character_count
            cell_locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=page_number,
                table_index=table_index,
                row_index=row_index,
                column_index=column_index,
                bbox=cell_bbox,
            )
            cells.append(
                TableCellIR(
                    cell_id=stable_cell_id(table_id, row_index, column_index),
                    row=row_index,
                    column=column_index,
                    row_span=geometry.row_span if geometry is not None else 1,
                    column_span=(geometry.column_span if geometry is not None else 1),
                    raw_text=raw_text,
                    native_locator=cell_locator,
                )
            )

    local_diagnostics: list[SourceDiagnostic] = []
    if watermark_cleaned_positions:
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "schema": "pdf-table-watermark-exclusion-proof/v1",
                    "source_sha256": source_sha256,
                    "page_number": page_number,
                    "table_index": table_index,
                    "cell_positions": watermark_cleaned_positions,
                    "removed_character_count": (watermark_removed_character_count),
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        local_diagnostics.append(
            _diagnostic(
                "pdf_table_proven_watermark_text_excluded",
                (
                    f"Native table candidate {table_index} on page {page_number} "
                    f"excluded {watermark_removed_character_count} proven "
                    "watermark character occurrence(s) from "
                    f"{len(watermark_cleaned_positions)} cell(s); the immutable "
                    "watermark SourceIR facts remain available separately."
                ),
                severity=SourceDiagnosticSeverity.INFO,
                source_node_id=node_id,
                native_locator=table_locator,
                evidence_sha256=evidence_sha256,
            )
        )
    if table_bbox is None:
        local_diagnostics.append(
            _diagnostic(
                "pdf_table_bbox_missing",
                (
                    f"Native table candidate {table_index} on page {page_number} has no "
                    "usable bounding box."
                ),
                source_node_id=node_id,
                native_locator=table_locator,
            )
        )
    if not shape_complete:
        local_diagnostics.append(
            _diagnostic(
                "pdf_table_matrix_shape_incomplete",
                (
                    f"Native table candidate {table_index} on page {page_number} did not "
                    "provide a complete rectangular cell-text matrix."
                ),
                source_node_id=node_id,
                native_locator=table_locator,
            )
        )
    if proven_geometry is None and _candidate_has_covered_native_cell_slots(
        native_rows,
        row_count=row_count,
        column_count=column_count,
    ):
        local_diagnostics.append(
            _diagnostic(
                "pdf_table_merge_geometry_unresolved",
                (
                    f"Native table candidate {table_index} on page {page_number} contains "
                    "covered cell slots, but their row/column boundary geometry could not "
                    "be proven as one complete non-overlapping grid."
                ),
                source_node_id=node_id,
                native_locator=table_locator,
            )
        )

    merged_regions = (
        tuple(
            MergedRegion(
                row=row,
                column=column,
                row_span=geometry.row_span,
                column_span=geometry.column_span,
            )
            for (row, column), geometry in sorted(proven_geometry.items())
            if geometry.row_span > 1 or geometry.column_span > 1
        )
        if proven_geometry is not None
        else ()
    )

    # Only grid geometry is preserved here.  Header meaning is intentionally
    # absent because native PDF header heuristics are not source facts, and no
    # row is promoted to a requirement.
    table = TableIR(
        table_id=table_id,
        row_count=row_count,
        column_count=column_count,
        header_regions=(),
        cells=tuple(cells),
        merged_regions=merged_regions,
    )
    node = SourceNode(
        source_node_id=node_id,
        kind=SourceNodeKind.TABLE,
        source_order=source_order,
        raw_text="",
        native_locator=table_locator,
        visual_locator=table_locator if table_bbox is not None else None,
        table_payload=table,
        extraction_diagnostics=tuple(local_diagnostics),
    )
    return (
        node,
        local_diagnostics,
        any(
            diagnostic.severity is not SourceDiagnosticSeverity.INFO
            for diagnostic in local_diagnostics
        ),
    )


def _table_candidate_sort_key(candidate: Any, native_index: int) -> tuple[object, ...]:
    """Canonicalize detector order without using any customer text."""

    try:
        bbox = _native_bbox(getattr(candidate, "bbox", None))
    except Exception:
        bbox = None
    if bbox is None:
        geometry = (math.inf, math.inf, math.inf, math.inf)
    else:
        x0, y0, x1, y1 = bbox
        geometry = (y0, x0, y1, x1)
    try:
        row_count = getattr(candidate, "row_count", math.inf)
    except Exception:
        row_count = math.inf
    try:
        column_count = getattr(candidate, "col_count", math.inf)
    except Exception:
        column_count = math.inf
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        row_count = math.inf
    if isinstance(column_count, bool) or not isinstance(column_count, int):
        column_count = math.inf
    return (*geometry, row_count, column_count, native_index)


def _validate_created_at(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SourceIRAdapterRejected(
            SourceIRAdapterRejectionCode.INVALID_TIMESTAMP,
            "created_at must be a timezone-aware datetime.",
        )


def _page_locator(page_number: int, rectangle: Any) -> NativeLocator | None:
    bbox = _native_bbox(
        (
            getattr(rectangle, "x0", None),
            getattr(rectangle, "y0", None),
            getattr(rectangle, "x1", None),
            getattr(rectangle, "y1", None),
        )
    )
    if bbox is None:
        return None
    return NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=page_number,
        bbox=bbox,
    )


def _proven_mrc_page_composite_plan(
    *,
    document: Any,
    page: Any,
    blocks: Any,
    page_bbox: tuple[float, float, float, float] | None,
    preservable_text_block_indexes: frozenset[int],
    rotation_matrix: object,
) -> _MrcPageCompositePlan | None:
    """Prove a scan-base plus PDF paint-stencil stack without guessing.

    PDF `/ImageMask true` XObjects are paint stencils: their 1-bit samples
    select where the current graphics-state colour is painted. PyMuPDF exposes
    those occurrences as ordinary rawdict images even though they are not
    standalone figures. Folding is allowed only when every occurrence maps
    one-to-one to an inspectable XObject, the sole non-stencil raster covers
    the page, and every later occurrence is an explicit paint stencil.
    """

    if page_bbox is None or not isinstance(blocks, (list, tuple)):
        return None
    image_blocks: list[tuple[int, dict[str, Any]]] = []
    preserved_text_block_indexes: list[int] = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict):
            return None
        block_type = block.get("type")
        if block_type == 0:
            if (
                _raw_text_from_block(block)
                and block_index not in preservable_text_block_indexes
            ):
                return None
            if _raw_text_from_block(block):
                preserved_text_block_indexes.append(block_index)
            continue
        if block_type != 1:
            return None
        image_blocks.append((block_index, block))
    if not (2 <= len(image_blocks) <= _MAX_PDF_MRC_LAYERS_PER_PAGE):
        return None

    try:
        drawings = page.get_drawings()
        image_infos = page.get_image_info(hashes=True, xrefs=True)
    except MemoryError:
        raise
    except Exception:
        return None
    if (
        not isinstance(drawings, (list, tuple))
        or len(drawings) > _MAX_PDF_DRAWINGS_PER_PAGE
        or not isinstance(image_infos, (list, tuple))
        or len(image_infos) != len(image_blocks)
    ):
        return None

    layers: list[_MrcImageLayer] = []
    for image_ordinal, ((block_index, block), image_info) in enumerate(
        zip(image_blocks, image_infos, strict=True)
    ):
        if not isinstance(image_info, dict):
            return None
        raw_bbox = _native_bbox(block.get("bbox"))
        info_bbox = _native_bbox(image_info.get("bbox"))
        raw_transform = _pdf_image_transform(block.get("transform"))
        info_transform = _pdf_image_transform(image_info.get("transform"))
        xref = image_info.get("xref")
        if (
            raw_bbox is None
            or info_bbox is None
            or not _mrc_bboxes_match(raw_bbox, info_bbox)
            or raw_transform is None
            or info_transform is None
            or not _mrc_transforms_match(raw_transform, info_transform)
            or image_info.get("number") != image_ordinal
            or isinstance(xref, bool)
            or not isinstance(xref, int)
            or xref <= 0
            or block.get("mask") is not None
            or image_info.get("has-mask") is not False
        ):
            return None
        width = block.get("width")
        height = block.get("height")
        bits_per_component = block.get("bpc")
        raw_colorspace_components = block.get("colorspace")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
            or isinstance(bits_per_component, bool)
            or not isinstance(bits_per_component, int)
            or bits_per_component <= 0
            or image_info.get("width") != width
            or image_info.get("height") != height
            or image_info.get("bpc") != bits_per_component
            or not _xref_key_equals(document, xref, "Subtype", "name", "/Image")
            or _mupdf_positive_integer_key(
                document=document,
                xref=xref,
                key="Width",
            )
            != width
            or _mupdf_positive_integer_key(
                document=document,
                xref=xref,
                key="Height",
            )
            != height
            or _mupdf_positive_integer_key(
                document=document,
                xref=xref,
                key="BitsPerComponent",
            )
            != bits_per_component
            or not _xref_key_is_null(document=document, xref=xref, key="Mask")
            or not _xref_key_is_null(document=document, xref=xref, key="SMask")
        ):
            return None
        image_bytes = _image_bytes(block.get("image"))
        if image_bytes is None:
            return None

        is_stencil = _xref_key_equals(
            document,
            xref,
            "ImageMask",
            "bool",
            "true",
        )
        if is_stencil:
            if (
                bits_per_component != 1
                or raw_colorspace_components != 0
                or image_info.get("colorspace") != 0
                or not _xref_key_is_null(
                    document=document,
                    xref=xref,
                    key="ColorSpace",
                )
            ):
                return None
        elif (
            bits_per_component <= 1
            or isinstance(raw_colorspace_components, bool)
            or not isinstance(raw_colorspace_components, int)
            or raw_colorspace_components <= 0
            or image_info.get("colorspace") != raw_colorspace_components
            or not _xref_key_is_false_or_null(
                document=document,
                xref=xref,
                key="ImageMask",
            )
            or _xref_key_is_null(
                document=document,
                xref=xref,
                key="ColorSpace",
            )
        ):
            return None
        layers.append(
            _MrcImageLayer(
                block_index=block_index,
                xref=xref,
                bbox=raw_bbox,
                native_width=width,
                native_height=height,
                transform=raw_transform,
                content_sha256=hashlib.sha256(image_bytes).hexdigest(),
                is_stencil=is_stencil,
            )
        )

    base_layers = tuple(layer for layer in layers if not layer.is_stencil)
    stencil_layers = tuple(layer for layer in layers if layer.is_stencil)
    if (
        len(base_layers) != 1
        or not stencil_layers
        or layers[0] != base_layers[0]
        or not _is_full_page_image_bbox(
            image_bbox=_pdf_visual_bbox(
                base_layers[0].bbox,
                rotation_matrix=rotation_matrix,
            ),
            page_bbox=page_bbox,
        )
    ):
        return None
    # `get_image_info().number` and rawdict image order are paint occurrence
    # order. Requiring the base first proves that each ImageMask is an overlay,
    # not an underlay that would be hidden by the scan raster.
    if any(layer.block_index <= base_layers[0].block_index for layer in stencil_layers):
        return None
    orthogonal_drawing_count = _proven_mrc_orthogonal_drawing_count(
        drawings,
        page_bbox=page_bbox,
        rotation_matrix=rotation_matrix,
    )
    if orthogonal_drawing_count is None:
        return None
    if orthogonal_drawing_count and (
        base_layers[0].native_width != 1
        or base_layers[0].native_height != 1
        or any(
            not _mrc_bboxes_match(base_layers[0].bbox, layer.bbox)
            or not _mrc_transforms_match(
                base_layers[0].transform,
                layer.transform,
            )
            for layer in stencil_layers
        )
    ):
        # A proven white page repair remains orthogonal to the scan stack only
        # under the strongest observed page-canvas form: a 1x1 base and
        # same-transform ImageMask overlays.
        return None
    return _MrcPageCompositePlan(
        base_layer=base_layers[0],
        stencil_layers=stencil_layers,
        preserved_text_block_indexes=tuple(preserved_text_block_indexes),
        orthogonal_drawing_count=orthogonal_drawing_count,
    )


def _proven_mrc_orthogonal_drawing_count(
    drawings: Any,
    *,
    page_bbox: tuple[float, float, float, float],
    rotation_matrix: object,
) -> int | None:
    """Accept only small opaque white rectangle repairs beside an MRC stack."""

    if not isinstance(drawings, (list, tuple)):
        return None
    if not drawings:
        return 0
    if len(drawings) > _MRC_ORTHOGONAL_WHITE_RECT_MAX_COUNT:
        return None
    page_area = _bbox_area(page_bbox)
    if page_area <= 0.0:
        return None
    total_area = 0.0
    for drawing in drawings:
        if not isinstance(drawing, dict):
            return None
        drawing_type = str(drawing.get("type") or "").lower()
        if drawing_type not in {"f", "fs"}:
            return None
        items = drawing.get("items")
        if (
            not isinstance(items, (list, tuple))
            or len(items) != 1
            or not isinstance(items[0], (list, tuple))
            or len(items[0]) < 2
            or items[0][0] != "re"
        ):
            return None
        drawing_bbox = _pdf_visual_bbox(
            _native_bbox(drawing.get("rect") or drawing.get("bbox")),
            rotation_matrix=rotation_matrix,
        )
        item_bbox = _pdf_visual_bbox(
            _native_bbox(items[0][1]),
            rotation_matrix=rotation_matrix,
        )
        if (
            drawing_bbox is None
            or item_bbox is None
            or not _mrc_bboxes_match(drawing_bbox, item_bbox)
            or _positive_bbox_intersection(drawing_bbox, page_bbox) != drawing_bbox
            or not _mrc_white_color(drawing.get("fill"))
        ):
            return None
        try:
            fill_opacity = float(drawing.get("fill_opacity"))
            stroke_opacity = (
                float(drawing.get("stroke_opacity"))
                if "s" in drawing_type
                else 1.0
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            not math.isclose(fill_opacity, 1.0, rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(stroke_opacity, 1.0, rel_tol=0.0, abs_tol=1e-9)
            or ("s" in drawing_type and not _mrc_white_color(drawing.get("color")))
        ):
            return None
        total_area += _bbox_area(drawing_bbox)
        if (
            total_area / page_area
            > _MRC_ORTHOGONAL_WHITE_RECT_MAX_PAGE_AREA_RATIO
        ):
            return None
    return len(drawings)


def _mrc_white_color(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) not in {1, 3, 4}:
        return False
    try:
        channels = tuple(float(channel) for channel in value)
    except (TypeError, ValueError, OverflowError):
        return False
    return all(
        math.isclose(channel, 1.0, rel_tol=0.0, abs_tol=1e-9)
        for channel in channels
    )


def _mrc_bboxes_match(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    left_center = ((left[0] + left[2]) / 2.0, (left[1] + left[3]) / 2.0)
    right_center = ((right[0] + right[2]) / 2.0, (right[1] + right[3]) / 2.0)
    if any(
        abs(left_value - right_value) > _MRC_CROSS_API_MAX_CENTER_DELTA_POINTS
        for left_value, right_value in zip(left_center, right_center, strict=True)
    ):
        return False
    intersection = _bbox_intersection(left, right)
    if intersection is None:
        return False
    intersection_area = _bbox_area(intersection)
    union_area = _bbox_area(left) + _bbox_area(right) - intersection_area
    return (
        union_area > 0.0
        and intersection_area / union_area >= _MRC_CROSS_API_MIN_BBOX_IOU
    )


def _mrc_transforms_match(
    left: tuple[float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float],
) -> bool:
    return all(
        math.isclose(
            left_value,
            right_value,
            rel_tol=0.0,
            abs_tol=_MRC_TRANSFORM_ABSOLUTE_TOLERANCE,
        )
        for left_value, right_value in zip(left, right, strict=True)
    )


def _xref_key_is_false_or_null(
    *,
    document: Any,
    xref: int,
    key: str,
) -> bool:
    try:
        value_type, value = document.xref_get_key(xref, key)
    except MemoryError:
        raise
    except Exception:
        return False
    return (value_type, value) in {("null", "null"), ("bool", "false")}


def _has_unlocated_image_blocks(blocks: Any) -> bool:
    return any(
        isinstance(block, dict)
        and block.get("type") == 1
        and _native_bbox(block.get("bbox")) is None
        for block in blocks
    )


def _unlocated_image_content_sha256s(blocks: Any) -> frozenset[str]:
    content_sha256s: set[str] = set()
    for block in blocks:
        if (
            not isinstance(block, dict)
            or block.get("type") != 1
            or _native_bbox(block.get("bbox")) is not None
        ):
            continue
        image_bytes = _image_bytes(block.get("image"))
        if image_bytes is not None:
            content_sha256s.add(hashlib.sha256(image_bytes).hexdigest())
    return frozenset(content_sha256s)


def _has_possible_inline_header_logo_fragments(blocks: Any) -> bool:
    if not isinstance(blocks, (list, tuple)):
        return False
    image_blocks = tuple(
        block for block in blocks if isinstance(block, dict) and block.get("type") == 1
    )
    if not (
        _MIN_PDF_INLINE_HEADER_LOGO_FRAGMENTS
        <= len(image_blocks)
        <= _MAX_PDF_INLINE_HEADER_LOGO_FRAGMENTS
    ):
        return False
    for block in image_blocks:
        bbox = _native_bbox(block.get("bbox"))
        width = block.get("width")
        height = block.get("height")
        if (
            bbox is None
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width < 1
            or width > _MAX_PDF_INLINE_HEADER_LOGO_INTRINSIC_WIDTH
            or height != 1
            or block.get("bpc") != 8
            or block.get("colorspace") != 3
            or block.get("ext") != "png"
            or block.get("mask") is not None
            or bbox[2] - bbox[0] > _MAX_PDF_INLINE_HEADER_LOGO_PLACEMENT_WIDTH_POINTS
            or bbox[3] - bbox[1] > _MAX_PDF_INLINE_HEADER_LOGO_PLACEMENT_HEIGHT_POINTS
        ):
            return False
    return True


def _proven_inline_header_logo_fragments(
    *,
    document: Any,
    reader: PdfReader,
    page: Any,
    page_index: int,
    page_number: int,
    page_bbox: tuple[float, float, float, float] | None,
    rotation_matrix: Any,
    blocks: Any,
    source_sha256: str,
    budget: _PdfPatternAnalysisBudget,
) -> dict[int, str]:
    """Prove a compact header logo split into direct inline-image scanlines."""

    if (
        page_bbox is None
        or _bbox_area(page_bbox) <= 0.0
        or not _has_possible_inline_header_logo_fragments(blocks)
        or budget.exhausted
    ):
        return {}
    image_entries: list[
        tuple[
            int,
            dict[str, Any],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float, float, float],
            int,
            bytes,
        ]
    ] = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 1:
            continue
        native_bbox = _native_bbox(block.get("bbox"))
        visible_bbox = _pdf_visual_bbox(
            native_bbox,
            rotation_matrix=rotation_matrix,
        )
        transform = _inline_image_transform(block.get("transform"))
        image_bytes = _image_bytes(block.get("image"))
        width = block.get("width")
        if (
            native_bbox is None
            or visible_bbox is None
            or transform is None
            or image_bytes is None
            or isinstance(width, bool)
            or not isinstance(width, int)
        ):
            return {}
        try:
            pixmap = pymupdf.Pixmap(image_bytes)
            if (
                pixmap.width != width
                or pixmap.height != 1
                or pixmap.alpha
                or pixmap.colorspace is None
                or pixmap.colorspace.n != 3
                or pixmap.n != 3
                or pixmap.stride < width * 3
            ):
                return {}
            decoded_pixels = bytes(pixmap.samples[: width * 3])
        except MemoryError:
            raise
        except Exception:
            return {}
        if len(decoded_pixels) != width * 3:
            return {}
        image_entries.append(
            (
                block_index,
                block,
                native_bbox,
                visible_bbox,
                transform,
                width,
                decoded_pixels,
            )
        )
    occurrence_count = len(image_entries)
    if not (
        _MIN_PDF_INLINE_HEADER_LOGO_FRAGMENTS
        <= occurrence_count
        <= _MAX_PDF_INLINE_HEADER_LOGO_FRAGMENTS
    ):
        return {}

    union_bbox = _bbox_union(tuple(entry[3] for entry in image_entries))
    if union_bbox is None:
        return {}
    page_width = page_bbox[2] - page_bbox[0]
    page_height = page_bbox[3] - page_bbox[1]
    union_width = union_bbox[2] - union_bbox[0]
    union_height = union_bbox[3] - union_bbox[1]
    if (
        page_width <= 0.0
        or page_height <= 0.0
        or union_bbox[3]
        > page_bbox[1] + page_height * _MAX_PDF_INLINE_HEADER_LOGO_BAND_RATIO
        or union_width > page_width * _MAX_PDF_INLINE_HEADER_LOGO_WIDTH_RATIO
        or union_height > page_height * _MAX_PDF_INLINE_HEADER_LOGO_HEIGHT_RATIO
        or _bbox_area(union_bbox)
        > _bbox_area(page_bbox) * _MAX_PDF_INLINE_HEADER_LOGO_AREA_RATIO
    ):
        return {}

    try:
        page_images = page.get_images(full=True)
        raw_image_infos = page.get_image_info(hashes=True, xrefs=True)
    except MemoryError:
        raise
    except Exception:
        return {}
    if (
        not isinstance(page_images, (list, tuple))
        or page_images
        or not isinstance(raw_image_infos, (list, tuple))
        or len(raw_image_infos) != occurrence_count
    ):
        return {}
    for entry, info in zip(image_entries, raw_image_infos, strict=True):
        (
            block_index,
            _block,
            native_bbox,
            _visible_bbox,
            transform,
            width,
            decoded_pixels,
        ) = entry
        if not isinstance(info, dict):
            return {}
        info_bbox = _native_bbox(info.get("bbox"))
        info_transform = _inline_image_transform(info.get("transform"))
        digest = info.get("digest")
        if (
            info.get("number") != block_index
            or info.get("xref") != 0
            or info.get("width") != width
            or info.get("height") != 1
            or info.get("colorspace") != 3
            or info.get("cs-name") != "DeviceRGB"
            or info.get("bpc") != 8
            or info.get("has-mask") is not False
            or info_bbox is None
            or info_transform is None
            or not _inline_raw_bbox_matches_info_bbox(native_bbox, info_bbox)
            or not _float_sequences_match(transform, info_transform)
            or not isinstance(digest, (bytes, bytearray))
            or bytes(digest)
            != hashlib.md5(decoded_pixels, usedforsecurity=False).digest()
        ):
            return {}

    try:
        pypdf_page = reader.pages[page_index]
        page_content = pypdf_page.get_contents()
        operations = tuple(ContentStream(page_content, reader).operations)
    except MemoryError:
        raise
    except Exception:
        return {}
    if len(
        operations
    ) > _MAX_PDF_INLINE_HEADER_LOGO_OPERATIONS or not _pdf_page_content_streams_match(
        document=document,
        page=page,
        pypdf_page=pypdf_page,
        budget=budget,
    ):
        return {}

    inline_records: list[tuple[int, bytes, str, int]] = []
    encoded_byte_count = 0
    inline_positions = tuple(
        index
        for index, (_operands, operator) in enumerate(operations)
        if operator == b"INLINE IMAGE"
    )
    if len(inline_positions) != occurrence_count:
        return {}
    for position in inline_positions:
        if position < 2 or position + 1 >= len(operations):
            return {}
        q_operands, q_operator = operations[position - 2]
        cm_operands, cm_operator = operations[position - 1]
        inline_operands, _inline_operator = operations[position]
        q_close_operands, q_close_operator = operations[position + 1]
        if (
            q_operator != b"q"
            or q_operands
            or cm_operator != b"cm"
            or q_close_operator != b"Q"
            or q_close_operands
            or not isinstance(inline_operands, dict)
        ):
            return {}
        settings = inline_operands.get("settings")
        data = inline_operands.get("data")
        if not isinstance(settings, DictionaryObject) or not isinstance(data, bytes):
            return {}
        width = _positive_pdf_integer(settings.get("/W"))
        if (
            width is None
            or width > _MAX_PDF_INLINE_HEADER_LOGO_INTRINSIC_WIDTH
            or _positive_pdf_integer(settings.get("/H")) != 1
            or _positive_pdf_integer(settings.get("/BPC")) != 8
            or str(settings.get("/CS")) != "/RGB"
        ):
            return {}
        cm = _inline_image_transform(cm_operands)
        if (
            cm is None
            or abs(cm[1]) > 1e-9
            or abs(cm[2]) > 1e-9
            or cm[0] <= 0.0
            or cm[3] <= 0.0
            or not math.isclose(
                cm[0] / width,
                cm[3],
                rel_tol=1e-9,
                abs_tol=1e-9,
            )
        ):
            return {}
        encoded_byte_count += len(data)
        if encoded_byte_count > _MAX_PDF_INLINE_HEADER_LOGO_ENCODED_BYTES:
            return {}
        decoded = _decode_inline_rgb_scanline(
            settings=settings,
            data=data,
            width=width,
        )
        if decoded is None:
            return {}
        profile = "flate_png_predictor_15" if settings.get("/F") is not None else "raw"
        inline_records.append((width, decoded, profile, position))

    raw_records = tuple((entry[5], entry[6]) for entry in image_entries)
    if tuple((width, decoded) for width, decoded, _profile, _ in inline_records) != (
        raw_records
    ):
        return {}

    evidence_payload = {
        "schema": "pdf-inline-header-logo-fragments-proof/v1",
        "source_sha256": source_sha256,
        "page_number": page_number,
        "page_xref": int(page.xref),
        "union_bbox": _rounded_bbox(union_bbox),
        "occurrence_count": occurrence_count,
        "encoded_byte_count": encoded_byte_count,
        "occurrences": [
            {
                "block_index": entry[0],
                "bbox": _rounded_bbox(entry[3]),
                "transform": [round(value, 6) for value in entry[4]],
                "width": entry[5],
                "decoded_sha256": hashlib.sha256(entry[6]).hexdigest(),
                "content_operation_index": inline_record[3],
                "profile": inline_record[2],
            }
            for entry, inline_record in zip(
                image_entries,
                inline_records,
                strict=True,
            )
        ],
        "disposition": "DROP_STANDALONE_IMAGE_KEEP_PAGE_RENDER",
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {entry[0]: evidence_sha256 for entry in image_entries}


def _inline_image_transform(
    value: Any,
) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        return None
    try:
        transform = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(item) for item in transform):
        return None
    return transform  # type: ignore[return-value]


def _float_sequences_match(
    left: tuple[float, ...],
    right: tuple[float, ...],
    *,
    tolerance: float = 0.001,
) -> bool:
    return len(left) == len(right) and all(
        abs(first - second) <= tolerance
        for first, second in zip(left, right, strict=True)
    )


def _inline_raw_bbox_matches_info_bbox(
    raw_bbox: tuple[float, float, float, float],
    info_bbox: tuple[float, float, float, float],
) -> bool:
    """Allow only MuPDF's exact right/bottom 0.12pt inline clip."""

    if (
        abs(raw_bbox[0] - info_bbox[0]) > 0.001
        or abs(raw_bbox[1] - info_bbox[1]) > 0.001
    ):
        return False
    right_clip = info_bbox[2] - raw_bbox[2]
    bottom_clip = info_bbox[3] - raw_bbox[3]
    return (
        -0.001 <= right_clip <= 0.121
        and -0.001 <= bottom_clip <= 0.121
        and raw_bbox[2] > raw_bbox[0]
        and raw_bbox[3] > raw_bbox[1]
    )


def _positive_pdf_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return integer if integer > 0 and float(value) == integer else None


def _decode_inline_rgb_scanline(
    *,
    settings: DictionaryObject,
    data: bytes,
    width: int,
) -> bytes | None:
    common_keys = {"/CS", "/W", "/H", "/BPC"}
    setting_keys = {str(key) for key in settings.keys()}
    expected_byte_count = width * 3
    if setting_keys == common_keys:
        return data if len(data) == expected_byte_count else None
    if setting_keys != common_keys | {"/F", "/DP"}:
        return None
    if str(settings.get("/F")) != "/Fl":
        return None
    decode_parameters = settings.get("/DP")
    if not isinstance(decode_parameters, DictionaryObject) or {
        str(key) for key in decode_parameters.keys()
    } != {"/Predictor", "/Columns", "/Colors"}:
        return None
    if (
        _positive_pdf_integer(decode_parameters.get("/Predictor")) != 15
        or _positive_pdf_integer(decode_parameters.get("/Columns")) != width
        or _positive_pdf_integer(decode_parameters.get("/Colors")) != 3
    ):
        return None
    try:
        decompressor = zlib.decompressobj()
        predicted = decompressor.decompress(data, expected_byte_count + 2)
    except MemoryError:
        raise
    except zlib.error:
        return None
    if (
        len(predicted) != expected_byte_count + 1
        or not decompressor.eof
        or len(decompressor.unused_data) > 8
        or decompressor.unused_data.strip(b"\x00\t\n\f\r ")
        or decompressor.unconsumed_tail
    ):
        return None
    filter_type = predicted[0]
    if filter_type not in {0, 1, 2, 3, 4}:
        return None
    filtered = predicted[1:]
    if filter_type in {0, 2}:
        return filtered
    decoded = bytearray(expected_byte_count)
    for index, value in enumerate(filtered):
        left = decoded[index - 3] if index >= 3 else 0
        if filter_type in {1, 4}:
            predictor = left
        else:
            predictor = left // 2
        decoded[index] = (value + predictor) & 0xFF
    return bytes(decoded)


def _open_pdf_pattern_reader(source_bytes: bytes) -> PdfReader | None:
    """Open the same verified bytes for narrow PDF resource-lineage checks."""

    try:
        reader = PdfReader(BytesIO(source_bytes), strict=False)
        if reader.is_encrypted:
            try:
                password_type = reader.decrypt("")
            except MemoryError:
                raise
            except Exception:
                return None
            if password_type is PasswordType.NOT_DECRYPTED:
                return None
        return reader
    except MemoryError:
        raise
    except Exception:
        return None


def _proven_tiling_pattern_tile_hashes_by_block_index(
    *,
    document: Any,
    reader: PdfReader,
    page: Any,
    page_index: int,
    blocks: Any,
    analysis_state: _PdfPatternAnalysisState,
) -> dict[int, str]:
    """Identify malformed rawdict images proven to be internal pattern tiles.

    MuPDF can expose an image used inside a PDF tiling-pattern stream as if it
    were a standalone page image and can assign that synthetic occurrence an
    unusable bbox.  The tile transform describes the repeating tile, not the
    painted page region, so it must not be used as a locator fallback.  We only
    exclude a candidate when two parsers agree on page identity, the page
    content actually paints a PatternType 1 resource, that pattern directly
    invokes an Image XObject with the same bytes, and occurrence counts are
    exact.  Every ambiguity returns an empty set and preserves the existing
    fail-closed bbox diagnostic.
    """

    invalid_by_hash: dict[str, list[int]] = {}
    hashes_with_valid_placement: set[str] = set()
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != 1:
            continue
        image_bytes = _image_bytes(block.get("image"))
        if image_bytes is None or block.get("mask") is not None:
            continue
        content_sha256 = hashlib.sha256(image_bytes).hexdigest()
        if _native_bbox(block.get("bbox")) is None:
            invalid_by_hash.setdefault(content_sha256, []).append(block_index)
        else:
            hashes_with_valid_placement.add(content_sha256)
    if not invalid_by_hash:
        return {}

    occurrence_counts = _tiling_pattern_image_occurrence_counts(
        document=document,
        reader=reader,
        page=page,
        page_index=page_index,
        analysis_state=analysis_state,
    )
    if occurrence_counts is None:
        return {}

    proven: dict[int, str] = {}
    for content_sha256, block_indices in invalid_by_hash.items():
        if content_sha256 in hashes_with_valid_placement:
            continue
        if occurrence_counts.get(content_sha256, 0) == len(block_indices):
            proven.update(
                (block_index, content_sha256) for block_index in block_indices
            )
    return proven


def _tiling_pattern_image_occurrence_counts(
    *,
    document: Any,
    reader: PdfReader,
    page: Any,
    page_index: int,
    analysis_state: _PdfPatternAnalysisState,
) -> Counter[str] | None:
    if analysis_state.budget.exhausted:
        return None
    try:
        pypdf_page = reader.pages[page_index]
        page_reference = pypdf_page.indirect_reference
        page_xref = int(page.xref)
    except MemoryError:
        raise
    except Exception:
        return None
    if (
        not isinstance(page_reference, IndirectObject)
        or page_reference.idnum != page_xref
    ):
        return None

    if not _pdf_page_content_streams_match(
        document=document,
        page=page,
        pypdf_page=pypdf_page,
        budget=analysis_state.budget,
    ):
        return None

    resources = _resolved_pdf_dictionary(pypdf_page.get("/Resources"))
    if resources is None:
        return None
    patterns = _resolved_pdf_dictionary(resources.get("/Pattern"))
    if patterns is None:
        return Counter()
    if len(patterns) > _MAX_PDF_PATTERN_RESOURCES_PER_PAGE:
        return None
    pattern_color_space_names = _pattern_color_space_names(resources)
    if pattern_color_space_names is None:
        return None

    try:
        page_content = pypdf_page.get_contents()
        operations = tuple(ContentStream(page_content, reader).operations)
    except MemoryError:
        raise
    except Exception:
        return None
    if any(operator == b"INLINE IMAGE" for _, operator in operations):
        return None
    pattern_names = frozenset(str(name) for name in patterns.keys())
    paint_counts = _pattern_paint_counts(
        operations=operations,
        pattern_names=pattern_names,
        pattern_color_space_names=pattern_color_space_names,
    )
    if paint_counts is None:
        return None

    direct_image_hashes = _direct_page_image_hashes(
        document=document,
        page=page,
        budget=analysis_state.budget,
        image_evidence_by_xref=analysis_state.image_evidence_by_xref,
    )
    if direct_image_hashes is None:
        return None

    occurrences: Counter[str] = Counter()
    for pattern_name, paint_count in paint_counts.items():
        if analysis_state.budget.exhausted:
            return None
        if paint_count <= 0:
            continue
        try:
            raw_pattern = patterns.raw_get(NameObject(pattern_name))
        except MemoryError:
            raise
        except Exception:
            continue
        if not isinstance(raw_pattern, IndirectObject):
            continue
        pattern_hashes = _direct_pattern_image_hashes(
            document=document,
            reader=reader,
            pattern_reference=raw_pattern,
            budget=analysis_state.budget,
            image_evidence_by_xref=analysis_state.image_evidence_by_xref,
            uniform_opaque_by_xref=analysis_state.uniform_opaque_by_xref,
            pattern_image_hashes_by_xref=(analysis_state.pattern_image_hashes_by_xref),
        )
        if pattern_hashes is None:
            continue
        for content_sha256, count_per_paint in pattern_hashes.items():
            occurrences[content_sha256] += paint_count * count_per_paint

    for content_sha256 in direct_image_hashes:
        occurrences.pop(content_sha256, None)
    return occurrences


def _pdf_page_content_streams_match(
    *,
    document: Any,
    page: Any,
    pypdf_page: Any,
    budget: _PdfPatternAnalysisBudget,
) -> bool:
    try:
        content_xrefs = page.get_contents()
        raw_contents = pypdf_page.raw_get(NameObject("/Contents"))
    except MemoryError:
        raise
    except Exception:
        return False
    if not isinstance(content_xrefs, (list, tuple)):
        return False

    if isinstance(raw_contents, IndirectObject):
        try:
            resolved_contents = raw_contents.get_object()
        except MemoryError:
            raise
        except Exception:
            return False
        content_references = (
            tuple(resolved_contents)
            if isinstance(resolved_contents, ArrayObject)
            else (raw_contents,)
        )
    elif isinstance(raw_contents, ArrayObject):
        content_references = tuple(raw_contents)
    else:
        return False
    if len(content_xrefs) != len(content_references):
        return False

    for xref, content_reference in zip(
        content_xrefs,
        content_references,
        strict=True,
    ):
        if isinstance(xref, bool) or not isinstance(xref, int) or xref <= 0:
            return False
        if (
            not isinstance(content_reference, IndirectObject)
            or content_reference.idnum != xref
        ):
            return False
        try:
            mupdf_stream = document.xref_stream(xref)
            pypdf_stream = content_reference.get_object().get_data()
        except MemoryError:
            raise
        except Exception:
            return False
        if (
            not isinstance(mupdf_stream, bytes)
            or not isinstance(pypdf_stream, bytes)
            or mupdf_stream != pypdf_stream
            or not budget.consume_stream_bytes(len(mupdf_stream))
        ):
            return False
    return True


def _resolved_pdf_dictionary(value: Any) -> DictionaryObject | None:
    try:
        if isinstance(value, IndirectObject):
            value = value.get_object()
    except MemoryError:
        raise
    except Exception:
        return None
    return value if isinstance(value, DictionaryObject) else None


def _pattern_color_space_names(
    resources: DictionaryObject,
) -> frozenset[str] | None:
    names = {"/Pattern"}
    raw_color_spaces = resources.get("/ColorSpace")
    if raw_color_spaces is None:
        return frozenset(names)
    color_spaces = _resolved_pdf_dictionary(raw_color_spaces)
    if color_spaces is None or len(color_spaces) > _MAX_PDF_PATTERN_RESOURCES_PER_PAGE:
        return None
    for raw_name in color_spaces.keys():
        try:
            raw_color_space = color_spaces.raw_get(raw_name)
            if isinstance(raw_color_space, IndirectObject):
                raw_color_space = raw_color_space.get_object()
        except MemoryError:
            raise
        except Exception:
            return None
        if (
            isinstance(raw_color_space, ArrayObject)
            and raw_color_space
            and isinstance(raw_color_space[0], NameObject)
            and str(raw_color_space[0]) == "/Pattern"
        ):
            names.add(str(raw_name))
    return frozenset(names)


def _pattern_paint_counts(
    *,
    operations: tuple[tuple[Any, bytes], ...],
    pattern_names: frozenset[str],
    pattern_color_space_names: frozenset[str],
) -> Counter[str] | None:
    fill_pattern: str | None = None
    stroke_pattern: str | None = None
    fill_uses_pattern_color_space = False
    stroke_uses_pattern_color_space = False
    graphics_stack: list[tuple[str | None, str | None, bool, bool]] = []
    counts: Counter[str] = Counter()
    fill_operators = {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
    stroke_operators = {b"S", b"s", b"B", b"B*", b"b", b"b*"}

    for operands, operator in operations:
        if operator == b"q":
            graphics_stack.append(
                (
                    fill_pattern,
                    stroke_pattern,
                    fill_uses_pattern_color_space,
                    stroke_uses_pattern_color_space,
                )
            )
            continue
        if operator == b"Q":
            if not graphics_stack:
                return None
            (
                fill_pattern,
                stroke_pattern,
                fill_uses_pattern_color_space,
                stroke_uses_pattern_color_space,
            ) = graphics_stack.pop()
            continue
        if operator == b"cs":
            fill_pattern = None
            selected = _selected_pattern_color_space(
                operands,
                pattern_color_space_names,
            )
            if selected is None:
                return None
            fill_uses_pattern_color_space = selected
            continue
        if operator == b"CS":
            stroke_pattern = None
            selected = _selected_pattern_color_space(
                operands,
                pattern_color_space_names,
            )
            if selected is None:
                return None
            stroke_uses_pattern_color_space = selected
            continue
        if operator == b"scn":
            fill_pattern = (
                _selected_pattern_name(operands, pattern_names)
                if fill_uses_pattern_color_space
                else None
            )
            continue
        if operator == b"SCN":
            stroke_pattern = (
                _selected_pattern_name(operands, pattern_names)
                if stroke_uses_pattern_color_space
                else None
            )
            continue
        if operator in {b"sc", b"g", b"rg", b"k"}:
            fill_pattern = None
            fill_uses_pattern_color_space = False
            continue
        if operator in {b"SC", b"G", b"RG", b"K"}:
            stroke_pattern = None
            stroke_uses_pattern_color_space = False
            continue
        if operator in fill_operators and fill_pattern is not None:
            counts[fill_pattern] += 1
        if operator in stroke_operators and stroke_pattern is not None:
            counts[stroke_pattern] += 1
    if graphics_stack:
        return None
    return counts


def _selected_pattern_color_space(
    operands: Any,
    pattern_color_space_names: frozenset[str],
) -> bool | None:
    if (
        not isinstance(operands, (list, tuple))
        or len(operands) != 1
        or not isinstance(operands[0], NameObject)
    ):
        return None
    return str(operands[0]) in pattern_color_space_names


def _selected_pattern_name(
    operands: Any,
    pattern_names: frozenset[str],
) -> str | None:
    if not isinstance(operands, (list, tuple)):
        return None
    matches = tuple(
        str(operand)
        for operand in operands
        if isinstance(operand, NameObject) and str(operand) in pattern_names
    )
    return matches[0] if len(matches) == 1 else None


def _direct_page_image_hashes(
    *,
    document: Any,
    page: Any,
    budget: _PdfPatternAnalysisBudget,
    image_evidence_by_xref: dict[int, _PdfPatternImageEvidence],
) -> set[str] | None:
    try:
        image_records = page.get_images(full=True)
    except MemoryError:
        raise
    except Exception:
        return None
    if not isinstance(image_records, (list, tuple)):
        return None
    if len(
        image_records
    ) > _MAX_PDF_PATTERN_ANALYSIS_XOBJECTS or not budget.consume_xobjects(
        len(image_records)
    ):
        return None
    hashes: set[str] = set()
    for record in image_records:
        if not isinstance(record, (list, tuple)) or not record:
            return None
        xref = record[0]
        if isinstance(xref, bool) or not isinstance(xref, int) or xref <= 0:
            return None
        evidence = _mupdf_pattern_image_evidence(
            document=document,
            xref=xref,
            budget=budget,
            image_evidence_by_xref=image_evidence_by_xref,
        )
        if evidence is None:
            return None
        hashes.add(evidence.content_sha256)
    return hashes


def _direct_pattern_image_hashes(
    *,
    document: Any,
    reader: PdfReader,
    pattern_reference: IndirectObject,
    budget: _PdfPatternAnalysisBudget,
    image_evidence_by_xref: dict[int, _PdfPatternImageEvidence],
    uniform_opaque_by_xref: dict[int, bool],
    pattern_image_hashes_by_xref: dict[int, Counter[str] | None],
) -> Counter[str] | None:
    pattern_xref = pattern_reference.idnum
    if budget.exhausted:
        return None
    if pattern_xref in pattern_image_hashes_by_xref:
        cached = pattern_image_hashes_by_xref[pattern_xref]
        return Counter(cached) if cached is not None else None
    pattern_image_hashes_by_xref[pattern_xref] = None
    if not _xref_key_equals(document, pattern_xref, "Type", "name", "/Pattern"):
        return None
    if not _xref_key_equals(document, pattern_xref, "PatternType", "int", "1"):
        return None
    if not _xref_key_equals(document, pattern_xref, "PaintType", "int", "1"):
        return None
    try:
        pattern = pattern_reference.get_object()
        pattern_stream = document.xref_stream(pattern_xref)
        pypdf_pattern_stream = pattern.get_data()
    except MemoryError:
        raise
    except Exception:
        return None
    if (
        not isinstance(pattern_stream, bytes)
        or not isinstance(pypdf_pattern_stream, bytes)
        or pattern_stream != pypdf_pattern_stream
        or not budget.consume_stream_bytes(len(pattern_stream))
    ):
        return None
    pattern_resources = _resolved_pdf_dictionary(pattern.get("/Resources"))
    if pattern_resources is None:
        return None
    xobjects = _resolved_pdf_dictionary(pattern_resources.get("/XObject"))
    if (
        xobjects is None
        or len(xobjects) > _MAX_PDF_PATTERN_XOBJECTS_PER_PATTERN
        or not budget.consume_xobjects(len(xobjects))
    ):
        return None
    try:
        operations = tuple(ContentStream(pattern, reader).operations)
    except MemoryError:
        raise
    except Exception:
        return None
    if any(operator == b"INLINE IMAGE" for _, operator in operations):
        return None

    invoked_names: Counter[str] = Counter()
    for operands, operator in operations:
        if operator != b"Do" or not isinstance(operands, (list, tuple)):
            continue
        names = tuple(
            str(operand) for operand in operands if isinstance(operand, NameObject)
        )
        if len(names) != 1:
            return None
        invoked_names[names[0]] += 1
    if not invoked_names:
        return None

    hashes: Counter[str] = Counter()
    for xobject_name, invocation_count in invoked_names.items():
        try:
            raw_xobject = xobjects.raw_get(NameObject(xobject_name))
        except MemoryError:
            raise
        except Exception:
            return None
        if not isinstance(raw_xobject, IndirectObject):
            return None
        xref = raw_xobject.idnum
        if not _xref_key_equals(document, xref, "Type", "name", "/XObject"):
            return None
        if not _xref_key_equals(document, xref, "Subtype", "name", "/Image"):
            return None
        content_sha256 = _uniform_opaque_pattern_image_sha256(
            document=document,
            xref=xref,
            budget=budget,
            image_evidence_by_xref=image_evidence_by_xref,
            uniform_opaque_by_xref=uniform_opaque_by_xref,
        )
        if content_sha256 is None:
            return None
        hashes[content_sha256] += invocation_count
    pattern_image_hashes_by_xref[pattern_xref] = Counter(hashes)
    return hashes


def _xref_key_equals(
    document: Any,
    xref: int,
    key: str,
    expected_type: str,
    expected_value: str,
) -> bool:
    try:
        value_type, value = document.xref_get_key(xref, key)
    except MemoryError:
        raise
    except Exception:
        return False
    return value_type == expected_type and value == expected_value


def _mupdf_pattern_image_evidence(
    *,
    document: Any,
    xref: int,
    budget: _PdfPatternAnalysisBudget,
    image_evidence_by_xref: dict[int, _PdfPatternImageEvidence],
) -> _PdfPatternImageEvidence | None:
    cached = image_evidence_by_xref.get(xref)
    if cached is not None:
        return cached
    width = _mupdf_positive_integer_key(document=document, xref=xref, key="Width")
    height = _mupdf_positive_integer_key(
        document=document,
        xref=xref,
        key="Height",
    )
    if (
        width is None
        or height is None
        or not budget.consume_image_pixels(width * height)
    ):
        return None
    try:
        extracted = document.extract_image(xref)
    except MemoryError:
        raise
    except Exception:
        return None
    if not isinstance(extracted, dict):
        return None
    image_bytes = _image_bytes(extracted.get("image"))
    if image_bytes is None or not budget.consume_image_bytes(len(image_bytes)):
        return None
    evidence = _PdfPatternImageEvidence(
        content_sha256=hashlib.sha256(image_bytes).hexdigest(),
        width=width,
        height=height,
    )
    image_evidence_by_xref[xref] = evidence
    return evidence


def _uniform_opaque_pattern_image_sha256(
    *,
    document: Any,
    xref: int,
    budget: _PdfPatternAnalysisBudget,
    image_evidence_by_xref: dict[int, _PdfPatternImageEvidence],
    uniform_opaque_by_xref: dict[int, bool],
) -> str | None:
    evidence = _mupdf_pattern_image_evidence(
        document=document,
        xref=xref,
        budget=budget,
        image_evidence_by_xref=image_evidence_by_xref,
    )
    if (
        evidence is None
        or evidence.width * evidence.height > _MAX_PDF_PATTERN_TILE_PIXELS
    ):
        return None
    cached_uniform = uniform_opaque_by_xref.get(xref)
    if cached_uniform is not None:
        return evidence.content_sha256 if cached_uniform else None
    if not _xref_key_is_null(document=document, xref=xref, key="Mask") or not (
        _xref_key_is_null(document=document, xref=xref, key="SMask")
    ):
        uniform_opaque_by_xref[xref] = False
        return None
    try:
        pixmap = pymupdf.Pixmap(document, xref)
        width = int(pixmap.width)
        height = int(pixmap.height)
        component_count = int(pixmap.n)
        has_alpha = bool(pixmap.alpha)
        samples = bytes(pixmap.samples)
    except MemoryError:
        raise
    except Exception:
        uniform_opaque_by_xref[xref] = False
        return None
    pixel_count = evidence.width * evidence.height
    if (
        width != evidence.width
        or height != evidence.height
        or component_count <= 0
        or component_count > 5
        or len(samples) != pixel_count * component_count
        or not budget.consume_image_bytes(len(samples))
    ):
        uniform_opaque_by_xref[xref] = False
        return None
    first_pixel = samples[:component_count]
    is_uniform_opaque = True
    for offset in range(0, len(samples), component_count):
        pixel = samples[offset : offset + component_count]
        if pixel != first_pixel or (has_alpha and pixel[-1] != 255):
            is_uniform_opaque = False
            break
    uniform_opaque_by_xref[xref] = is_uniform_opaque
    return evidence.content_sha256 if is_uniform_opaque else None


def _mupdf_positive_integer_key(
    *,
    document: Any,
    xref: int,
    key: str,
) -> int | None:
    try:
        value_type, raw_value = document.xref_get_key(xref, key)
        value = int(raw_value)
    except (TypeError, ValueError, OverflowError, RuntimeError):
        return None
    return value if value_type == "int" and value > 0 else None


def _xref_key_is_null(*, document: Any, xref: int, key: str) -> bool:
    try:
        value_type, value = document.xref_get_key(xref, key)
    except MemoryError:
        raise
    except Exception:
        return False
    return value_type == "null" and value == "null"


def _native_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)):
        try:
            value = (value.x0, value.y0, value.x1, value.y1)
        except (AttributeError, TypeError, ValueError):
            return None
    if len(value) != 4:
        return None
    try:
        bbox = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in bbox):
        return None
    x0, y0, x1, y1 = bbox
    if x1 < x0 or y1 < y0:
        return None
    return x0, y0, x1, y1


def _pdf_variable_page_marker_kinds(
    document: Any,
    *,
    resource_budget: _PdfResourceBudget,
) -> dict[tuple[int, int], SourceNodeKind]:
    """Run the optional furniture prepass with the adapter's budget contract."""

    try:
        return _scan_pdf_variable_page_marker_kinds(
            document,
            resource_budget=resource_budget,
        )
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc


def _scan_pdf_variable_page_marker_kinds(
    document: Any,
    *,
    resource_budget: _PdfResourceBudget,
) -> dict[tuple[int, int], SourceNodeKind]:
    """Convert native rawdict blocks to the shared page-furniture proof."""

    text_candidates: list[PdfPageFurnitureCandidate[tuple[int, int]]] = []
    image_candidates: list[PdfPageFurnitureImageCandidate[tuple[int, int]]] = []
    for candidate in _native_pdf_page_furniture_candidates(
        document,
        resource_budget=resource_budget,
    ):
        if isinstance(candidate, PdfPageFurnitureImageCandidate):
            image_candidates.append(candidate)
        else:
            text_candidates.append(candidate)
    proven = classify_pdf_page_furniture(text_candidates)
    proven.update(
        classify_pdf_repeated_image_furniture(image_candidates)
    )
    return {
        candidate_id: (
            SourceNodeKind.HEADER
            if kind is PdfPageFurnitureKind.HEADER
            else SourceNodeKind.FOOTER
        )
        for candidate_id, kind in proven.items()
    }


def _native_pdf_page_furniture_candidates(
    document: Any,
    *,
    resource_budget: _PdfResourceBudget,
) -> Iterator[
    PdfPageFurnitureCandidate[tuple[int, int]]
    | PdfPageFurnitureImageCandidate[tuple[int, int]]
]:
    """Stream rawdict candidates; the domain proof retains margin data only."""

    try:
        page_count = int(document.page_count)
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        return
    image_hash_bytes = 0
    for page_index in range(page_count):
        try:
            page = document.load_page(page_index)
            raw_page = page.get_text("rawdict", sort=False)
            blocks = raw_page.get("blocks") if isinstance(raw_page, dict) else None
            if not isinstance(blocks, (list, tuple)):
                continue
            if len(blocks) > _MAX_PDF_BLOCKS_PER_PAGE:
                raise _PdfResourceBudgetExceeded
            resource_budget.consume_page_furniture_scan_blocks(len(blocks))
            page_bbox = _native_bbox(page.rect)
            page_rotation_matrix = getattr(page, "rotation_matrix", None)
        except _PdfResourceBudgetExceeded:
            raise
        except MemoryError as exc:
            raise _PdfResourceBudgetExceeded from exc
        except Exception:
            continue
        if page_bbox is None:
            continue
        page_x0, page_y0, page_x1, page_y1 = page_bbox
        if page_x1 <= page_x0 or page_y1 <= page_y0:
            continue
        page_number = page_index + 1
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict) or block.get("type") not in {0, 1}:
                continue
            bbox = _raw_text_bbox_from_block(block) or _native_bbox(block.get("bbox"))
            bbox = _pdf_visual_bbox(
                bbox,
                rotation_matrix=page_rotation_matrix,
            )
            if bbox is None or pdf_page_furniture_margin_kind(page_bbox, bbox) is None:
                continue
            if block.get("type") == 1:
                image_content = _pdf_image_furniture_content_identity(
                    block,
                    max_content_bytes=(
                        _MAX_PDF_PAGE_FURNITURE_IMAGE_HASH_BYTES
                        - image_hash_bytes
                    ),
                )
                if image_content is None:
                    continue
                content_sha256, content_bytes = image_content
                image_hash_bytes += content_bytes
                yield PdfPageFurnitureImageCandidate(
                    candidate_id=(page_number, block_index),
                    page_number=page_number,
                    page_order=block_index,
                    content_sha256=content_sha256,
                    bbox=bbox,
                    page_bbox=page_bbox,
                )
                continue
            raw_text = _raw_text_from_block(block)
            if not raw_text.strip():
                continue
            yield PdfPageFurnitureCandidate(
                candidate_id=(page_number, block_index),
                page_number=page_number,
                page_order=block_index,
                raw_text=raw_text,
                bbox=bbox,
                page_bbox=page_bbox,
            )


def _pdf_image_furniture_content_identity(
    block: dict[str, Any],
    *,
    max_content_bytes: int | None = None,
) -> tuple[str, int] | None:
    """Bind a repeat candidate to exact embedded base/mask bytes."""

    image_bytes = _image_bytes(block.get("image"))
    if image_bytes is None:
        return None
    mask_bytes = _image_bytes(block.get("mask"))
    content_byte_count = len(image_bytes) + len(mask_bytes or b"")
    if max_content_bytes is not None and content_byte_count > max_content_bytes:
        return None
    digest = hashlib.sha256(b"pdf-image-furniture-content/v1\x00")
    for part in (image_bytes, mask_bytes):
        if part is None:
            digest.update((0).to_bytes(8, "big"))
            continue
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest(), content_byte_count


def _repeated_pdf_image_furniture_diagnostic(
    *,
    kind: PdfPageFurnitureKind,
    source_sha256: str,
    page_number: int,
    block_index: int,
    node_id: str,
    locator: NativeLocator,
    asset_content_sha256: str,
) -> SourceDiagnostic:
    furniture_label = "header" if kind is PdfPageFurnitureKind.HEADER else "footer"
    evidence = json.dumps(
        {
            "schema_version": "pdf-repeated-image-furniture-proof/v1",
            "source_sha256": source_sha256,
            "page_number": page_number,
            "block_index": block_index,
            "kind": furniture_label,
            "bbox": (
                list(_rounded_bbox(locator.bbox))
                if locator.bbox is not None
                else None
            ),
            "asset_content_sha256": asset_content_sha256,
            "minimum_adjacent_pages": PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES,
            "normalized_geometry_tolerance_ratio": (
                PDF_PAGE_FURNITURE_IMAGE_GEOMETRY_TOLERANCE_RATIO
            ),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _diagnostic(
        pdf_image_furniture_diagnostic_code(kind),
        (
            f"Page {page_number} block {block_index} is a repeated {furniture_label} "
            "image: exact embedded content and near-normalized geometry recur on at "
            f"least {PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES} adjacent pages. The image "
            "asset is preserved for audit but "
            "is page furniture, not adopted body content."
        ),
        severity=SourceDiagnosticSeverity.INFO,
        source_node_id=node_id,
        native_locator=locator,
        evidence_sha256=hashlib.sha256(evidence).hexdigest(),
    )


def _pdf_visual_bbox(
    bbox: tuple[float, float, float, float] | None,
    *,
    rotation_matrix: object,
) -> tuple[float, float, float, float] | None:
    """Map an unrotated rawdict box into the visible ``page.rect`` space."""

    if bbox is None:
        return None
    try:
        a, b, c, d, e, f = (float(value) for value in rotation_matrix)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return bbox
    x0, y0, x1, y1 = bbox
    points = (
        (a * x0 + c * y0 + e, b * x0 + d * y0 + f),
        (a * x0 + c * y1 + e, b * x0 + d * y1 + f),
        (a * x1 + c * y0 + e, b * x1 + d * y0 + f),
        (a * x1 + c * y1 + e, b * x1 + d * y1 + f),
    )
    if not all(math.isfinite(value) for point in points for value in point):
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _raw_text_bbox_from_block(
    block: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    """Return the union of visible glyph geometry, not a padded block box."""

    boxes: list[tuple[float, float, float, float]] = []
    lines = block.get("lines")
    if not isinstance(lines, (list, tuple)):
        return None
    for line in lines:
        if not isinstance(line, dict):
            continue
        spans = line.get("spans")
        if not isinstance(spans, (list, tuple)):
            continue
        for span in spans:
            if not isinstance(span, dict):
                continue
            characters = span.get("chars")
            if isinstance(characters, (list, tuple)):
                for character in characters:
                    if (
                        not isinstance(character, dict)
                        or not str(character.get("c") or "").strip()
                    ):
                        continue
                    bbox = _native_bbox(character.get("bbox"))
                    if bbox is not None:
                        boxes.append(bbox)
                continue
            if str(span.get("text") or "").strip():
                bbox = _native_bbox(span.get("bbox"))
                if bbox is not None:
                    boxes.append(bbox)
    if not boxes:
        return None
    return (
        min(bbox[0] for bbox in boxes),
        min(bbox[1] for bbox in boxes),
        max(bbox[2] for bbox in boxes),
        max(bbox[3] for bbox in boxes),
    )


def _raw_text_from_block(block: dict[str, Any]) -> str:
    """Rebuild rawdict text without stripping, joining, or normalizing it."""

    raw_lines: list[str] = []
    lines = block.get("lines")
    if not isinstance(lines, (list, tuple)):
        return ""
    for line in lines:
        if not isinstance(line, dict):
            raw_lines.append("")
            continue
        raw_spans: list[str] = []
        spans = line.get("spans")
        if isinstance(spans, (list, tuple)):
            for span in spans:
                if not isinstance(span, dict):
                    continue
                characters = span.get("chars")
                if isinstance(characters, (list, tuple)):
                    raw_spans.append(
                        "".join(
                            character.get("c", "")
                            for character in characters
                            if isinstance(character, dict)
                            and isinstance(character.get("c", ""), str)
                        )
                    )
                elif isinstance(span.get("text"), str):
                    # Kept only for compatible native payloads; rawdict normally
                    # exposes characters rather than a pre-cleaned span string.
                    raw_spans.append(span["text"])
        raw_lines.append("".join(raw_spans))
    return "\n".join(raw_lines)


def _pdf_text_lines_from_block(
    block: dict[str, Any],
    *,
    rotation_matrix: object,
) -> tuple[PdfTextLineGeometry, ...]:
    """Bind each native PDF line to its exact interval in ``raw_text``.

    The adapter already receives per-character rawdict geometry.  Persisting
    line intervals here lets later Structure spans select only their physical
    source lines without re-parsing the PDF or trusting browser text search.
    """

    raw_lines = block.get("lines")
    if not isinstance(raw_lines, (list, tuple)):
        return ()
    result: list[PdfTextLineGeometry] = []
    offset = 0
    reconstructed: list[str] = []
    for line_index, line in enumerate(raw_lines):
        line_text_parts: list[str] = []
        boxes: list[tuple[float, float, float, float]] = []
        if isinstance(line, dict):
            spans = line.get("spans")
            if isinstance(spans, (list, tuple)):
                for span in spans:
                    if not isinstance(span, dict):
                        continue
                    characters = span.get("chars")
                    if isinstance(characters, (list, tuple)):
                        for character in characters:
                            if not isinstance(character, dict):
                                continue
                            value = character.get("c", "")
                            if not isinstance(value, str):
                                value = ""
                            line_text_parts.append(value)
                            character_bbox = _pdf_visual_bbox(
                                _native_bbox(character.get("bbox")),
                                rotation_matrix=rotation_matrix,
                            )
                            if value and character_bbox is not None:
                                boxes.append(character_bbox)
                    elif isinstance(span.get("text"), str):
                        value = span["text"]
                        line_text_parts.append(value)
                        span_bbox = _pdf_visual_bbox(
                            _native_bbox(span.get("bbox")),
                            rotation_matrix=rotation_matrix,
                        )
                        if value and span_bbox is not None:
                            boxes.append(span_bbox)
        line_text = "".join(line_text_parts)
        reconstructed.append(line_text)
        start_offset = offset
        end_offset = start_offset + len(line_text)
        if line_text and boxes:
            bbox = (
                min(item[0] for item in boxes),
                min(item[1] for item in boxes),
                max(item[2] for item in boxes),
                max(item[3] for item in boxes),
            )
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                result.append(
                    PdfTextLineGeometry(
                        start_offset=start_offset,
                        end_offset=end_offset,
                        bbox=bbox,
                    )
                )
        offset = end_offset + (1 if line_index + 1 < len(raw_lines) else 0)
    if "\n".join(reconstructed) != _raw_text_from_block(block):
        return ()
    return tuple(result)


def _native_pdf_text_kind(
    block: dict[str, Any],
    *,
    fallback: SourceNodeKind,
) -> SourceNodeKind:
    """Use only conjunctive native geometry/alpha proof to tag a watermark."""

    if _is_proven_oblique_translucent_watermark(block):
        return SourceNodeKind.WATERMARK
    return fallback


def _is_proven_oblique_translucent_watermark(block: dict[str, Any]) -> bool:
    """Fail closed unless every non-empty raw span is diagonal and translucent."""

    lines = block.get("lines")
    if not isinstance(lines, (list, tuple)) or not lines:
        return False

    observed_text = False
    for line in lines:
        if not isinstance(line, dict):
            return False
        spans = line.get("spans")
        if not isinstance(spans, (list, tuple)) or not spans:
            return False
        non_empty_spans: list[dict[str, Any]] = []
        for span in spans:
            if not isinstance(span, dict):
                return False
            characters = span.get("chars")
            if isinstance(characters, (list, tuple)):
                span_text = "".join(
                    character.get("c", "")
                    for character in characters
                    if isinstance(character, dict)
                    and isinstance(character.get("c", ""), str)
                )
            elif isinstance(span.get("text"), str):
                span_text = span["text"]
            else:
                span_text = ""
            if span_text.strip():
                non_empty_spans.append(span)
        if not non_empty_spans:
            continue

        direction = line.get("dir")
        if not isinstance(direction, (list, tuple)) or len(direction) != 2:
            return False
        try:
            direction_x = float(direction[0])
            direction_y = float(direction[1])
        except (TypeError, ValueError):
            return False
        norm = math.hypot(direction_x, direction_y)
        if (
            not math.isfinite(norm)
            or norm <= 0.0
            or abs(direction_x / norm) < _NATIVE_WATERMARK_MIN_ABS_DIRECTION_COMPONENT
            or abs(direction_y / norm) < _NATIVE_WATERMARK_MIN_ABS_DIRECTION_COMPONENT
        ):
            return False

        for span in non_empty_spans:
            alpha = span.get("alpha")
            if (
                isinstance(alpha, bool)
                or not isinstance(alpha, (int, float))
                or not math.isfinite(float(alpha))
                or not 0.0 < float(alpha) <= _NATIVE_WATERMARK_MAX_ALPHA
            ):
                return False
            observed_text = True
    return observed_text


def _invisible_text_statistics(block: dict[str, Any]) -> tuple[int, int]:
    """Count rawdict spans explicitly marked fully transparent.

    Missing or malformed alpha values are not guessed.  The adapter preserves
    their raw text exactly as before; this signal only records that a native
    text layer is not visually equivalent to the rendered page.
    """

    span_count = 0
    character_count = 0
    lines = block.get("lines")
    if not isinstance(lines, (list, tuple)):
        return 0, 0
    for line in lines:
        if not isinstance(line, dict):
            continue
        spans = line.get("spans")
        if not isinstance(spans, (list, tuple)):
            continue
        for span in spans:
            if not isinstance(span, dict):
                continue
            alpha = span.get("alpha")
            if (
                isinstance(alpha, bool)
                or not isinstance(alpha, (int, float))
                or not math.isfinite(float(alpha))
                or float(alpha) != 0.0
            ):
                continue
            span_count += 1
            characters = span.get("chars")
            if isinstance(characters, (list, tuple)):
                character_count += sum(
                    len(character.get("c", ""))
                    for character in characters
                    if isinstance(character, dict)
                    and isinstance(character.get("c", ""), str)
                )
            elif isinstance(span.get("text"), str):
                character_count += len(span["text"])
    return span_count, character_count


def _visible_text_character_counts(block: dict[str, Any]) -> tuple[int, int]:
    """Count visible native characters and the CJK subset.

    Missing or malformed alpha remains untrusted.  This signal is used only to
    avoid sending an ordinary native-text page to a remote visual provider and
    to recognize pages whose visible glyphs were converted to vector paths.
    """

    character_count = 0
    cjk_character_count = 0
    lines = block.get("lines")
    if not isinstance(lines, (list, tuple)):
        return 0, 0
    for line in lines:
        if not isinstance(line, dict):
            continue
        spans = line.get("spans")
        if not isinstance(spans, (list, tuple)):
            continue
        for span in spans:
            if not isinstance(span, dict):
                continue
            alpha = span.get("alpha")
            if (
                isinstance(alpha, bool)
                or not isinstance(alpha, (int, float))
                or not math.isfinite(float(alpha))
                or float(alpha) <= 0.0
            ):
                continue
            characters = span.get("chars")
            if isinstance(characters, (list, tuple)):
                native_text = "".join(
                    character.get("c", "")
                    for character in characters
                    if isinstance(character, dict)
                    and isinstance(character.get("c", ""), str)
                )
            elif isinstance(span.get("text"), str):
                native_text = span["text"]
            else:
                native_text = ""
            character_count += len(native_text)
            cjk_character_count += sum(
                _is_cjk_character(character) for character in native_text
            )
    return character_count, cjk_character_count


def _dense_vector_text_risk(
    *,
    page: Any,
    page_bbox: tuple[float, float, float, float] | None,
    native_visible_character_count: int,
    native_visible_cjk_character_count: int,
) -> _VectorTextRisk | None:
    """Detect likely vectorized text without performing local OCR.

    The signal is deliberately conjunctive: many small filled paths, high path
    density, a material path-to-native-character ratio, and extremely low CJK
    coverage must all be present.  Ordinary drawings with a handful of filled
    shapes and native-text documents therefore remain on the native path.
    """

    if page_bbox is None:
        return None
    try:
        get_bboxlog = getattr(page, "get_bboxlog", None)
    except Exception:
        return None
    if not callable(get_bboxlog):
        return None
    try:
        entries = get_bboxlog()
    except Exception:
        return None
    if not isinstance(entries, (list, tuple)):
        return None

    small_fill_path_count = 0
    for entry in entries:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) < 2
            or entry[0] != "fill-path"
        ):
            continue
        bbox = _native_bbox(entry[1])
        if bbox is None:
            continue
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        area = width * height
        if (
            0.0 < width <= _VECTOR_TEXT_MAX_GLYPH_DIMENSION
            and 0.0 < height <= _VECTOR_TEXT_MAX_GLYPH_DIMENSION
            and area <= _VECTOR_TEXT_MAX_GLYPH_AREA
        ):
            small_fill_path_count += 1

    page_area = _bbox_area(page_bbox)
    if (
        small_fill_path_count < _VECTOR_TEXT_MIN_SMALL_FILL_PATHS
        or page_area <= 0.0
        or small_fill_path_count / page_area < _VECTOR_TEXT_MIN_PATH_DENSITY
    ):
        return None
    if (
        native_visible_character_count > 0
        and small_fill_path_count / native_visible_character_count
        < _VECTOR_TEXT_MIN_PATH_TO_NATIVE_CHARACTER_RATIO
    ):
        return None
    native_cjk_allowance = max(
        _VECTOR_TEXT_MIN_NATIVE_CJK_ALLOWANCE,
        math.floor(native_visible_character_count * _VECTOR_TEXT_MAX_NATIVE_CJK_RATIO),
    )
    if native_visible_cjk_character_count > native_cjk_allowance:
        return None
    return _VectorTextRisk(small_fill_path_count=small_fill_path_count)


def _is_cjk_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0x20000 <= codepoint <= 0x323AF
    )


def _is_full_page_image_bbox(
    *,
    image_bbox: tuple[float, float, float, float] | None,
    page_bbox: tuple[float, float, float, float] | None,
) -> bool:
    if image_bbox is None or page_bbox is None:
        return False
    page_area = _bbox_area(page_bbox)
    if page_area <= 0.0:
        return False
    intersection = (
        max(image_bbox[0], page_bbox[0]),
        max(image_bbox[1], page_bbox[1]),
        min(image_bbox[2], page_bbox[2]),
        min(image_bbox[3], page_bbox[3]),
    )
    covered_area = _bbox_area(intersection)
    return covered_area / page_area >= _FULL_PAGE_IMAGE_MIN_AREA_RATIO


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _image_bytes(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value or None
    if isinstance(value, (bytearray, memoryview)):
        converted = bytes(value)
        return converted or None
    return None


def _uniform_opaque_white_image_evidence_sha256(
    *,
    image_bytes: bytes,
    width: Any,
    height: Any,
    cache: dict[tuple[str, int, int], str | None],
    resource_budget: _PdfResourceBudget,
) -> str | None:
    """Return a bounded decode proof for an exactly white opaque image.

    This is a semantic-node exclusion proof, not a PDF rewrite.  Anything
    ambiguous—including alpha, unsupported colorspaces, dimension drift, or
    exhausted proof budget—keeps the original IMAGE occurrence.
    """

    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
    ):
        return None
    content_sha256 = hashlib.sha256(image_bytes).hexdigest()
    cache_key = (content_sha256, width, height)
    if cache_key in cache:
        return cache[cache_key]
    pixel_count = width * height
    if not resource_budget.reserve_uniform_white_image_proof(
        encoded_byte_count=len(image_bytes),
        pixel_count=pixel_count,
    ):
        cache[cache_key] = None
        return None
    try:
        pixmap = pymupdf.Pixmap(image_bytes)
        if (
            int(pixmap.width) != width
            or int(pixmap.height) != height
            or bool(pixmap.alpha)
            or pixmap.colorspace is None
            or int(pixmap.colorspace.n) not in {1, 3, 4}
        ):
            cache[cache_key] = None
            return None
        if int(pixmap.colorspace.n) == 3 and int(pixmap.n) == 3:
            rgb_samples = bytes(pixmap.samples)
        else:
            rgb_pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
            if (
                int(rgb_pixmap.width) != width
                or int(rgb_pixmap.height) != height
                or bool(rgb_pixmap.alpha)
                or int(rgb_pixmap.n) != 3
            ):
                cache[cache_key] = None
                return None
            rgb_samples = bytes(rgb_pixmap.samples)
    except MemoryError as exc:
        raise _PdfResourceBudgetExceeded from exc
    except Exception:
        cache[cache_key] = None
        return None
    if len(rgb_samples) != pixel_count * 3 or rgb_samples.count(255) != len(
        rgb_samples
    ):
        cache[cache_key] = None
        return None
    evidence_payload = {
        "schema": "pdf-uniform-opaque-white-image-proof/v1",
        "content_sha256": content_sha256,
        "decoded_rgb_sha256": hashlib.sha256(rgb_samples).hexdigest(),
        "width": width,
        "height": height,
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cache[cache_key] = evidence_sha256
    return evidence_sha256


def _safe_image_extension(value: Any) -> str:
    candidate = str(value or "").lower()
    return candidate if _SAFE_EXTENSION_PATTERN.fullmatch(candidate) else "bin"


def _native_image_dimensions(*, width: Any, height: Any) -> tuple[int, int]:
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
    ):
        raise _ImageMaskMaterializationRejected("pdf_image_mask_invalid_dimensions")
    return width, height


def _png_image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    if (
        len(image_bytes) < 24
        or image_bytes[:8] != b"\x89PNG\r\n\x1a\n"
        or image_bytes[12:16] != b"IHDR"
    ):
        raise _ImageMaskMaterializationRejected("pdf_image_mask_dimensions_unavailable")
    width = int.from_bytes(image_bytes[16:20], "big")
    height = int.from_bytes(image_bytes[20:24], "big")
    if width <= 0 or height <= 0:
        raise _ImageMaskMaterializationRejected("pdf_image_mask_invalid_dimensions")
    return width, height


def _proven_pdf_alpha_mask_source(
    *,
    document: Any,
    block: dict[str, Any],
    image_infos: tuple[Any, ...],
    image_bytes: bytes,
    mask_bytes: bytes,
    expected_width: int,
    expected_height: int,
    expected_mask_width: int,
    expected_mask_height: int,
    resource_budget: _PdfResourceBudget,
) -> _PdfAlphaMaskSourceProof | None:
    """Bind one alpha-bearing rawdict image to its exact PDF `/SMask`."""

    try:
        base_pixmap = pymupdf.Pixmap(image_bytes)
        mask_pixmap = pymupdf.Pixmap(mask_bytes)
    except MemoryError:
        raise
    except Exception:
        return None
    transform = _pdf_image_transform(block.get("transform"))
    if (
        transform is None
        or not base_pixmap.alpha
        or base_pixmap.colorspace is None
        or base_pixmap.colorspace.n != 3
        or base_pixmap.width != expected_width
        or base_pixmap.height != expected_height
        or mask_pixmap.alpha
        or mask_pixmap.colorspace is None
        or mask_pixmap.colorspace.n != 1
        or mask_pixmap.width != expected_mask_width
        or mask_pixmap.height != expected_mask_height
        or block.get("bpc") != 8
        or block.get("colorspace") != 3
    ):
        return None
    digest = hashlib.md5(base_pixmap.samples, usedforsecurity=False).digest()
    matching_infos: list[dict[str, Any]] = []
    for info in image_infos:
        if not isinstance(info, dict):
            continue
        info_transform = _pdf_image_transform(info.get("transform"))
        xref = info.get("xref")
        if (
            info.get("digest") == digest
            and info.get("width") == expected_width
            and info.get("height") == expected_height
            and info.get("bpc") == 8
            and info.get("colorspace") == 3
            and info.get("has-mask") is True
            and isinstance(xref, int)
            and not isinstance(xref, bool)
            and xref > 0
            and info_transform is not None
            and all(
                math.isclose(left, right, rel_tol=0.0, abs_tol=1e-4)
                for left, right in zip(transform, info_transform, strict=True)
            )
        ):
            matching_infos.append(info)
    base_xrefs = {int(info["xref"]) for info in matching_infos}
    if len(base_xrefs) != 1:
        return None
    base_xref = next(iter(base_xrefs))
    mask_xref = _mupdf_xref_reference_key(
        document=document,
        xref=base_xref,
        key="SMask",
    )
    if mask_xref is None:
        return None
    base_native_width = _mupdf_positive_integer_key(
        document=document,
        xref=base_xref,
        key="Width",
    )
    base_native_height = _mupdf_positive_integer_key(
        document=document,
        xref=base_xref,
        key="Height",
    )
    mask_native_width = _mupdf_positive_integer_key(
        document=document,
        xref=mask_xref,
        key="Width",
    )
    mask_native_height = _mupdf_positive_integer_key(
        document=document,
        xref=mask_xref,
        key="Height",
    )
    if (
        base_native_width is None
        or base_native_height is None
        or mask_native_width is None
        or mask_native_height is None
    ):
        return None
    resource_budget.consume_image_mask_proof_work(
        width=base_native_width,
        height=base_native_height,
        mask_width=mask_native_width,
        mask_height=mask_native_height,
    )
    if (
        not _xref_key_equals(document, base_xref, "Subtype", "name", "/Image")
        or not _xref_key_equals(
            document,
            base_xref,
            "ColorSpace",
            "name",
            "/DeviceRGB",
        )
        or _mupdf_positive_integer_key(
            document=document,
            xref=base_xref,
            key="BitsPerComponent",
        )
        != 8
        or not _xref_key_is_null(document=document, xref=base_xref, key="Mask")
        or not _xref_key_is_false_or_null(
            document=document,
            xref=base_xref,
            key="ImageMask",
        )
        or not _xref_key_equals(document, mask_xref, "Subtype", "name", "/Image")
        or not _xref_key_equals(
            document,
            mask_xref,
            "ColorSpace",
            "name",
            "/DeviceGray",
        )
        or mask_native_width != base_native_width
        or mask_native_height != base_native_height
        or _mupdf_positive_integer_key(
            document=document,
            xref=mask_xref,
            key="BitsPerComponent",
        )
        != 8
        or not _xref_key_is_null(document=document, xref=mask_xref, key="Mask")
        or not _xref_key_is_null(document=document, xref=mask_xref, key="SMask")
        or not _xref_key_is_false_or_null(
            document=document,
            xref=mask_xref,
            key="ImageMask",
        )
        or not _xref_key_is_null(document=document, xref=mask_xref, key="Decode")
        or not _xref_key_is_zero_matte(
            document=document,
            xref=mask_xref,
        )
    ):
        return None
    try:
        source_base_pixmap = pymupdf.Pixmap(document, base_xref)
        if (
            source_base_pixmap.width != base_native_width
            or source_base_pixmap.height != base_native_height
        ):
            return None
        if source_base_pixmap.alpha:
            source_base_pixmap = pymupdf.Pixmap(source_base_pixmap, 0)
        if (
            source_base_pixmap.colorspace is not None
            and source_base_pixmap.colorspace.n > 3
        ):
            source_base_pixmap = pymupdf.Pixmap(
                pymupdf.csRGB,
                source_base_pixmap,
            )
        if (
            source_base_pixmap.width != expected_width
            or source_base_pixmap.height != expected_height
        ):
            source_base_pixmap = pymupdf.Pixmap(
                source_base_pixmap,
                float(expected_width),
                float(expected_height),
            )
        raw_base_pixmap = pymupdf.Pixmap(base_pixmap, 0)
        if (
            source_base_pixmap.colorspace is None
            or source_base_pixmap.colorspace.n != 3
            or source_base_pixmap.width != expected_width
            or source_base_pixmap.height != expected_height
            or bytes(source_base_pixmap.samples) != bytes(raw_base_pixmap.samples)
        ):
            return None
        extracted_mask = document.extract_image(mask_xref)
        extracted_mask_bytes = _image_bytes(extracted_mask.get("image"))
        if extracted_mask_bytes is None:
            return None
        extracted_mask_pixmap = pymupdf.Pixmap(extracted_mask_bytes)
    except MemoryError:
        raise
    except Exception:
        return None
    if (
        extracted_mask_pixmap.alpha
        or extracted_mask_pixmap.colorspace is None
        or extracted_mask_pixmap.colorspace.n != 1
        or extracted_mask_pixmap.width != mask_native_width
        or extracted_mask_pixmap.height != mask_native_height
    ):
        return None
    extracted_mask_samples = bytes(extracted_mask_pixmap.samples)
    raw_mask_samples = bytes(mask_pixmap.samples)
    raw_mask_samples_proven = (
        extracted_mask_pixmap.width == mask_pixmap.width
        and extracted_mask_pixmap.height == mask_pixmap.height
        and extracted_mask_samples == raw_mask_samples
    ) or (
        bool(extracted_mask_samples)
        and bool(raw_mask_samples)
        and all(value == extracted_mask_samples[0] for value in extracted_mask_samples)
        and all(value == raw_mask_samples[0] for value in raw_mask_samples)
        and extracted_mask_samples[0] == raw_mask_samples[0]
    )
    return _PdfAlphaMaskSourceProof(
        base_xref=base_xref,
        mask_xref=mask_xref,
        native_width=base_native_width,
        native_height=base_native_height,
        transform=transform,
        raw_mask_samples_proven=raw_mask_samples_proven,
    )


def _pdf_image_transform(
    value: Any,
) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        return None
    converted: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        converted.append(number)
    return (
        converted[0],
        converted[1],
        converted[2],
        converted[3],
        converted[4],
        converted[5],
    )


def _mupdf_xref_reference_key(
    *,
    document: Any,
    xref: int,
    key: str,
) -> int | None:
    try:
        value_type, value = document.xref_get_key(xref, key)
    except MemoryError:
        raise
    except Exception:
        return None
    if value_type != "xref":
        return None
    match = re.fullmatch(r"([1-9][0-9]*)\s+0\s+R", str(value).strip())
    return int(match.group(1)) if match is not None else None


def _xref_key_is_zero_matte(
    *,
    document: Any,
    xref: int,
) -> bool:
    try:
        value_type, value = document.xref_get_key(xref, "Matte")
    except MemoryError:
        raise
    except Exception:
        return False
    return bool(
        value_type == "array"
        and re.fullmatch(
            r"\[\s*0(?:\.0+)?\s+0(?:\.0+)?\s+0(?:\.0+)?\s*\]",
            str(value),
        )
    )


def _isolated_pdf_image_xobject_pixmap(
    *,
    source_bytes: bytes,
    proof: _PdfAlphaMaskSourceProof,
    expected_width: int,
    expected_height: int,
    expected_mask_samples: bytes,
) -> Any:
    try:
        render_document = pymupdf.open(stream=source_bytes, filetype="pdf")
        try:
            render_page = render_document.new_page(
                width=expected_width,
                height=expected_height,
            )
            render_page.insert_image(render_page.rect, xref=proof.base_xref)
            rendered = render_page.get_pixmap(alpha=True, annots=False)
        finally:
            render_document.close()
    except MemoryError:
        raise
    except Exception as exc:
        raise _ImageMaskMaterializationRejected(
            "pdf_image_mask_base_alpha_ambiguous"
        ) from exc
    if (
        rendered.width != expected_width
        or rendered.height != expected_height
        or not rendered.alpha
        or rendered.colorspace is None
        or rendered.colorspace.n != 3
    ):
        raise _ImageMaskMaterializationRejected("pdf_image_mask_base_alpha_ambiguous")
    rendered_alpha = bytes(rendered.samples[rendered.n - 1 :: rendered.n])
    if len(rendered_alpha) != len(expected_mask_samples) or any(
        abs(left - right) > 1
        for left, right in zip(
            rendered_alpha,
            expected_mask_samples,
            strict=True,
        )
    ):
        raise _ImageMaskMaterializationRejected("pdf_image_mask_base_alpha_ambiguous")
    return rendered


def _materialize_alpha_mask_composite(
    *,
    image_bytes: bytes,
    mask_bytes: bytes,
    expected_width: int,
    expected_height: int,
    expected_mask_width: int,
    expected_mask_height: int,
    source_bytes: bytes | None = None,
    alpha_mask_source_proof: _PdfAlphaMaskSourceProof | None = None,
    resource_budget: _PdfResourceBudget | None = None,
) -> _MaterializedImageAsset:
    """Return the visible PNG represented by separate PDF image/mask bytes."""

    if pymupdf is None:  # pragma: no cover - guarded by the adapter entrypoint
        raise RuntimeError("PyMuPDF is unavailable")
    base_pixmap = pymupdf.Pixmap(image_bytes)
    mask_pixmap = pymupdf.Pixmap(mask_bytes)
    if base_pixmap.width != expected_width or base_pixmap.height != expected_height:
        raise _ImageMaskMaterializationRejected("pdf_image_dimension_metadata_mismatch")
    if (
        mask_pixmap.width != expected_mask_width
        or mask_pixmap.height != expected_mask_height
    ):
        raise _ImageMaskMaterializationRejected(
            "pdf_image_mask_dimension_metadata_mismatch"
        )
    if base_pixmap.colorspace is None:
        raise _ImageMaskMaterializationRejected(
            "pdf_image_mask_base_colorspace_missing"
        )
    if mask_pixmap.alpha or mask_pixmap.colorspace is None:
        raise _ImageMaskMaterializationRejected("pdf_image_mask_invalid_colorspace")
    if mask_pixmap.colorspace.n != 1:
        raise _ImageMaskMaterializationRejected("pdf_image_mask_invalid_colorspace")
    if base_pixmap.colorspace.n > 3:
        base_pixmap = pymupdf.Pixmap(pymupdf.csRGB, base_pixmap)

    composite_pixmap = None
    mask_materialization_strategy = "raw_component_composite"
    if base_pixmap.alpha:
        base_alpha = bytes(base_pixmap.samples[base_pixmap.n - 1 :: base_pixmap.n])
        mask_samples = bytes(mask_pixmap.samples)
        if (
            alpha_mask_source_proof is None
            or source_bytes is None
            or not all(value == 255 for value in base_alpha)
        ):
            # Alpha-bearing rawdict bytes are producer-dependent. They can be
            # interpreted only after this occurrence is uniquely bound to the
            # source PDF image XObject and its direct `/SMask`; every other
            # relationship remains deliberately ambiguous.
            raise _ImageMaskMaterializationRejected(
                "pdf_image_mask_base_alpha_ambiguous"
            )
        if (
            alpha_mask_source_proof.raw_mask_samples_proven
            and mask_samples
            and all(value == 0 for value in mask_samples)
        ):
            mask_materialization_strategy = "source_bound_uniform_zero"
            composite_pixmap = pymupdf.Pixmap(
                pymupdf.csRGB,
                pymupdf.IRect(0, 0, mask_pixmap.width, mask_pixmap.height),
                True,
            )
            composite_pixmap.clear_with()
        elif (
            alpha_mask_source_proof.raw_mask_samples_proven
            and mask_samples
            and all(value == 255 for value in mask_samples)
        ):
            mask_materialization_strategy = "source_bound_uniform_255"
            base_pixmap = pymupdf.Pixmap(base_pixmap, 0)
        else:
            mask_materialization_strategy = "source_bound_isolated_render"
            if resource_budget is None:
                raise _ImageMaskMaterializationRejected(
                    "pdf_image_mask_base_alpha_ambiguous"
                )
            resource_budget.consume_alpha_mask_isolated_render()
            composite_pixmap = _isolated_pdf_image_xobject_pixmap(
                source_bytes=source_bytes,
                proof=alpha_mask_source_proof,
                expected_width=mask_pixmap.width,
                expected_height=mask_pixmap.height,
                expected_mask_samples=mask_samples,
            )

    if composite_pixmap is None:
        if (
            base_pixmap.width != mask_pixmap.width
            or base_pixmap.height != mask_pixmap.height
        ):
            # An explicit PDF image mask has its own image coordinate space.
            # MuPDF renders the base into that coordinate space before
            # applying the mask. Keeping the higher-resolution mask is
            # essential: shrinking it destroys 1-bit stencil detail.
            base_pixmap = pymupdf.Pixmap(
                base_pixmap,
                float(mask_pixmap.width),
                float(mask_pixmap.height),
            )
        composite_pixmap = pymupdf.Pixmap(base_pixmap, mask_pixmap)
    if (
        composite_pixmap.width != expected_mask_width
        or composite_pixmap.height != expected_mask_height
        or not composite_pixmap.alpha
    ):
        raise _ImageMaskMaterializationRejected(
            "pdf_image_mask_composite_invalid_geometry"
        )
    composite_bytes = bytes(composite_pixmap.tobytes("png"))
    if not composite_bytes:
        raise _ImageMaskMaterializationRejected("pdf_image_mask_composite_empty")
    alpha_footprint = _exact_alpha_footprint_from_pixmap(composite_pixmap)
    return _MaterializedImageAsset(
        content_sha256=hashlib.sha256(composite_bytes).hexdigest(),
        byte_count=len(composite_bytes),
        extension="png",
        media_type="image/png",
        content_bytes=composite_bytes,
        derivation=SourceAssetDerivation(
            kind=SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE,
            base_content_sha256=hashlib.sha256(image_bytes).hexdigest(),
            mask_content_sha256=hashlib.sha256(mask_bytes).hexdigest(),
            alpha_footprint=alpha_footprint,
        ),
        mask_materialization_strategy=mask_materialization_strategy,
    )


def _exact_alpha_footprint_from_pixmap(pixmap: Any) -> SourceAlphaFootprint | None:
    """Return bounded canonical RLE for every non-zero final alpha sample."""

    try:
        width = int(pixmap.width)
        height = int(pixmap.height)
        channel_count = int(pixmap.n)
        stride = int(pixmap.stride)
        has_alpha = bool(pixmap.alpha)
        samples = bytes(pixmap.samples)
    except MemoryError:
        raise
    except Exception:
        return None
    if (
        not has_alpha
        or width < 1
        or height < 1
        or channel_count < 2
        or width * height > _MAX_PDF_ALPHA_FOOTPRINT_PIXELS
        or stride < width * channel_count
        or len(samples) < stride * height
    ):
        return None

    runs: list[SourceAlphaFootprintRun] = []
    nonzero_sample_count = 0
    alpha_channel_offset = channel_count - 1
    for y in range(height):
        row_offset = y * stride
        run_start: int | None = None
        for x in range(width):
            alpha = samples[row_offset + x * channel_count + alpha_channel_offset]
            if alpha != 0:
                nonzero_sample_count += 1
                if run_start is None:
                    run_start = x
                continue
            if run_start is None:
                continue
            runs.append(SourceAlphaFootprintRun(y=y, x_start=run_start, x_end=x))
            if len(runs) > _MAX_PDF_ALPHA_FOOTPRINT_RUNS:
                return None
            run_start = None
        if run_start is not None:
            runs.append(SourceAlphaFootprintRun(y=y, x_start=run_start, x_end=width))
            if len(runs) > _MAX_PDF_ALPHA_FOOTPRINT_RUNS:
                return None
    if not runs or nonzero_sample_count < 1:
        return None
    canonical_runs = tuple(runs)
    return SourceAlphaFootprint(
        width_px=width,
        height_px=height,
        runs=canonical_runs,
        nonzero_sample_count=nonzero_sample_count,
        evidence_sha256=stable_source_alpha_footprint_evidence_sha256(
            width_px=width,
            height_px=height,
            runs=canonical_runs,
            nonzero_sample_count=nonzero_sample_count,
        ),
    )


def _stable_asset_id(
    *,
    content_sha256: str,
    media_type: str,
) -> str:
    payload = json.dumps(
        (SourceAssetKind.IMAGE.value, content_sha256, media_type),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"asset-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _stable_page_render_asset_id(
    *,
    source_sha256: str,
    page_number: int,
    content_sha256: str,
) -> str:
    payload = json.dumps(
        (
            SourceAssetKind.PAGE_RENDER.value,
            source_sha256,
            page_number,
            content_sha256,
            "image/png",
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"asset-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _image_mask_materialization_evidence_sha256(
    *,
    source_sha256: str,
    page_number: int,
    block_index: int,
    materialized_image: _MaterializedImageAsset,
    source_proof: _PdfAlphaMaskSourceProof | None,
    dimensions: tuple[int, int, int, int],
) -> str:
    derivation = materialized_image.derivation
    strategy = materialized_image.mask_materialization_strategy
    if derivation is None or strategy is None:
        raise ValueError(
            "mask materialization evidence requires derivation and strategy"
        )
    base_width, base_height, mask_width, mask_height = dimensions
    payload = {
        "schema_version": "pdf-image-mask-materialization-evidence/v1",
        "source_sha256": source_sha256,
        "page_number": page_number,
        "block_index": block_index,
        "base_content_sha256": derivation.base_content_sha256,
        "mask_content_sha256": derivation.mask_content_sha256,
        "output_content_sha256": materialized_image.content_sha256,
        "alpha_footprint_evidence_sha256": (
            derivation.alpha_footprint.evidence_sha256
            if derivation.alpha_footprint is not None
            else None
        ),
        "strategy": strategy,
        "raw_base_dimensions": [base_width, base_height],
        "raw_mask_dimensions": [mask_width, mask_height],
        "base_xref": (source_proof.base_xref if source_proof is not None else None),
        "mask_xref": (source_proof.mask_xref if source_proof is not None else None),
        "native_xobject_dimensions": (
            [source_proof.native_width, source_proof.native_height]
            if source_proof is not None
            else None
        ),
        "transform": (
            [round(value, 6) for value in source_proof.transform]
            if source_proof is not None
            else None
        ),
        "raw_mask_samples_proven": (
            source_proof.raw_mask_samples_proven if source_proof is not None else None
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _mrc_layer_evidence_sha256(
    *,
    source_sha256: str,
    page_number: int,
    layer: _MrcImageLayer,
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "page_number": page_number,
        "block_index": layer.block_index,
        "xref": layer.xref,
        "bbox": [round(value, 6) for value in layer.bbox],
        "native_dimensions": [layer.native_width, layer.native_height],
        "transform": [round(value, 6) for value in layer.transform],
        "content_sha256": layer.content_sha256,
        "role": "paint_stencil" if layer.is_stencil else "scan_base",
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _mrc_composite_evidence_sha256(
    *,
    source_sha256: str,
    page_number: int,
    rendered_page: _RenderedPdfPage,
    plan: _MrcPageCompositePlan,
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "page_number": page_number,
        "render": {
            "content_sha256": rendered_page.image_sha256,
            "width_px": rendered_page.width_px,
            "height_px": rendered_page.height_px,
            "scale": rendered_page.scale,
            "annots": False,
        },
        "layers": [
            {
                "block_index": layer.block_index,
                "xref": layer.xref,
                "bbox": [round(value, 6) for value in layer.bbox],
                "native_dimensions": [layer.native_width, layer.native_height],
                "transform": [round(value, 6) for value in layer.transform],
                "content_sha256": layer.content_sha256,
                "role": "paint_stencil" if layer.is_stencil else "scan_base",
            }
            for layer in plan.layers
        ],
        "preserved_text_block_indexes": list(plan.preserved_text_block_indexes),
        "orthogonal_drawing_count": plan.orthogonal_drawing_count,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _logical_image_storage_key(
    *,
    content_sha256: str,
    extension: str,
) -> str:
    # Content-addressed logical key: placement remains on each SourceNode while
    # identical embedded bytes share one asset reference. The adapter does not
    # materialize this key and never writes beside the source file.
    return f"source-assets/{content_sha256[:2]}/{content_sha256}.{extension}"


def _logical_page_render_storage_key(
    *,
    source_sha256: str,
    page_number: int,
    content_sha256: str,
) -> str:
    return (
        f"source-page-renders/{source_sha256[:2]}/{source_sha256}/"
        f"page-{page_number:06d}-{content_sha256}.png"
    )


def _diagnostic(
    code: str,
    message: str,
    *,
    severity: SourceDiagnosticSeverity = SourceDiagnosticSeverity.WARNING,
    source_node_id: str | None = None,
    native_locator: NativeLocator | None = None,
    evidence_sha256: str | None = None,
) -> SourceDiagnostic:
    return SourceDiagnostic(
        code=code,
        severity=severity,
        message=message,
        source_node_id=source_node_id,
        native_locator=native_locator,
        evidence_sha256=evidence_sha256,
    )


def _failed_snapshot(
    *,
    source_ir_id: str,
    document_id: str,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    created_at: datetime,
    code: str,
    message: str,
) -> SourceIRSnapshot:
    return SourceIRSnapshot(
        source_ir_id=source_ir_id,
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.FAILED,
        diagnostics=(
            _diagnostic(
                code,
                message,
                severity=SourceDiagnosticSeverity.ERROR,
            ),
        ),
        created_at=created_at,
    )


__all__ = [
    "PDF_SOURCE_IR_ADAPTER_ID",
    "PDF_SOURCE_IR_ADAPTER_VERSION",
    "PdfSourceIRAdapter",
    "render_pdf_visual_page_input",
]
