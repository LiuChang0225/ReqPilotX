from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.domain.source_visual_text import VisualTextSnapshot
from app.domain.extraction.visual_table import VisualTableSnapshot


SOURCE_IR_SCHEMA_VERSION = "source-ir/v1"
SOURCE_CONVERSION_PROVENANCE_SCHEMA_VERSION = (
    "source-conversion-provenance/xlsb-to-xlsx/v1"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_XLSX_RANGE_PATTERN = re.compile(
    r"^\$?[A-Z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Z]{1,3}\$?[1-9][0-9]*)?$"
)
_XLSX_RANGE_PARTS_PATTERN = re.compile(
    r"^\$?([A-Z]{1,3})\$?([1-9][0-9]*)(?::\$?([A-Z]{1,3})\$?([1-9][0-9]*))?$"
)


class _StrictFrozenModel(BaseModel):
    """Provider-neutral immutable value object used by SourceIR.

    Adapters must map their payloads explicitly. Rejecting unknown fields keeps
    vendor-specific data and accidental schema drift out of the fact layer.
    Tuple fields are used for collections so a frozen model cannot be mutated
    indirectly through a list.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SourceIRStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class NativeDocumentFormat(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    TIF = "tif"


class SourceNodeKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    CELL = "cell"
    IMAGE = "image"
    FIGURE = "figure"
    NOTE = "note"
    HEADER = "header"
    FOOTER = "footer"
    WATERMARK = "watermark"
    TOC_ENTRY = "toc_entry"
    ATTACHMENT = "attachment"
    UNKNOWN = "unknown"


class SourceAssetKind(str, Enum):
    IMAGE = "image"
    FIGURE = "figure"
    TABLE_RENDER = "table_render"
    PAGE_RENDER = "page_render"
    ATTACHMENT = "attachment"
    UNKNOWN = "unknown"


class SourceAssetDerivationKind(str, Enum):
    ALPHA_MASK_COMPOSITE = "alpha_mask_composite"


_MAX_SOURCE_ALPHA_FOOTPRINT_PIXELS = 16_777_216
_MAX_SOURCE_ALPHA_FOOTPRINT_RUNS = 65_536


class SourceAlphaFootprintRun(_StrictFrozenModel):
    """One canonical non-zero-alpha interval in final composite pixels."""

    y: int = Field(ge=0)
    x_start: int = Field(ge=0)
    x_end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_half_open_interval(self) -> SourceAlphaFootprintRun:
        if self.x_end <= self.x_start:
            raise ValueError("alpha-footprint runs require x_start < x_end")
        return self


class SourceAlphaFootprint(_StrictFrozenModel):
    """Exact, bounded RLE of non-zero alpha in the final composite image."""

    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    runs: tuple[SourceAlphaFootprintRun, ...] = Field(
        min_length=1,
        max_length=_MAX_SOURCE_ALPHA_FOOTPRINT_RUNS,
    )
    nonzero_sample_count: int = Field(ge=1)
    evidence_sha256: StrictStr

    @field_validator("evidence_sha256")
    @classmethod
    def validate_evidence_sha256(cls, value: str) -> str:
        _validate_sha256(value, "alpha_footprint.evidence_sha256")
        return value

    @model_validator(mode="after")
    def validate_canonical_exact_footprint(self) -> SourceAlphaFootprint:
        pixel_count = self.width_px * self.height_px
        if pixel_count > _MAX_SOURCE_ALPHA_FOOTPRINT_PIXELS:
            raise ValueError("alpha-footprint pixel count exceeds safe boundary")
        run_keys = tuple((run.y, run.x_start, run.x_end) for run in self.runs)
        if run_keys != tuple(sorted(run_keys)):
            raise ValueError("alpha-footprint runs must be sorted")
        observed_sample_count = 0
        previous: SourceAlphaFootprintRun | None = None
        for run in self.runs:
            if run.y >= self.height_px or run.x_end > self.width_px:
                raise ValueError("alpha-footprint run exceeds image bounds")
            if (
                previous is not None
                and previous.y == run.y
                and previous.x_end >= run.x_start
            ):
                raise ValueError(
                    "alpha-footprint runs must be non-overlapping and maximally merged"
                )
            observed_sample_count += run.x_end - run.x_start
            previous = run
        if observed_sample_count != self.nonzero_sample_count:
            raise ValueError(
                "alpha-footprint nonzero_sample_count must equal exact run coverage"
            )
        expected_evidence_sha256 = stable_source_alpha_footprint_evidence_sha256(
            width_px=self.width_px,
            height_px=self.height_px,
            runs=self.runs,
            nonzero_sample_count=self.nonzero_sample_count,
        )
        if self.evidence_sha256 != expected_evidence_sha256:
            raise ValueError(
                "alpha-footprint evidence_sha256 must bind the canonical exact RLE"
            )
        return self


class SourceDiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DocxInlineLanguageEvidence(str, Enum):
    """Script evidence attached to one source-bound DOCX text interval."""

    EN = "en"
    ZH_CN = "zh-CN"


class DocxInlineBoundaryBasis(str, Enum):
    """Native evidence that made an inline language boundary reviewable."""

    DOCX_RUN_BOUNDARY = "DOCX_RUN_BOUNDARY"


class DocxInlineSpan(_StrictFrozenModel):
    """A half-open interval over ``SourceNode.raw_text`` without copied text."""

    source_node_id: str = Field(pattern=r"^node-[0-9a-f]{32}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    language_evidence: DocxInlineLanguageEvidence
    boundary_basis: Literal[DocxInlineBoundaryBasis.DOCX_RUN_BOUNDARY] = (
        DocxInlineBoundaryBasis.DOCX_RUN_BOUNDARY
    )

    @model_validator(mode="after")
    def validate_interval(self) -> DocxInlineSpan:
        if self.end_offset <= self.start_offset:
            raise ValueError("DOCX inline spans use a non-empty half-open interval")
        return self


class SourceConversionSecurityConfig(_StrictFrozenModel):
    """Auditable, content-free configuration for one format conversion."""

    output_filter: Literal["xlsx:Calc MS Excel 2007 XML"]
    isolated_temporary_directory: StrictBool
    isolated_user_profile: StrictBool
    macro_policy: Literal["reject_source_and_disable_runtime"]
    external_link_policy: Literal[
        "reject_external_data_and_block_unresolved_cid_visual"
    ]
    network_policy: Literal["macos_sandbox_deny_non_loopback_ip"]
    timeout_seconds: StrictInt = Field(ge=1, le=600)


class SourceConversionProvenance(_StrictFrozenModel):
    """Proves how a derived package was produced without replacing source truth."""

    schema_version: Literal["source-conversion-provenance/xlsb-to-xlsx/v1"] = (
        SOURCE_CONVERSION_PROVENANCE_SCHEMA_VERSION
    )
    original_format: Literal["xlsb"]
    derived_format: Literal["xlsx"]
    original_source_sha256: StrictStr
    derived_source_sha256: StrictStr
    converter_id: str = Field(min_length=1, max_length=160)
    converter_version: str = Field(min_length=1, max_length=160)
    converter_build_id: str = Field(min_length=1, max_length=160)
    converter_executable_sha256: StrictStr
    converter_config: SourceConversionSecurityConfig
    converter_config_sha256: StrictStr
    converter_output_sha256: StrictStr
    source_truth: Literal["original_bytes"]
    fidelity_profile: Literal["derived_spreadsheet_structure_and_values_only"]

    @field_validator(
        "original_source_sha256",
        "derived_source_sha256",
        "converter_executable_sha256",
        "converter_config_sha256",
        "converter_output_sha256",
    )
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        _validate_sha256(value, "conversion provenance digest")
        return value

    @field_validator("converter_id", "converter_version", "converter_build_id")
    @classmethod
    def reject_blank_converter_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("converter identity values must not be blank")
        return value

    @model_validator(mode="after")
    def validate_config_fingerprint(self) -> SourceConversionProvenance:
        canonical = json.dumps(
            self.converter_config.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != self.converter_config_sha256:
            raise ValueError("converter_config_sha256 must bind converter_config")
        return self


class NativeLocator(_StrictFrozenModel):
    """A strict locator into the original PDF, DOCX, or XLSX container.

    The optional fields form a tagged union validated by ``format``. This keeps
    one serializable contract while rejecting coordinates from another format.
    All indexes are zero-based except PDF page numbers, which are one-based.
    """

    format: NativeDocumentFormat
    whole_document: Literal[True] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    # PDF path.
    page_number: int | None = Field(default=None, ge=1)
    block_index: int | None = Field(default=None, ge=0)
    bbox: tuple[float, float, float, float] | None = None

    # DOCX body/table/cell path.
    body_element_index: int | None = Field(default=None, ge=0)
    paragraph_index: int | None = Field(default=None, ge=0)
    table_index: int | None = Field(default=None, ge=0)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    object_index: int | None = Field(default=None, ge=0)

    # XLSX workbook/sheet/range path.
    workbook: str | None = None
    sheet: str | None = None
    cell_range: str | None = None

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float] | None,
    ) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        x0, y0, x1, y1 = value
        if x1 < x0 or y1 < y0:
            raise ValueError("bbox must be ordered as x0, y0, x1, y1")
        return value

    @model_validator(mode="after")
    def validate_format_path(self) -> NativeLocator:
        pdf_values = (self.page_number, self.block_index, self.bbox)
        docx_body_values = (self.body_element_index, self.paragraph_index)
        shared_table_values = (self.table_index, self.row_index, self.column_index)
        xlsx_values = (self.workbook, self.sheet, self.cell_range)
        all_native_values = (
            *pdf_values,
            *docx_body_values,
            *shared_table_values,
            self.object_index,
            *xlsx_values,
        )

        if self.whole_document is True:
            if self.format not in {NativeDocumentFormat.PDF, NativeDocumentFormat.TIF}:
                raise ValueError(
                    "whole-document locators are valid only for PDF or TIF"
                )
            if any(value is not None for value in all_native_values):
                raise ValueError(
                    "whole-document locators cannot contain page, block, table, or cell paths"
                )
            return self
        if self.format is NativeDocumentFormat.TIF:
            raise ValueError("TIF locators require whole_document=true")

        if self.format is NativeDocumentFormat.PDF:
            if self.page_number is None or (
                self.block_index is None
                and self.bbox is None
                and self.table_index is None
            ):
                raise ValueError(
                    "PDF locator requires page_number and block_index, bbox, or table_index"
                )
            if self.block_index is not None and self.table_index is not None:
                raise ValueError(
                    "PDF locator cannot combine block_index and table_index"
                )
            if self.table_index is None and (
                self.row_index is not None or self.column_index is not None
            ):
                raise ValueError("PDF cell coordinates require table_index")
            if (self.row_index is None) != (self.column_index is None):
                raise ValueError(
                    "PDF row_index and column_index must be provided together"
                )
            if self.object_index is not None:
                raise ValueError(
                    "PDF locator uses block_index for native visual objects"
                )
            if any(value is not None for value in (*docx_body_values, *xlsx_values)):
                raise ValueError(
                    "PDF locator cannot contain DOCX body/paragraph or XLSX path fields"
                )
            return self

        if self.format is NativeDocumentFormat.DOCX:
            if self.body_element_index is None:
                raise ValueError("DOCX locator requires body_element_index")
            if self.paragraph_index is None and self.table_index is None:
                raise ValueError("DOCX locator requires a paragraph or table path")
            if self.table_index is None and (
                self.row_index is not None or self.column_index is not None
            ):
                raise ValueError("DOCX cell coordinates require table_index")
            if (self.row_index is None) != (self.column_index is None):
                raise ValueError(
                    "DOCX row_index and column_index must be provided together"
                )
            if self.object_index is not None and self.paragraph_index is None:
                raise ValueError("DOCX object_index requires paragraph_index")
            if any(value is not None for value in (*pdf_values, *xlsx_values)):
                raise ValueError("DOCX locator cannot contain PDF or XLSX path fields")
            return self

        if not all(value is not None and str(value).strip() for value in xlsx_values):
            raise ValueError("XLSX locator requires workbook, sheet, and cell_range")
        if not _XLSX_RANGE_PATTERN.fullmatch(self.cell_range or ""):
            raise ValueError("cell_range must be an A1-style XLSX range")
        if any(
            value is not None
            for value in (*pdf_values, *docx_body_values, *shared_table_values)
        ):
            raise ValueError("XLSX locator cannot contain PDF or DOCX path fields")
        return self


class SourceAssetDerivation(_StrictFrozenModel):
    """Immutable lineage for an asset derived from native source bytes."""

    kind: SourceAssetDerivationKind
    base_content_sha256: StrictStr
    mask_content_sha256: StrictStr
    alpha_footprint: SourceAlphaFootprint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("base_content_sha256", "mask_content_sha256")
    @classmethod
    def validate_component_sha256(cls, value: str) -> str:
        _validate_sha256(value, "component_content_sha256")
        return value


class SourceAssetRef(_StrictFrozenModel):
    asset_id: str = Field(min_length=1, max_length=160)
    kind: SourceAssetKind
    content_sha256: StrictStr
    media_type: str = Field(min_length=1, max_length=160)
    storage_key: str = Field(min_length=1, max_length=1000)
    native_locator: NativeLocator | None = None

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        _validate_sha256(value, "content_sha256")
        return value

    @field_validator("asset_id", "media_type", "storage_key")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("storage_key")
    @classmethod
    def require_portable_relative_storage_key(cls, value: str) -> str:
        """Keep customer-machine paths out of immutable SourceIR payloads."""

        if "\\" in value:
            raise ValueError("storage_key must use portable forward slashes")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("storage_key must be a contained relative key")
        # PurePosixPath treats ``C:/...`` as relative, so reject drive-like
        # prefixes explicitly as well.
        if path.parts and path.parts[0].endswith(":"):
            raise ValueError("storage_key must not contain a drive prefix")
        return value


class MergedRegion(_StrictFrozenModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)


class TableCellIR(_StrictFrozenModel):
    cell_id: str = Field(min_length=1, max_length=160)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    raw_text: str = ""
    formula: str | None = None
    display_value: str | None = None
    native_locator: NativeLocator

    @field_validator("formula")
    @classmethod
    def reject_empty_formula(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("formula must be non-blank when provided")
        return value


class TableIR(_StrictFrozenModel):
    table_id: str = Field(min_length=1, max_length=160)
    row_count: int = Field(ge=1)
    column_count: int = Field(ge=1)
    header_regions: tuple[MergedRegion, ...] = ()
    cells: tuple[TableCellIR, ...] = ()
    notes: tuple[str, ...] = ()
    merged_regions: tuple[MergedRegion, ...] = ()
    visual_asset_ref: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_regions_and_cells(self) -> TableIR:
        for region in (*self.header_regions, *self.merged_regions):
            self._require_region_in_bounds(region)

        merged_coverage: set[tuple[int, int]] = set()
        merged_by_anchor: dict[tuple[int, int], MergedRegion] = {}
        for region in self.merged_regions:
            if region.row_span == 1 and region.column_span == 1:
                raise ValueError("merged region must span more than one cell")
            anchor = (region.row, region.column)
            if anchor in merged_by_anchor:
                raise ValueError("merged regions must have unique anchors")
            coverage = _region_coverage(region)
            if merged_coverage.intersection(coverage):
                raise ValueError("merged regions must not overlap")
            merged_coverage.update(coverage)
            merged_by_anchor[anchor] = region

        positions: set[tuple[int, int]] = set()
        occupied: set[tuple[int, int]] = set()
        cells_by_anchor: dict[tuple[int, int], TableCellIR] = {}
        for cell in self.cells:
            anchor = (cell.row, cell.column)
            if anchor in positions:
                raise ValueError("table cells must have unique row/column anchors")
            positions.add(anchor)
            if cell.row + cell.row_span > self.row_count:
                raise ValueError("table cell exceeds row_count")
            if cell.column + cell.column_span > self.column_count:
                raise ValueError("table cell exceeds column_count")
            coverage = _cell_coverage(cell)
            if occupied.intersection(coverage):
                raise ValueError("table cell spans must not overlap")
            occupied.update(coverage)
            if cell.cell_id != stable_cell_id(self.table_id, cell.row, cell.column):
                raise ValueError("cell_id must be derived from table_id and its anchor")
            if (cell.row_span > 1 or cell.column_span > 1) and (
                merged_by_anchor.get(anchor)
                != MergedRegion(
                    row=cell.row,
                    column=cell.column,
                    row_span=cell.row_span,
                    column_span=cell.column_span,
                )
            ):
                raise ValueError("spanning cells require a matching merged region")
            cells_by_anchor[anchor] = cell

        for anchor, region in merged_by_anchor.items():
            anchor_cell = cells_by_anchor.get(anchor)
            if anchor_cell is None:
                raise ValueError("merged region requires one anchor cell")
            if (
                anchor_cell.row_span != region.row_span
                or anchor_cell.column_span != region.column_span
            ):
                raise ValueError("merged region and anchor cell spans must match")
        return self

    def _require_region_in_bounds(self, region: MergedRegion) -> None:
        if region.row + region.row_span > self.row_count:
            raise ValueError("table region exceeds row_count")
        if region.column + region.column_span > self.column_count:
            raise ValueError("table region exceeds column_count")


class SourceDiagnostic(_StrictFrozenModel):
    code: str = Field(min_length=1, max_length=120)
    severity: SourceDiagnosticSeverity = SourceDiagnosticSeverity.WARNING
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    source_node_id: str | None = Field(default=None, min_length=1, max_length=160)
    native_locator: NativeLocator | None = None
    evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )

    @field_validator("code", "message")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class DocxListMetadata(_StrictFrozenModel):
    """Direct ``w:numPr`` facts without inferred numbering presentation.

    Either value may be absent in the OOXML paragraph.  The fact layer does
    not synthesize a missing value and deliberately has no ``lvlText`` field.
    """

    num_id: StrictInt | None = Field(default=None, ge=0)
    ilvl: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_direct_value(self) -> DocxListMetadata:
        if self.num_id is None and self.ilvl is None:
            raise ValueError("DOCX list metadata requires a direct numId or ilvl value")
        return self


class PdfTextLineGeometry(_StrictFrozenModel):
    """Source-bound PDF line geometry over one node's immutable ``raw_text``."""

    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    bbox: tuple[float, float, float, float]

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = value
        if (
            any(not math.isfinite(item) for item in value)
            or x1 <= x0
            or y1 <= y0
        ):
            raise ValueError("PDF text-line bbox must be finite and non-empty")
        return value

    @model_validator(mode="after")
    def validate_offsets(self) -> PdfTextLineGeometry:
        if self.end_offset <= self.start_offset:
            raise ValueError("PDF text-line offsets must be non-empty")
        return self


class PdfTextTraceRef(_StrictFrozenModel):
    origin: Literal['PDF_TEXT_DRAWING_STREAM'] = 'PDF_TEXT_DRAWING_STREAM'
    reader_version: Literal['1'] = '1'
    page_number: StrictInt = Field(ge=1)
    spans: tuple[tuple[StrictInt, StrictInt, StrictInt], ...] = Field(min_length=1)
    text_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    visibility: Literal['REQUIRES_ORIGINAL_REVIEW'] = 'REQUIRES_ORIGINAL_REVIEW'

    @field_validator('spans')
    @classmethod
    def validate_spans(cls, value):
        if tuple(sorted(set(value))) != value or any(i < 0 or a < 0 or b <= a for i,a,b in value):
            raise ValueError('PDF text trace spans need ordered indexes and nonempty character intervals')
        return value


class VisualTextProjectionRef(_StrictFrozenModel):
    """Exact link to an immutable visual observation, not native PDF text."""

    origin: Literal["VISUAL_TEXT_OBSERVATION"] = "VISUAL_TEXT_OBSERVATION"
    snapshot_id: str = Field(min_length=1, max_length=160)
    observation_id: str = Field(min_length=1, max_length=160)


class SourceNode(_StrictFrozenModel):
    source_node_id: str = Field(min_length=1, max_length=160)
    kind: SourceNodeKind
    source_order: int = Field(ge=0)
    raw_text: str = ""
    native_locator: NativeLocator
    visual_locator: NativeLocator | None = None
    parent_source_node_id: str | None = Field(
        default=None, min_length=1, max_length=160
    )
    table_payload: TableIR | None = None
    asset_ref: str | None = Field(default=None, min_length=1, max_length=160)
    asset_derivation: SourceAssetDerivation | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    language: str | None = Field(default=None, min_length=1, max_length=32)
    docx_list_metadata: DocxListMetadata | None = None
    docx_inline_spans: tuple[DocxInlineSpan, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    pdf_text_lines: tuple[PdfTextLineGeometry, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    pdf_text_trace: PdfTextTraceRef | None = Field(default=None, exclude_if=lambda value: value is None)
    visual_text_ref: VisualTextProjectionRef | None = Field(default=None, exclude_if=lambda value: value is None)
    extraction_diagnostics: tuple[SourceDiagnostic, ...] = ()

    @model_validator(mode="after")
    def validate_payload(self) -> SourceNode:
        if self.visual_text_ref is not None and (
            self.pdf_text_trace is not None
            or self.native_locator.format is not NativeDocumentFormat.PDF
            or self.kind not in {SourceNodeKind.PARAGRAPH, SourceNodeKind.TABLE}
            or self.asset_ref is not None
        ):
            raise ValueError("visual text projections must retain distinct PDF text/table provenance")
        if self.pdf_text_trace is not None:
            if (self.native_locator.format is not NativeDocumentFormat.PDF
                or self.native_locator.page_number != self.pdf_text_trace.page_number
                or self.table_payload is not None or self.asset_ref is not None
                or hashlib.sha256(self.raw_text.encode('utf-8')).hexdigest() != self.pdf_text_trace.text_sha256):
                raise ValueError('PDF drawing text requires matching source text, page and native PDF location')
        if self.kind is SourceNodeKind.TABLE and self.table_payload is None:
            raise ValueError("table nodes require table_payload")
        if self.kind is not SourceNodeKind.TABLE and self.table_payload is not None:
            raise ValueError("table_payload is valid only for table nodes")
        if (
            self.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            and self.asset_ref is None
        ):
            raise ValueError("image and figure nodes require asset_ref")
        if self.asset_derivation is not None and (
            self.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            or self.asset_ref is None
        ):
            raise ValueError(
                "asset_derivation is valid only for image or figure asset nodes"
            )
        if self.docx_list_metadata is not None and (
            self.kind is not SourceNodeKind.LIST_ITEM
            or self.native_locator.format is not NativeDocumentFormat.DOCX
        ):
            raise ValueError(
                "docx_list_metadata is valid only for DOCX list-item nodes"
            )
        if self.docx_inline_spans:
            if self.native_locator.format is not NativeDocumentFormat.DOCX:
                raise ValueError("docx_inline_spans are valid only for DOCX nodes")
            if len(self.docx_inline_spans) != 2:
                raise ValueError(
                    "a conservative DOCX inline split requires exactly two spans"
                )
            if any(
                span.source_node_id != self.source_node_id
                for span in self.docx_inline_spans
            ):
                raise ValueError("DOCX inline spans must bind their owning SourceNode")
            first, second = self.docx_inline_spans
            if (
                first.language_evidence is not DocxInlineLanguageEvidence.EN
                or second.language_evidence is not DocxInlineLanguageEvidence.ZH_CN
                or first.start_offset != 0
                or first.end_offset != second.start_offset
                or second.end_offset != len(self.raw_text)
            ):
                raise ValueError(
                    "DOCX inline spans must completely partition raw_text as EN then zh-CN"
                )
            if len(self.docx_inline_spans) != len(set(self.docx_inline_spans)):
                raise ValueError("DOCX inline spans must not contain duplicates")
        if self.pdf_text_lines:
            if (
                self.native_locator.format is not NativeDocumentFormat.PDF
                or self.table_payload is not None
                or self.asset_ref is not None
            ):
                raise ValueError(
                    "pdf_text_lines are valid only for PDF text source nodes"
                )
            previous_end = -1
            for line in self.pdf_text_lines:
                if (
                    line.start_offset < previous_end
                    or line.end_offset > len(self.raw_text)
                    or "\n"
                    in self.raw_text[line.start_offset : line.end_offset]
                ):
                    raise ValueError(
                        "pdf_text_lines must be ordered, non-overlapping raw_text ranges"
                    )
                previous_end = line.end_offset
        if not self.raw_text and self.table_payload is None and self.asset_ref is None:
            raise ValueError("textless nodes require a table or asset payload")
        if self.parent_source_node_id == self.source_node_id:
            raise ValueError("a source node cannot be its own parent")
        if self.visual_locator is not None:
            _require_same_native_scope(
                self.native_locator,
                self.visual_locator,
                relation_name="visual_locator",
            )
        return self


class SourceIRSnapshot(_StrictFrozenModel):
    schema_version: Literal["source-ir/v1"] = SOURCE_IR_SCHEMA_VERSION
    source_ir_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    adapter_id: str = Field(min_length=1, max_length=160)
    adapter_version: str = Field(min_length=1, max_length=160)
    status: SourceIRStatus
    nodes: tuple[SourceNode, ...] = ()
    assets: tuple[SourceAssetRef, ...] = ()
    # Empty sidecars are omitted from canonical serialization so the optional
    # visual extension does not rewrite existing v1 snapshots for DOCX/XLSX or
    # born-digital PDFs.  A PDF that actually carries visual observations uses
    # the bumped PDF adapter version and serializes this field explicitly.
    visual_text_snapshots: tuple[VisualTextSnapshot, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    visual_table_page_numbers: tuple[StrictInt, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    visual_table_snapshots: tuple[VisualTableSnapshot, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    source_conversion: SourceConversionProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    diagnostics: tuple[SourceDiagnostic, ...] = ()
    created_at: datetime

    @field_validator("source_sha256")
    @classmethod
    def validate_source_sha256(cls, value: str) -> str:
        _validate_sha256(value, "source_sha256")
        return value

    @field_validator("document_id", "adapter_id", "adapter_version")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_validator("visual_table_page_numbers")
    @classmethod
    def validate_visual_table_page_numbers(
        cls,
        value: tuple[int, ...],
    ) -> tuple[int, ...]:
        if any(page_number < 1 for page_number in value):
            raise ValueError("visual table page numbers must be positive")
        if len(value) != len(set(value)) or value != tuple(sorted(value)):
            raise ValueError("visual table page numbers must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_snapshot_graph(self) -> SourceIRSnapshot:
        if self.source_conversion is not None:
            if self.source_conversion.original_source_sha256 != self.source_sha256:
                raise ValueError(
                    "source conversion must retain the snapshot source_sha256 as original truth"
                )
            if self.status is SourceIRStatus.COMPLETE:
                raise ValueError(
                    "a derived format conversion cannot claim COMPLETE native fidelity"
                )
            if self.source_conversion.derived_source_sha256 not in self.adapter_version:
                raise ValueError(
                    "adapter_version must bind the exact derived package identity"
                )
        expected_document_id = stable_document_id(self.source_sha256)
        if self.document_id != expected_document_id:
            raise ValueError("document_id must be derived from source_sha256")
        expected_snapshot_id = stable_source_ir_id(
            document_id=self.document_id,
            source_sha256=self.source_sha256,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )
        if self.source_ir_id != expected_snapshot_id:
            raise ValueError(
                "source_ir_id must be derived from source and adapter identity"
            )

        orders = [node.source_order for node in self.nodes]
        if len(orders) != len(set(orders)):
            raise ValueError("source_order must be unique within a snapshot")
        if orders != sorted(orders):
            raise ValueError("nodes must be stored in ascending source_order")

        nodes_by_id = {node.source_node_id: node for node in self.nodes}
        node_ids = set(nodes_by_id)
        if len(node_ids) != len(self.nodes):
            raise ValueError("source_node_id must be unique within a snapshot")
        assets_by_id = {asset.asset_id: asset for asset in self.assets}
        asset_ids = set(assets_by_id)
        if len(asset_ids) != len(self.assets):
            raise ValueError("asset_id must be unique within a snapshot")
        visual_snapshot_ids = {
            snapshot.visual_text_snapshot_id for snapshot in self.visual_text_snapshots
        }
        if len(visual_snapshot_ids) != len(self.visual_text_snapshots):
            raise ValueError("visual_text_snapshot_id must be unique within a snapshot")
        visual_order = [
            (
                snapshot.page_number,
                snapshot.provider_id,
                snapshot.provider_version,
                snapshot.visual_text_snapshot_id,
            )
            for snapshot in self.visual_text_snapshots
        ]
        if visual_order != sorted(visual_order):
            raise ValueError(
                "visual text snapshots must be stored in canonical page order"
            )
        for visual_snapshot in self.visual_text_snapshots:
            if visual_snapshot.source_sha256 != self.source_sha256:
                raise ValueError(
                    "visual text snapshot source_sha256 must match SourceIR"
                )

        visual_table_snapshot_ids = {
            snapshot.visual_table_snapshot_id
            for snapshot in self.visual_table_snapshots
        }
        if len(visual_table_snapshot_ids) != len(self.visual_table_snapshots):
            raise ValueError(
                "visual_table_snapshot_id must be unique within a snapshot"
            )
        visual_table_source_ids = [
            snapshot.table_source_node_id for snapshot in self.visual_table_snapshots
        ]
        if len(visual_table_source_ids) != len(set(visual_table_source_ids)):
            raise ValueError(
                "one native table can have at most one visual table snapshot"
            )
        visual_table_order = [
            (
                snapshot.page_number,
                snapshot.table_index,
                snapshot.provider_id,
                snapshot.provider_version,
                snapshot.visual_table_snapshot_id,
            )
            for snapshot in self.visual_table_snapshots
        ]
        if visual_table_order != sorted(visual_table_order):
            raise ValueError(
                "visual table snapshots must be stored in canonical page/table order"
            )
        for visual_table_snapshot in self.visual_table_snapshots:
            if visual_table_snapshot.page_number not in self.visual_table_page_numbers:
                raise ValueError(
                    "visual table snapshots must stay in the explicit authorized scope"
                )
            if visual_table_snapshot.source_sha256 != self.source_sha256:
                raise ValueError(
                    "visual table snapshot source_sha256 must match SourceIR"
                )
            table_node = nodes_by_id.get(visual_table_snapshot.table_source_node_id)
            if (
                table_node is None
                or table_node.kind is not SourceNodeKind.TABLE
                or table_node.table_payload is None
            ):
                raise ValueError(
                    "visual table snapshot must reference one native table node"
                )
            locator = table_node.native_locator
            if (
                locator.format is not NativeDocumentFormat.PDF
                or locator.page_number != visual_table_snapshot.page_number
                or locator.table_index != visual_table_snapshot.table_index
                or locator.bbox != visual_table_snapshot.table_bbox
                or table_node.table_payload.table_id != visual_table_snapshot.table_id
            ):
                raise ValueError(
                    "visual table snapshot binding must match its native PDF table"
                )

        visual_observations = {
            (snapshot.visual_text_snapshot_id, observation.observation_id): observation
            for snapshot in self.visual_text_snapshots for observation in snapshot.observations
        }
        for node in self.nodes:
            if (
                self.source_conversion is not None
                and node.native_locator.format is not NativeDocumentFormat.XLSX
            ):
                raise ValueError(
                    "XLSB conversion nodes must locate facts in the recorded derived XLSX"
                )
            identity_adapter = 'pdf-texttrace' if node.pdf_text_trace else self.adapter_id
            identity_version = node.pdf_text_trace.reader_version if node.pdf_text_trace else self.adapter_version
            if node.visual_text_ref is not None:
                observation = visual_observations.get((node.visual_text_ref.snapshot_id,
                                                       node.visual_text_ref.observation_id))
                if observation is None:
                    raise ValueError("visual projection must reference a captured observation")
                if (node.native_locator.page_number != observation.page_number
                    or node.native_locator.bbox != observation.bbox
                    or node.raw_text != observation.raw_text):
                    raise ValueError("visual projection text/page/bbox must match its observation")
                is_table = observation.content_type == "TABLE"
                if (is_table != (node.kind is SourceNodeKind.TABLE)
                    or observation.content_type not in {"TEXT", "HEADER", "FOOTER", "WATERMARK", "TABLE"}):
                    raise ValueError("visual projection kind must match its observation")
                if is_table:
                    table = node.table_payload
                    layout = observation.layout_table
                    if (table is None or layout is None
                        or (table.row_count, table.column_count) != (layout.row_count, layout.column_count)
                        or {(c.row,c.column,c.row_span,c.column_span,c.raw_text) for c in table.cells}
                        != {(c.row,c.column,c.row_span,c.column_span,c.raw_text) for c in layout.cells}):
                        raise ValueError("visual table projection must preserve observed cells and spans")
                    observed_cells = {(c.row,c.column):c for c in layout.cells}
                    for cell in table.cells:
                        expected_bbox = observed_cells[(cell.row,cell.column)].bbox or observation.bbox
                        if cell.native_locator.bbox != expected_bbox:
                            raise ValueError("visual table projection must preserve observed cell geometry")
                identity_adapter = "visual-text-projection"
                identity_version = node.visual_text_ref.snapshot_id
            expected_node_id = stable_node_id(
                source_sha256=self.source_sha256,
                adapter_id=identity_adapter,
                adapter_version=identity_version,
                kind=node.kind,
                native_locator=node.native_locator,
            )
            if node.source_node_id != expected_node_id:
                raise ValueError(
                    "source_node_id must be derived from native source identity"
                )
            if (
                node.parent_source_node_id is not None
                and node.parent_source_node_id not in node_ids
            ):
                raise ValueError(
                    "parent_source_node_id must reference a node in the snapshot"
                )
            if node.visual_locator is not None:
                _require_same_native_scope(
                    node.native_locator,
                    node.visual_locator,
                    relation_name="visual_locator",
                )
            if node.asset_ref is not None and node.asset_ref not in asset_ids:
                raise ValueError("asset_ref must reference an asset in the snapshot")
            if node.asset_derivation is not None:
                if node.asset_ref is None or node.asset_ref not in assets_by_id:
                    raise ValueError(
                        "asset_derivation requires a referenced snapshot asset"
                    )
                derived_asset = assets_by_id[node.asset_ref]
                if (
                    derived_asset.kind
                    not in {SourceAssetKind.IMAGE, SourceAssetKind.FIGURE}
                    or derived_asset.media_type != "image/png"
                ):
                    raise ValueError(
                        "alpha-mask derivations require a referenced PNG image asset"
                    )
            if node.kind is SourceNodeKind.ATTACHMENT:
                attachment_asset = (
                    assets_by_id.get(node.asset_ref)
                    if node.asset_ref is not None
                    else None
                )
                if (
                    attachment_asset is None
                    or attachment_asset.kind is not SourceAssetKind.ATTACHMENT
                    or attachment_asset.content_sha256 != self.source_sha256
                    or node.native_locator.whole_document is not True
                ):
                    raise ValueError(
                        "attachment nodes require one whole-document attachment asset bound to the source hash"
                    )
            if node.table_payload is not None:
                expected_table_id = stable_table_id(
                    source_sha256=self.source_sha256,
                    adapter_id=identity_adapter,
                    adapter_version=identity_version,
                    native_locator=node.native_locator,
                )
                if node.table_payload.table_id != expected_table_id:
                    raise ValueError(
                        "table_id must be derived from its native source identity"
                    )
                for cell in node.table_payload.cells:
                    _require_cell_locator_matches_table(node.native_locator, cell)
                if (
                    node.table_payload.visual_asset_ref is not None
                    and node.table_payload.visual_asset_ref not in asset_ids
                ):
                    raise ValueError(
                        "visual_asset_ref must reference an asset in the snapshot"
                    )

        for asset in self.assets:
            if asset.kind is not SourceAssetKind.ATTACHMENT:
                continue
            matching_nodes = tuple(
                node
                for node in self.nodes
                if node.kind is SourceNodeKind.ATTACHMENT
                and node.asset_ref == asset.asset_id
            )
            if (
                len(matching_nodes) != 1
                or asset.content_sha256 != self.source_sha256
                or asset.native_locator is None
                or asset.native_locator.whole_document is not True
            ):
                raise ValueError(
                    "attachment assets require one source-bound whole-document node"
                )

        _require_acyclic_parent_graph(nodes_by_id)

        diagnostics = list(self.diagnostics)
        diagnostics.extend(
            diagnostic
            for node in self.nodes
            for diagnostic in node.extraction_diagnostics
        )
        for diagnostic in diagnostics:
            if (
                diagnostic.source_node_id is not None
                and diagnostic.source_node_id not in node_ids
            ):
                raise ValueError(
                    "diagnostic source_node_id must reference a node in the snapshot"
                )
            if (
                diagnostic.source_node_id is not None
                and diagnostic.native_locator is not None
            ):
                referenced_node = nodes_by_id[diagnostic.source_node_id]
                _require_same_native_scope(
                    referenced_node.native_locator,
                    diagnostic.native_locator,
                    relation_name="diagnostic native_locator",
                )
        locator_formats = {
            locator.format for locator in _snapshot_native_locators(self)
        }
        if len(locator_formats) > 1:
            raise ValueError(
                "all native locators in a snapshot must use one document format"
            )
        return self


def _snapshot_native_locators(
    snapshot: SourceIRSnapshot,
) -> tuple[NativeLocator, ...]:
    locators: list[NativeLocator] = []
    for node in snapshot.nodes:
        locators.append(node.native_locator)
        if node.visual_locator is not None:
            locators.append(node.visual_locator)
        if node.table_payload is not None:
            locators.extend(cell.native_locator for cell in node.table_payload.cells)
        locators.extend(
            diagnostic.native_locator
            for diagnostic in node.extraction_diagnostics
            if diagnostic.native_locator is not None
        )
    locators.extend(
        asset.native_locator
        for asset in snapshot.assets
        if asset.native_locator is not None
    )
    locators.extend(
        diagnostic.native_locator
        for diagnostic in snapshot.diagnostics
        if diagnostic.native_locator is not None
    )
    return tuple(locators)


def stable_source_alpha_footprint_evidence_sha256(
    *,
    width_px: int,
    height_px: int,
    runs: tuple[SourceAlphaFootprintRun, ...],
    nonzero_sample_count: int,
) -> str:
    """Bind the canonical exact non-zero-alpha RLE without asset bytes."""

    payload = {
        "schema_version": "source-alpha-footprint/v1",
        "width_px": width_px,
        "height_px": height_px,
        "nonzero_sample_count": nonzero_sample_count,
        "runs": [[run.y, run.x_start, run.x_end] for run in runs],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def stable_document_id(source_sha256: str) -> str:
    """Return the stable content identity; upload/role identity stays elsewhere."""

    _validate_sha256(source_sha256, "source_sha256")
    return f"doc-{source_sha256[:32]}"


def stable_source_ir_id(
    *,
    document_id: str,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
) -> str:
    _require_non_blank(document_id, "document_id")
    _validate_sha256(source_sha256, "source_sha256")
    _require_non_blank(adapter_id, "adapter_id")
    _require_non_blank(adapter_version, "adapter_version")
    return _stable_id(
        "sir",
        document_id,
        source_sha256,
        adapter_id,
        adapter_version,
    )


def stable_node_id(
    *,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    kind: SourceNodeKind,
    native_locator: NativeLocator,
) -> str:
    _validate_sha256(source_sha256, "source_sha256")
    _require_non_blank(adapter_id, "adapter_id")
    _require_non_blank(adapter_version, "adapter_version")
    if not isinstance(kind, SourceNodeKind):
        raise TypeError("kind must be a SourceNodeKind value")
    if not isinstance(native_locator, NativeLocator):
        raise TypeError("native_locator must be a NativeLocator value")
    return _stable_id(
        "node",
        source_sha256,
        adapter_id,
        adapter_version,
        kind.value,
        _canonical_model(native_locator),
    )


def stable_table_id(
    *,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    native_locator: NativeLocator,
) -> str:
    _validate_sha256(source_sha256, "source_sha256")
    _require_non_blank(adapter_id, "adapter_id")
    _require_non_blank(adapter_version, "adapter_version")
    if not isinstance(native_locator, NativeLocator):
        raise TypeError("native_locator must be a NativeLocator value")
    return _stable_id(
        "table",
        source_sha256,
        adapter_id,
        adapter_version,
        _canonical_model(native_locator),
    )


def stable_cell_id(table_id: str, row: int, column: int) -> str:
    _require_non_blank(table_id, "table_id")
    if row < 0 or column < 0:
        raise ValueError("cell row and column must not be negative")
    return _stable_id("cell", table_id, row, column)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _canonical_model(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _region_coverage(region: MergedRegion) -> set[tuple[int, int]]:
    return {
        (row, column)
        for row in range(region.row, region.row + region.row_span)
        for column in range(region.column, region.column + region.column_span)
    }


def _cell_coverage(cell: TableCellIR) -> set[tuple[int, int]]:
    return {
        (row, column)
        for row in range(cell.row, cell.row + cell.row_span)
        for column in range(cell.column, cell.column + cell.column_span)
    }


def _require_cell_locator_matches_table(
    table_locator: NativeLocator,
    cell: TableCellIR,
) -> None:
    locator = cell.native_locator
    if locator.format is not table_locator.format:
        raise ValueError("table cell native_locator format must match its table node")

    if table_locator.format is NativeDocumentFormat.PDF:
        if table_locator.table_index is None:
            raise ValueError("PDF table node locator requires table_index")
        if (
            locator.page_number != table_locator.page_number
            or locator.table_index != table_locator.table_index
        ):
            raise ValueError(
                "PDF table cell native_locator must stay in its table page and table_index"
            )
        if locator.row_index != cell.row or locator.column_index != cell.column:
            raise ValueError(
                "PDF table cell native_locator row/column must match the cell anchor"
            )
        return

    if table_locator.format is NativeDocumentFormat.DOCX:
        if table_locator.table_index is None:
            raise ValueError("DOCX table node locator requires table_index")
        if (
            locator.body_element_index != table_locator.body_element_index
            or locator.table_index != table_locator.table_index
        ):
            raise ValueError(
                "DOCX table cell native_locator must stay in its body element and table_index"
            )
        if locator.row_index != cell.row or locator.column_index != cell.column:
            raise ValueError(
                "DOCX table cell native_locator row/column must match the cell anchor"
            )
        return

    if (
        locator.workbook != table_locator.workbook
        or locator.sheet != table_locator.sheet
    ):
        raise ValueError(
            "XLSX table cell native_locator must stay in its workbook and sheet"
        )
    table_row, table_column = _xlsx_range_origin(table_locator.cell_range or "")
    cell_row, cell_column = _xlsx_range_origin(locator.cell_range or "")
    if cell_row != table_row + cell.row or cell_column != table_column + cell.column:
        raise ValueError(
            "XLSX table cell native_locator coordinate must match the cell anchor"
        )


def _require_same_native_scope(
    node_locator: NativeLocator,
    related_locator: NativeLocator,
    *,
    relation_name: str,
) -> None:
    if related_locator.format is not node_locator.format:
        raise ValueError(f"{relation_name} format must match its source node")

    if node_locator.format is NativeDocumentFormat.PDF:
        if related_locator.page_number != node_locator.page_number:
            raise ValueError(f"{relation_name} must stay on its source node PDF page")
        if (
            node_locator.table_index is not None
            or related_locator.table_index is not None
        ) and related_locator.table_index != node_locator.table_index:
            raise ValueError(f"{relation_name} must stay in its source node PDF table")
        return

    if node_locator.format is NativeDocumentFormat.DOCX:
        if related_locator.body_element_index != node_locator.body_element_index:
            raise ValueError(
                f"{relation_name} must stay in its source node DOCX body element"
            )
        if (
            node_locator.table_index is not None
            or related_locator.table_index is not None
        ) and related_locator.table_index != node_locator.table_index:
            raise ValueError(f"{relation_name} must stay in its source node DOCX table")
        return

    if (
        related_locator.workbook != node_locator.workbook
        or related_locator.sheet != node_locator.sheet
    ):
        raise ValueError(
            f"{relation_name} must stay in its source node XLSX workbook and sheet"
        )


def _require_acyclic_parent_graph(nodes_by_id: dict[str, SourceNode]) -> None:
    completed: set[str] = set()
    for start_id in nodes_by_id:
        if start_id in completed:
            continue
        path: set[str] = set()
        node_id: str | None = start_id
        while node_id is not None and node_id not in completed:
            if node_id in path:
                raise ValueError("parent_source_node_id graph must be acyclic")
            path.add(node_id)
            node_id = nodes_by_id[node_id].parent_source_node_id
        completed.update(path)


def _xlsx_range_origin(cell_range: str) -> tuple[int, int]:
    match = _XLSX_RANGE_PARTS_PATTERN.fullmatch(cell_range)
    if (
        match is None
    ):  # NativeLocator already validates this; retain fail-closed locality.
        raise ValueError("cell_range must be an A1-style XLSX range")
    column_letters, row_text = match.group(1), match.group(2)
    column = 0
    for character in column_letters:
        column = column * 26 + (ord(character) - ord("A") + 1)
    return int(row_text), column


__all__ = [
    "SOURCE_IR_SCHEMA_VERSION",
    "DocxInlineBoundaryBasis",
    "DocxInlineLanguageEvidence",
    "DocxInlineSpan",
    "DocxListMetadata",
    "MergedRegion",
    "NativeDocumentFormat",
    "NativeLocator",
    "PdfTextLineGeometry",
    "VisualTextProjectionRef",
    "SourceAlphaFootprint",
    "SourceAlphaFootprintRun",
    "SourceAssetKind",
    "SourceAssetDerivation",
    "SourceAssetDerivationKind",
    "SourceAssetRef",
    "SourceDiagnostic",
    "SourceDiagnosticSeverity",
    "SourceIRSnapshot",
    "SourceIRStatus",
    "SourceNode",
    "SourceNodeKind",
    "TableCellIR",
    "TableIR",
    "VisualTextSnapshot",
    "stable_cell_id",
    "stable_document_id",
    "stable_node_id",
    "stable_source_alpha_footprint_evidence_sha256",
    "stable_source_ir_id",
    "stable_table_id",
]
