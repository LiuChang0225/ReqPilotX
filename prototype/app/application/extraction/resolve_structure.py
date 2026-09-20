from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from enum import Enum

from app.application.extraction.structure_boundary_rules import (
    BODY_APPENDIX_HEADING as _BODY_APPENDIX_HEADING,
    EmbeddedPeerMarkerEvidence as _EmbeddedPeerMarkerEvidence,
    EXPLICIT_LIST_PREFIX as _EXPLICIT_LIST_PREFIX,
    EXPLICIT_SECTION_PREFIX as _EXPLICIT_SECTION_PREFIX,
    FIGURE_BODY_REFERENCE as _FIGURE_BODY_REFERENCE,
    FIGURE_CAPTION_PREFIX as _FIGURE_CAPTION_PREFIX,
    IDEOGRAPHIC_NUMBERED_PREFIX as _IDEOGRAPHIC_NUMBERED_PREFIX,
    ROOT_MEASUREMENT_PREFIX as _ROOT_MEASUREMENT_PREFIX,
    TABLE_CAPTION_PREFIX as _TABLE_CAPTION_PREFIX,
    bare_dotted_section_label_from_text as _bare_dotted_section_label_from_text,
    body_appendix_heading as _body_appendix_heading,
    body_appendix_identity as _body_appendix_identity,
    embedded_parenthesized_peer_sequences as _embedded_parenthesized_peer_sequences,
    explicit_section_label_from_text as _explicit_section_label_from_text,
    match_has_parenthesized_suffix as _match_has_parenthesized_suffix,
    normalize_boundary_title as _normalize_boundary_title,
    parse_alphabetic_parenthesized_marker as _parse_alphabetic_parenthesized_marker,
)
from app.application.extraction.structure_boundary_decisions import (
    credible_alphabetic_list_ordinals as _credible_alphabetic_list_ordinals,
    filter_unconfirmed_implicit_cjk_root_labels as _filter_unconfirmed_implicit_cjk_root_labels,
)
from app.application.extraction.pdf_table_text_arbitration import (
    NATIVE_NUMBER_REFERENCE_TRAILER as _NATIVE_NUMBER_REFERENCE_TRAILER,
    PDF_TABLE_TEXT_MIN_COVERAGE as _PDF_TABLE_TEXT_MIN_COVERAGE,
    PdfTableTextSelection as _PdfTableTextSelection,
    bbox_coverage as _bbox_coverage,
    cells_covering_row as _cells_covering_row,
    credible_single_dot_root_matches as _credible_single_dot_root_matches,
    document_flow_pdf_table_source_ids as _document_flow_pdf_table_source_ids,
    embedded_section_marker_is_wrapped_reference as _embedded_section_marker_is_wrapped_reference,
    expanded_bbox as _expanded_bbox,
    folded_pdf_table_text_nodes as _folded_pdf_table_text_nodes,
)
from app.domain.extraction.pdf_page_furniture import (
    pdf_image_furniture_kind_from_diagnostic_codes,
    visual_pdf_page_furniture_ids,
)
from app.domain.extraction.pdf_page_raster import (
    proven_pdf_page_raster_source_ids,
)
from app.domain.extraction.explicit_peer_items import (
    ExplicitNumericPeerItem,
    parse_explicit_numeric_peer_items,
)
from app.domain.extraction.source_ir import (
    DocxInlineBoundaryBasis,
    DocxInlineLanguageEvidence,
    NativeDocumentFormat,
    SourceAssetKind,
    SourceAssetDerivationKind,
    SourceAssetRef,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
)
from app.domain.extraction.visual_text import VisualTextObservation
from app.domain.extraction.visual_table_reconciliation import (
    VisualTableReconciliationRejected,
    reconcile_visual_table_snapshot,
)
from app.domain.extraction.structure import (
    BilingualCounterpartGroup,
    BilingualLanguageEvidence,
    BilingualMismatchSignal,
    BilingualSourceSpan,
    BilingualSpanBoundaryBasis,
    DocumentLanguagePolicy,
    StructureBoundaryBasis,
    StructureFinding,
    StructureFindingSeverity,
    StructureNode,
    StructureNodeKind,
    StructureOptionState,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureSourceSpan,
    StructureSourceSpanBoundaryBasis,
    StructureTreeSnapshot,
    VisualTableCellTextBinding,
    stable_bilingual_counterpart_group_id,
    stable_bilingual_mismatch_signal_id,
    stable_bilingual_source_span_id,
    stable_document_language_policy_id,
    stable_structure_finding_id,
    stable_structure_node_id,
    stable_structure_relation_id,
    stable_structure_source_span_id,
    stable_structure_snapshot_id,
)


DEFAULT_STRUCTURE_RESOLVER_ID = "deterministic-outline-structure"
DEFAULT_STRUCTURE_RESOLVER_VERSION = "2.48.1"

_PDF_BILINGUAL_PAIRED_RUN_MIN_SIZE = 3
_PDF_BILINGUAL_OWNER_X_TOLERANCE_POINTS = 6.0
_PDF_BILINGUAL_COUNTERPART_X_TOLERANCE_POINTS = 12.0
_PDF_BILINGUAL_PAIR_MAX_X_SHIFT_POINTS = 36.0
_PDF_BILINGUAL_PAIR_MAX_VERTICAL_GAP_POINTS = 36.0
_BILINGUAL_LEADING_ORDINAL = re.compile(
    r"^\s*(?:(?:\d{1,4}|[A-Za-z])\s*[)）.、]|[（(]\s*\d{1,4}\s*[)）])\s*"
)
_BILINGUAL_ARABIC_NUMBER = re.compile(
    r"(?<![A-Za-z])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z])"
)

_PDF_IMAGE_CELL_MIN_COVERAGE = 0.98
_PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE = 0.02
_PDF_IMAGE_CELL_BOUNDARY_TOLERANCE_POINTS = 3.0
_PDF_IMAGE_CELL_MIN_DOMINANT_COVERAGE = 0.90
_PDF_IMAGE_CELL_MAX_BOUNDARY_COMPETING_COVERAGE = 0.05
_PDF_IMAGE_CELL_MIN_DIMENSION_POINTS = 16.0
_PDF_IMAGE_CELL_MIN_OPAQUE_MULTI_CELL_COVERAGE = 0.25
_PDF_IMAGE_CELL_MAX_MULTI_CELL_COUNT = 16
_PDF_IMAGE_CELL_MAX_MULTI_CELL_GRID_SLOTS = 64
_PDF_IMAGE_CELL_NATIVE_BOUNDARY_TOLERANCE_POINTS = 0.25
_PDF_IMAGE_CELL_MIN_SHARED_BOUNDARY_POINTS = 1.0
_TABLE_HORIZONTAL_ALIGNMENT_RATIO = 0.03
_TABLE_HORIZONTAL_ALIGNMENT_MIN_POINTS = 6.0
_TABLE_COLUMN_GRID_ALIGNMENT_RATIO = 0.01
_TABLE_COLUMN_GRID_ALIGNMENT_MIN_POINTS = 2.0
_TABLE_PREVIOUS_PAGE_BOTTOM_RATIO = 0.75
_TABLE_NEXT_PAGE_TOP_RATIO = 0.30
_TABLE_SPARSE_PAGE_TOP_MAX_POINTS = 96.0
_TABLE_SPARSE_PAGE_TOP_ALIGNMENT_TOLERANCE_POINTS = 6.0
_TABLE_SPARSE_PAGE_CONTENT_EXTENT_TOLERANCE_POINTS = 2.0
_MAX_REPEATED_LEADING_ROWS = 3
_VISUAL_LINE_MAX_X_SHIFT = 60.0
_VISUAL_LINE_MAX_GAP_RATIO = 0.70
_NATIVE_CONTINUATION_MAX_X_SHIFT = 60.0
_NATIVE_CONTINUATION_MAX_GAP_RATIO = 1.25
_VISUAL_TEXT_AUTHORITY_DIAGNOSTIC_CODES = frozenset(
    {
        "pdf_full_page_image_with_invisible_text_layer",
        "pdf_full_page_image_without_visible_text",
        "pdf_vector_text_with_low_native_unicode_coverage",
        "pdf_visual_text_user_forced",
    }
)
_VISUAL_TEXT_SUPERSEDED_NATIVE_KINDS = frozenset(
    {
        SourceNodeKind.HEADING,
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST,
        SourceNodeKind.LIST_ITEM,
        SourceNodeKind.NOTE,
        SourceNodeKind.HEADER,
        SourceNodeKind.FOOTER,
        SourceNodeKind.WATERMARK,
        SourceNodeKind.TOC_ENTRY,
    }
)
_SENTENCE_TERMINATOR = re.compile(r"[.!?。！？；;：:]\s*$")
_LIST_ITEM_CLOSING_TERMINATOR = re.compile(r"[.!?。！？；;]\s*$")
_TABLE_INTRODUCTION_TERMINATOR = re.compile(r"[：:]\s*$")
_CAPTION_BOUNDARY_SEPARATOR = re.compile(r"^[ \t\u00a0\u3000:：.．。\-‐‑–—]*")
_CAPTION_BODY_REFERENCE_PREFIX = re.compile(
    r"^(?:"
    r"中|内|的|为|"
    r"所(?:述|示|列|载|定义|规定|要求)|"
    r"(?:必须|不得|严禁|禁止|不允许)|"
    r"(?:shall|must|should|will|may)\b|"
    r"(?:is|are|was|were)\b|"
    r"(?:shows?|lists?|defines?|specifies?|provides?|contains?|applies?)\b|"
    r"see\b|refer(?:s|red|ring)?\b"
    r")",
    re.IGNORECASE,
)
_CAPTION_UNSEPARATED_BODY_PREDICATE_PREFIX = re.compile(
    r"^(?:规定|要求|显示|给出|列出|说明|定义)"
)
_CAPTION_NORMATIVE_PREDICATE = re.compile(
    r"(?:"
    r"\b(?:shall|must|should|will|(?:is|are)\s+required\s+to)\b|"
    r"必须|不得|严禁|禁止|不允许|"
    r"应(?:由|当|按|在|为|向|将|能|可|符合|满足|具有|提供|提交|保持|采用|执行|达到|确保|包括|不)|"
    r"须(?:由|按|在|为|经|向|将|提供|提交|满足|符合|采用|执行)|"
    r"可(?:以|由|按|在|为|向|将|提供|提交|满足|符合|采用|执行|支持)|"
    r"将(?:由|按|在|为|向|提供|提交|满足|符合|采用|执行)"
    r")",
    re.IGNORECASE,
)
_ENGLISH_LANGUAGE_POLICY_EQUALITY = re.compile(
    r"\b(?:equal(?:ly)?\s+valid|equal\s+(?:legal\s+)?validity|"
    r"same\s+(?:legal\s+)?(?:effect|validity)|both\s+versions?\s+(?:are\s+)?valid)\b",
    re.IGNORECASE,
)
_ENGLISH_LANGUAGE_POLICY_CONFLICT = re.compile(
    r"\b(?:conflict|discrepanc(?:y|ies)|inconsisten(?:cy|cies)|difference)\b",
    re.IGNORECASE,
)
_ENGLISH_CHINESE_PRECEDENCE = re.compile(
    r"\bchinese(?:\s+version)?\b.{0,80}\b"
    r"(?:prevail|govern|take\s+(?:priority|precedence)|be\s+decisive)\b",
    re.IGNORECASE,
)
_ENGLISH_ENGLISH_PRECEDENCE = re.compile(
    r"\benglish(?:\s+version)?\b.{0,80}\b"
    r"(?:prevail|govern|take\s+(?:priority|precedence)|be\s+decisive)\b",
    re.IGNORECASE,
)
_CHINESE_LANGUAGE_POLICY_EQUALITY = re.compile(
    r"(?:中英文|中文.{0,20}英文|英文.{0,20}中文).{0,50}"
    r"(?:同等(?:法律)?效力|同样有效|效力相同)"
)
_CHINESE_LANGUAGE_POLICY_CONFLICT = re.compile(r"(?:冲突|不一致|歧义|差异)")
_CHINESE_CHINESE_PRECEDENCE = re.compile(
    r"(?:以\s*中文(?:版本)?\s*为准|中文(?:版本)?.{0,20}(?:优先|为准))"
)
_CHINESE_ENGLISH_PRECEDENCE = re.compile(
    r"(?:以\s*英文(?:版本)?\s*为准|英文(?:版本)?\s*(?:优先|为准))"
)

_EMBEDDED_EXPLICIT_SECTION_PREFIX = re.compile(
    r"^[ \t]*(?P<label>[1-9][0-9]{0,2}"
    r"(?:[ \t]*\.[ \t]*(?:0|[1-9][0-9]{0,2})){1,7})"
    r"(?P<trailing_dot>[.．])?"
    r"(?=$|[ \t\r\n]|[)）]|[\u3400-\u9fff])",
    re.MULTILINE,
)
_TRAILING_EXPLICIT_SECTION_LABEL = re.compile(
    r"(?<![0-9.．])"
    r"(?P<label>[1-9][0-9]{0,2}(?:[ \t]*[.．][ \t]*(?:0|[1-9][0-9]{0,2})){1,7})"
    r"[ \t]*$"
)
_ATTACHMENT_DIRECTORY_TITLE = re.compile(
    r"^(?:附件|附录)\s*(?:目录|清单)$|"
    r"^(?:appendix|annex)\s+(?:index|list|contents)$",
    re.IGNORECASE,
)
_ATTACHMENT_DIRECTORY_ROW = re.compile(
    r"^\s*(?P<ordinal>[1-9][0-9]{0,2})\s*"
    r"(?:[\r\n]+|[)）.．、:：|-]\s*|\s{2,})"
    r"(?P<title>\S(?:.|\n){0,160})\s*$"
)
_NATIVE_BLOCK_ITEM_PREFIX = re.compile(
    r"^\s*(?:"
    r"[A-Za-z][)）.]|"
    r"[（(]?(?:[1-9][0-9]{0,3}|[一二三四五六七八九十百两]{1,4})"
    r"[)）、.]|"
    r"\[[ \t]*(?:√|✓|✔|x|X|？|\?)?[ \t]*\]|"
    r"[-•●▪■◆◇]|"
    r"[nlu]\s*[\r\n]"
    r")"
)
_CHINESE_SECTION_PREFIX = re.compile(
    r"^\s*(?:第\s*)?(?P<ordinal>[零〇一二三四五六七八九十百两]{1,6})"
    r"(?:\s*章|\s*[、．.])\s*(?P<title>\S.*)$"
)
_TOC_NUMERIC_ENTRY_PREFIX = re.compile(
    r"^\s*[1-9][0-9]{0,2}(?:\s*\.\s*(?:0|[1-9][0-9]{0,2})){0,7}"
    r"(?=$|\s|[\u3400-\u9fff])"
)
_TOC_APPENDIX_ENTRY_PREFIX = re.compile(
    r"^\s*(?:附录|附件|appendix)\s*[A-Z0-9一二三四五六七八九十]?\s*\S",
    re.IGNORECASE,
)
_TOC_APPENDIX_LABEL_PREFIX = re.compile(
    r"^\s*(?P<label>(?:附录|附件)\s*[A-Z0-9一二三四五六七八九十]?"
    r"|appendix(?:\s+[A-Z0-9])?)(?=$|\s)",
    re.IGNORECASE,
)
_TOC_PAGE_TARGET_SUFFIX = re.compile(
    r"(?:[.．·•…_]{2,}|\s{2,})\s*"
    r"(?P<target>[1-9][0-9]{0,3}|[IVXLCDM]{1,10})\s*$",
    re.IGNORECASE,
)
_TOC_STRONG_CONTINUATION_MIN_TARGET_LINES = 2
_TOC_HEADINGLESS_SEED_MIN_TARGET_LINES = 3
_TOC_HEADINGLESS_SEED_MIN_OUTLINE_LINES = 2
_TOC_WEAK_SEED_MIN_ENTRY_LINES = 3
_TOC_WEAK_CONTINUATION_MIN_ENTRY_LINES = 2
_TOC_LAYOUT_BREAK_LINE_HEIGHT_MULTIPLIER = 4.0
_TRAILING_SECTION_MAX_SOURCE_GAP = 6
_IDEOGRAPHIC_CHAPTER_MAX_SOURCE_GAP = 16
_IDEOGRAPHIC_CHILD_EVIDENCE_MAX_SOURCE_GAP = 32
_ATTACHMENT_DIRECTORY_MAX_ROW_SCAN = 96
_ATTACHMENT_DIRECTORY_MAX_ENTRY_SKIP = 3
_ATTACHMENT_DIRECTORY_MIN_MATCHED_REGIONS = 2
_ATTACHMENT_REGION_TITLE_MAX_CHARACTERS = 100
_ATTACHMENT_REGION_TITLE_TOP_RATIO = 0.28
_ATTACHMENT_REGION_TITLE_MAX_WIDTH_RATIO = 0.95
_LIST_SCOPE_INDENT_TOLERANCE_POINTS = 12.0
_TABLE_NOTE_PREFIX = re.compile(r"^\s*(?:注|备注|说明|note)\s*[:：]", re.IGNORECASE)
_TABLE_PARAMETER_VALUE_PATTERN = re.compile(
    r"(?:[±≤≥<>%％]|\+\s*/\s*-|\b(?:min|max|tolerances?)\b)",
    re.IGNORECASE,
)
_EXTERNAL_REFERENCE_SECTION_SIGNAL = re.compile(
    r"(?:规范性|适用|参考|引用)(?:文件|标准|资料)"
    r"|\b(?:normative\s+references?|reference\s+documents?|"
    r"applicable\s+standards?)\b",
    re.IGNORECASE,
)
_SQUARE_OPTION_MARKER = re.compile(r"^\s*\[\s*(?P<mark>[^\]\r\n]{0,4})\s*\]")
_PAREN_OPTION_MARKER = re.compile(r"^\s*[（(]\s*(?P<mark>[√✓✔xX×]?)\s*[）)]")
_BOX_OPTION_MARKER = re.compile(r"^\s*(?P<mark>[☐☑☒□])")
_CHECKED_OPTION_MARKS = frozenset({"√", "✓", "✔", "x", "X", "×", "☑", "☒"})
_UNCHECKED_OPTION_MARKS = frozenset({"", "☐", "□"})


class StructureResolutionRejectionCode(str, Enum):
    SOURCE_IR_FAILED = "source_ir_failed"
    SOURCE_CATALOG_MISMATCH = "source_catalog_mismatch"
    INVALID_TABLE_GROUNDING = "invalid_table_grounding"


class StructureResolutionRejected(RuntimeError):
    def __init__(
        self,
        code: StructureResolutionRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _AppliesToSpec:
    source_structure_node_id: str
    target_structure_node_id: str
    source_refs: tuple[StructureSourceRef, ...]


@dataclass(frozen=True)
class _ContinuationSpec:
    source_structure_node_id: str
    target_structure_node_id: str
    source_refs: tuple[StructureSourceRef, ...]


@dataclass(frozen=True)
class _ReferenceSpec:
    source_structure_node_id: str
    target_structure_node_id: str
    source_refs: tuple[StructureSourceRef, ...]


@dataclass(frozen=True)
class _CellPlacementSpec:
    kind: StructureRelationKind
    source_structure_node_id: str
    target_structure_node_id: str
    source_refs: tuple[StructureSourceRef, ...]


@dataclass(frozen=True)
class _PdfImageCellPlacementMatch:
    cell: TableCellIR
    boundary_tolerant: bool


@dataclass(frozen=True)
class _PdfImageMultiCellPlacementMatch:
    cells: tuple[TableCellIR, ...]


@dataclass(frozen=True)
class _OptionStateSpec:
    source_structure_node_id: str
    target_structure_node_id: str
    source_refs: tuple[StructureSourceRef, ...]
    option_state: StructureOptionState


@dataclass(frozen=True)
class _VisualObservationGroup:
    observations: tuple[VisualTextObservation, ...]

    @property
    def first(self) -> VisualTextObservation:
        return self.observations[0]

    @property
    def raw_text(self) -> str:
        return "\n".join(item.raw_text for item in self.observations)

    @property
    def source_refs(self) -> tuple[StructureSourceRef, ...]:
        return tuple(_visual_ref(item.observation_id) for item in self.observations)


@dataclass(frozen=True)
class _VisualTableCellTextBindingSpec:
    table_source_node_id: str
    table_cell_id: str
    visual_table_cell_id: str


@dataclass(frozen=True)
class _VisualTableFactIssue:
    code: str
    table_source_node_ids: tuple[str, ...]
    table_cell_ids: tuple[str, ...] = ()
    blocking: bool = True


@dataclass(frozen=True)
class _TocTextProfile:
    line_count: int
    entry_line_count: int
    target_line_count: int


@dataclass(frozen=True)
class _TocPageScan:
    nodes: tuple[SourceNode, ...]
    line_count: int
    entry_line_count: int
    target_line_count: int
    consumed_all: bool
    layout_break: bool


@dataclass(frozen=True)
class _TocTargetEntry:
    label: str | None
    title_key: str
    target_page_number: int


@dataclass(frozen=True)
class _TocBodyOutlineEvidence:
    page_number: int
    label: str | None
    title_key: str


@dataclass(frozen=True)
class _ExplicitSectionSegment:
    label: str
    start_offset: int
    end_offset: int


class _OutlineMarkerFamily(str, Enum):
    """Canonical outline families accepted by the deterministic resolver.

    The enum describes notation, not a fixed global depth.  Numeric and
    alphabetic parenthesized markers can each introduce the other in common
    ISO/GB list grammars; active scope and peer continuity decide the local
    depth.  Rare typography remains outside this contract until it has its own
    independently sequenced evidence.
    """

    DOTTED_OUTLINE = "dotted_outline"
    NUMERIC_IDEOGRAPHIC = "numeric_ideographic"
    NUMERIC_PARENTHESIZED = "numeric_parenthesized"
    ALPHABETIC_PARENTHESIZED = "alphabetic_parenthesized"


_COMMON_CHILD_MARKER_FAMILIES: dict[
    _OutlineMarkerFamily,
    frozenset[_OutlineMarkerFamily],
] = {
    _OutlineMarkerFamily.DOTTED_OUTLINE: frozenset(
        {
            _OutlineMarkerFamily.NUMERIC_IDEOGRAPHIC,
            _OutlineMarkerFamily.NUMERIC_PARENTHESIZED,
            _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED,
        }
    ),
    _OutlineMarkerFamily.NUMERIC_IDEOGRAPHIC: frozenset(
        {
            _OutlineMarkerFamily.NUMERIC_PARENTHESIZED,
            _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED,
        }
    ),
    _OutlineMarkerFamily.NUMERIC_PARENTHESIZED: frozenset(
        {_OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED}
    ),
    _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED: frozenset(
        {_OutlineMarkerFamily.NUMERIC_PARENTHESIZED}
    ),
}


@dataclass(frozen=True)
class _ListScope:
    structure_node_id: str
    parent_structure_node_id: str
    marker_family: _OutlineMarkerFamily
    ordinal: int
    indent: float | None
    peer_count: int


@dataclass(frozen=True)
class _ListParentResolution:
    parent_structure_node_id: str
    retained_scopes: tuple[_ListScope, ...]
    peer_count: int


@dataclass(frozen=True)
class _EmbeddedPeerSegment:
    marker_family: _OutlineMarkerFamily
    ordinal: int
    start_offset: int
    end_offset: int
    text_indent: float


def build_structure_tree(
    snapshot: SourceIRSnapshot,
    *,
    resolver_id: str = DEFAULT_STRUCTURE_RESOLVER_ID,
    resolver_version: str = DEFAULT_STRUCTURE_RESOLVER_VERSION,
) -> StructureTreeSnapshot:
    """Resolve deterministic outline, list, continuation, and table structure.

    PDF adapters intentionally preserve native blocks instead of guessing
    headings.  This layer therefore derives *boundaries* from explicit numeric
    labels and native geometry while leaving customer text in SourceIR.  Page
    changes never close an outline scope.  Requirement classification remains
    a later semantic concern.
    """

    if not isinstance(snapshot, SourceIRSnapshot):
        raise TypeError("snapshot must be a SourceIRSnapshot")
    if snapshot.status is SourceIRStatus.FAILED:
        raise StructureResolutionRejected(
            StructureResolutionRejectionCode.SOURCE_IR_FAILED,
            "A failed SourceIR observation cannot produce a StructureTree snapshot.",
        )

    tree = _derive_structure_tree(
        snapshot,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
    )
    validate_structure_tree_against_source_ir(tree, snapshot)
    return tree


def visual_text_authority_pages(snapshot: SourceIRSnapshot) -> frozenset[int]:
    """Return pages whose adopted body-text channel is visual OCR.

    SourceIR remains a lossless catalog of both native and visual facts.  A
    page becomes visual-authoritative either because the adapter declared a
    visual-required/forced risk, or because a source-bound snapshot actually
    evaluated TEXT.  An evaluated-but-empty response remains authoritative and
    must fail closed; otherwise downstream code could silently fall back to a
    native text layer after OCR already became the selected channel.
    """

    if not isinstance(snapshot, SourceIRSnapshot):
        raise TypeError("snapshot must be a SourceIRSnapshot")
    pages = {
        diagnostic.native_locator.page_number
        for diagnostic in snapshot.diagnostics
        if diagnostic.code in _VISUAL_TEXT_AUTHORITY_DIAGNOSTIC_CODES
        and diagnostic.native_locator is not None
        and diagnostic.native_locator.page_number is not None
    }
    pages.update(
        visual_snapshot.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
        if "TEXT" in visual_snapshot.evaluated_content_types
    )
    return frozenset(pages)


def superseded_native_text_source_ids(
    snapshot: SourceIRSnapshot,
) -> frozenset[str]:
    """Identify native text facts retained for audit but excluded from Structure."""

    authority_pages = visual_text_authority_pages(snapshot)
    return frozenset(
        node.source_node_id
        for node in snapshot.nodes
        if node.kind in _VISUAL_TEXT_SUPERSEDED_NATIVE_KINDS
        and node.native_locator.page_number in authority_pages
    )


def _visual_text_authority_findings(
    snapshot: SourceIRSnapshot,
    *,
    resolver_id: str,
    resolver_version: str,
) -> tuple[StructureFinding, ...]:
    nonempty_visual_text_pages = {
        visual_snapshot.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
        if "TEXT" in visual_snapshot.evaluated_content_types
        and any(
            observation.content_type == "TEXT" and observation.raw_text.strip()
            for observation in visual_snapshot.observations
        )
    }
    findings: list[StructureFinding] = []
    for page_number in sorted(
        visual_text_authority_pages(snapshot) - nonempty_visual_text_pages
    ):
        identity_payload = f"pdf_ocr_authority_text_unresolved:page:{page_number}"
        identity_key = (
            "finding:"
            + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
        )
        findings.append(
            StructureFinding(
                finding_id=stable_structure_finding_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    code="pdf_ocr_authority_text_unresolved",
                    identity_key=identity_key,
                ),
                identity_key=identity_key,
                code="pdf_ocr_authority_text_unresolved",
                severity=StructureFindingSeverity.ERROR,
                blocking=True,
                message_key="structure.pdf_ocr_authority_text_unresolved",
            )
        )
    return tuple(findings)


def _derive_structure_tree(
    snapshot: SourceIRSnapshot,
    *,
    resolver_id: str,
    resolver_version: str,
) -> StructureTreeSnapshot:
    structure_snapshot_id = stable_structure_snapshot_id(
        source_ir_id=snapshot.source_ir_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
    )
    root_identity_key = "document"
    root_id = stable_structure_node_id(
        source_ir_id=snapshot.source_ir_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        kind=StructureNodeKind.DOCUMENT,
        identity_key=root_identity_key,
    )

    source_nodes_by_id = {node.source_node_id: node for node in snapshot.nodes}
    folded_table_cell_nodes: dict[tuple[str, int], tuple[SourceNode, ...]] = (
        _folded_table_cell_nodes(snapshot)
    )
    (
        folded_pdf_table_nodes,
        folded_pdf_table_row_selections,
        _table_outline_source_ids,
        ambiguous_pdf_table_source_ids,
    ) = _folded_pdf_table_text_nodes(snapshot)
    folded_source_node_ids = {
        node.source_node_id
        for nodes in folded_table_cell_nodes.values()
        for node in nodes
    }
    folded_source_node_ids.update(
        selection.source_node.source_node_id
        for selections in folded_pdf_table_row_selections.values()
        for selection in selections
    )
    folded_source_node_ids.update(
        node.source_node_id
        for nodes in folded_pdf_table_nodes.values()
        for node in nodes
    )
    document_flow_table_source_ids = _document_flow_pdf_table_source_ids(snapshot)
    visual_text_snapshots = tuple(snapshot.visual_text_snapshots)
    all_visual_text_observations = tuple(
        sorted(
            (
                observation
                for visual_snapshot in visual_text_snapshots
                for observation in visual_snapshot.observations
            ),
            key=lambda observation: (
                observation.page_number,
                observation.bbox[1],
                observation.bbox[0],
                observation.observation_index,
                observation.observation_id,
            ),
        )
    )
    (
        visual_table_cell_binding_specs,
        visual_table_fact_issues,
    ) = _verified_visual_table_cell_specs(snapshot)
    # Page-layout snapshots also carry TABLE/FIGURE/furniture facts.  They stay
    # in the immutable grounding catalog, but only actual body/TOC text can
    # create document structure.
    visual_text_observations = tuple(
        observation
        for observation in all_visual_text_observations
        if observation.content_type == "TEXT" and observation.raw_text.strip()
    )
    untrusted_native_pages = set(visual_text_authority_pages(snapshot))
    authority_superseded_source_ids = superseded_native_text_source_ids(snapshot)
    page_raster_source_ids = proven_pdf_page_raster_source_ids(snapshot)
    # Fold visual-authority native text out of the adopted Structure while its
    # immutable SourceIR fact remains available for audit/diff.  Including the
    # IDs in the ordinary parent-folding set also guarantees a retained table,
    # image, figure or attachment never points at a parent we did not emit.
    folded_source_node_ids.update(authority_superseded_source_ids)
    # Page-canvas rasters are immutable SourceIR/page-inspection support, not
    # document structure. Folding them before parent-first ordering guarantees
    # they cannot become CONTENT nodes, semantic targets, or RequirementTree
    # figure refs while the original assets and locators remain auditable.
    folded_source_node_ids.update(page_raster_source_ids)
    ordered_source_nodes = _parent_first_source_nodes(
        tuple(
            sorted(
                (
                    node
                    for node in snapshot.nodes
                    if node.source_node_id not in folded_source_node_ids
                ),
                key=_structure_reading_key,
            )
        ),
        source_nodes_by_id=source_nodes_by_id,
        folded_source_node_ids=folded_source_node_ids,
    )
    furniture_source_ids = {
        node.source_node_id
        for node in ordered_source_nodes
        if (
            node.kind
            in {
                SourceNodeKind.HEADER,
                SourceNodeKind.FOOTER,
                SourceNodeKind.WATERMARK,
            }
            or _is_pdf_image_furniture_node(node)
        )
    }
    toc_groups_by_anchor = _toc_entry_groups(
        ordered_source_nodes,
        furniture_source_ids=furniture_source_ids,
    )
    toc_source_ids = {
        node.source_node_id for group in toc_groups_by_anchor.values() for node in group
    }
    toc_chinese_title_keys = {
        key
        for group in toc_groups_by_anchor.values()
        for node in group[1:]
        for key in _chinese_section_title_keys(node.raw_text)
    }
    toc_outline_title_keys = frozenset(
        _normalize_boundary_title(_toc_outline_label_and_title(node.raw_text)[1])
        for group in toc_groups_by_anchor.values()
        for node in group[1:]
        if _normalize_boundary_title(_toc_outline_label_and_title(node.raw_text)[1])
    )
    folded_toc_source_ids = {
        node.source_node_id
        for group in toc_groups_by_anchor.values()
        for node in group[1:]
    }
    folded_source_node_ids.update(folded_toc_source_ids)
    structural_source_nodes = tuple(
        node
        for node in ordered_source_nodes
        if node.source_node_id not in folded_toc_source_ids
    )
    embedded_section_segments = {
        node.source_node_id: segments
        for node in structural_source_nodes
        if (segments := _embedded_explicit_section_segments(node.raw_text))
        and node.source_node_id not in toc_source_ids
        and node.source_node_id not in furniture_source_ids
        and node.source_node_id not in ambiguous_pdf_table_source_ids
        and node.native_locator.page_number not in untrusted_native_pages
        and node.kind in {SourceNodeKind.HEADING, SourceNodeKind.PARAGRAPH}
    }
    chinese_section_labels = _credible_chinese_section_labels(
        ordered_source_nodes,
        excluded_source_ids=(
            toc_source_ids | furniture_source_ids | ambiguous_pdf_table_source_ids
        ),
        toc_title_keys=toc_chinese_title_keys,
    )
    explicit_labels = {
        node.source_node_id: (
            None
            if node.source_node_id in toc_source_ids
            or node.source_node_id in furniture_source_ids
            or node.source_node_id in ambiguous_pdf_table_source_ids
            or node.native_locator.page_number in untrusted_native_pages
            else chinese_section_labels.get(node.source_node_id)
            or _explicit_section_label(node)
        )
        for node in structural_source_nodes
    }
    explicit_labels = _veto_native_continuation_section_labels(
        structural_source_nodes,
        explicit_labels=explicit_labels,
        excluded_source_ids=(
            toc_source_ids
            | furniture_source_ids
            | ambiguous_pdf_table_source_ids
            | set(embedded_section_segments)
        ),
    )
    explicit_labels = _filter_unconfirmed_implicit_cjk_root_labels(
        structural_source_nodes,
        explicit_labels=explicit_labels,
        excluded_source_ids=(
            toc_source_ids | furniture_source_ids | ambiguous_pdf_table_source_ids
        ),
        toc_title_keys=toc_outline_title_keys,
    )
    # A split bare label may be bracketed by a peer whose number is printed at
    # the end of the preceding native block.  Recover those already-proven
    # peer labels for sequence evidence before evaluating the bare token, then
    # rebuild the trailing segments after promotion so both passes retain the
    # same final outline context.  Neither pass copies or rewrites source text.
    preliminary_trailing_section_segments = _credible_trailing_section_segments(
        structural_source_nodes,
        explicit_labels=explicit_labels,
        embedded_section_segments=embedded_section_segments,
        excluded_source_ids=(
            toc_source_ids | furniture_source_ids | ambiguous_pdf_table_source_ids
        ),
    )
    if snapshot.status is SourceIRStatus.COMPLETE:
        bare_peer_labels = dict(explicit_labels)
        bare_peer_labels.update(
            {
                source_id: segments[0].label
                for source_id, segments in preliminary_trailing_section_segments.items()
                if segments
            }
        )
        explicit_labels.update(
            _credible_bare_dotted_section_labels(
                structural_source_nodes,
                explicit_labels=bare_peer_labels,
                excluded_source_ids=(
                    toc_source_ids
                    | furniture_source_ids
                    | ambiguous_pdf_table_source_ids
                    | set(embedded_section_segments)
                ),
            )
        )
    trailing_section_segments = _credible_trailing_section_segments(
        structural_source_nodes,
        explicit_labels=explicit_labels,
        embedded_section_segments=embedded_section_segments,
        excluded_source_ids=(
            toc_source_ids | furniture_source_ids | ambiguous_pdf_table_source_ids
        ),
    )
    for source_id, segments in trailing_section_segments.items():
        embedded_section_segments.setdefault(source_id, segments)
    for source_id, segments in embedded_section_segments.items():
        explicit_labels[source_id] = segments[0].label
    document_region_heading_source_ids = set(
        _credible_body_appendix_heading_source_ids(
            structural_source_nodes,
            excluded_source_ids=(
                toc_source_ids | furniture_source_ids | ambiguous_pdf_table_source_ids
            ),
            untrusted_native_pages=untrusted_native_pages,
        )
    )
    document_region_heading_source_ids.update(
        _credible_attachment_directory_region_headings(
            snapshot,
            structural_source_nodes=structural_source_nodes,
            excluded_source_ids=(
                toc_source_ids | furniture_source_ids | ambiguous_pdf_table_source_ids
            ),
        )
    )
    region_root_segments = _credible_document_region_root_segments(
        structural_source_nodes,
        document_region_heading_source_ids=document_region_heading_source_ids,
        embedded_section_segments=embedded_section_segments,
        explicit_labels=explicit_labels,
        table_outline_source_ids=_table_outline_source_ids,
    )
    for source_id, segments in region_root_segments.items():
        embedded_section_segments[source_id] = segments
        explicit_labels[source_id] = segments[0].label
    explicit_labels.update(
        _credible_ideographic_chapter_labels(
            structural_source_nodes,
            explicit_labels=explicit_labels,
            excluded_source_ids=(
                toc_source_ids
                | furniture_source_ids
                | ambiguous_pdf_table_source_ids
                | document_region_heading_source_ids
            ),
        )
    )
    ideographic_list_ordinals = _credible_ideographic_list_ordinals(
        structural_source_nodes,
        explicit_labels=explicit_labels,
        excluded_source_ids=(
            toc_source_ids
            | furniture_source_ids
            | ambiguous_pdf_table_source_ids
            | document_region_heading_source_ids
        ),
    )
    if snapshot.status is SourceIRStatus.COMPLETE:
        embedded_peer_segments, embedded_peer_followup_ordinals = (
            _credible_embedded_peer_segments(
                structural_source_nodes,
                explicit_labels=explicit_labels,
                excluded_source_ids=(
                    toc_source_ids
                    | furniture_source_ids
                    | ambiguous_pdf_table_source_ids
                    | document_region_heading_source_ids
                ),
                embedded_section_segments=embedded_section_segments,
            )
        )
    else:
        embedded_peer_segments, embedded_peer_followup_ordinals = {}, {}
    alphabetic_list_ordinals = _credible_alphabetic_list_ordinals(
        structural_source_nodes,
        explicit_labels=explicit_labels,
        excluded_source_ids=(
            toc_source_ids
            | furniture_source_ids
            | ambiguous_pdf_table_source_ids
            | document_region_heading_source_ids
        ),
        boundary_source_ids=document_region_heading_source_ids,
        allow_closed_pairs=snapshot.status is SourceIRStatus.COMPLETE,
    )
    alphabetic_list_ordinals.update(embedded_peer_followup_ordinals)
    list_ordinals = {
        node.source_node_id: (
            None
            if explicit_labels[node.source_node_id] is not None
            or node.source_node_id in document_region_heading_source_ids
            else None
            if node.native_locator.page_number in untrusted_native_pages
            else _explicit_list_ordinal(node)
            or ideographic_list_ordinals.get(node.source_node_id)
            or alphabetic_list_ordinals.get(node.source_node_id)
        )
        for node in structural_source_nodes
    }
    outline_marker_families = {
        node.source_node_id: _accepted_outline_marker_family(
            explicit_label=explicit_labels[node.source_node_id],
            list_ordinal=list_ordinals[node.source_node_id],
            ideographic_numeric=(node.source_node_id in ideographic_list_ordinals),
            alphabetic_parenthesized=(node.source_node_id in alphabetic_list_ordinals),
        )
        for node in structural_source_nodes
    }
    source_primary_kind = {
        node.source_node_id: _primary_structure_kind(
            node,
            explicit_label=explicit_labels[node.source_node_id],
            list_ordinal=list_ordinals[node.source_node_id],
            toc_block=node.source_node_id in toc_groups_by_anchor,
            document_region_heading=(
                node.source_node_id in document_region_heading_source_ids
            ),
        )
        for node in structural_source_nodes
    }
    source_primary_identity = {
        source_node.source_node_id: f"source:{source_node.source_node_id}"
        for source_node in structural_source_nodes
    }
    source_primary_id = {
        source_node.source_node_id: stable_structure_node_id(
            source_ir_id=snapshot.source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            kind=source_primary_kind[source_node.source_node_id],
            identity_key=source_primary_identity[source_node.source_node_id],
        )
        for source_node in structural_source_nodes
    }

    structure_nodes: list[StructureNode] = [
        StructureNode(
            structure_node_id=root_id,
            identity_key=root_identity_key,
            kind=StructureNodeKind.DOCUMENT,
            source_order=0,
            source_refs=(),
            boundary_basis=StructureBoundaryBasis.DOCUMENT,
        )
    ]
    structure_kind_by_id = {root_id: StructureNodeKind.DOCUMENT}
    next_order = 1
    applies_to_specs: list[_AppliesToSpec] = []
    continuation_specs: list[_ContinuationSpec] = []
    findings = list(
        _visual_text_authority_findings(
            snapshot,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
        )
    )
    emitted_source_ids: set[str] = set()
    section_stack: list[tuple[str, str]] = []
    last_section_by_label: dict[str, str] = {}
    current_document_region_id: str | None = None
    current_section_id: str | None = None
    current_clause_id: str | None = None
    current_list_scopes: list[_ListScope] = []
    current_parenthesized_outline_scope: _ListScope | None = None
    inside_parenthesized_outline_section = False
    folded_region_sources_by_table_id = _folded_document_region_sources_by_table(
        document_region_heading_source_ids=document_region_heading_source_ids,
        table_level_nodes=folded_pdf_table_nodes,
        row_level_selections=folded_pdf_table_row_selections,
    )
    last_continuation_anchor_by_section: dict[str, tuple[int, str]] = {}
    continued_pages_by_section: set[tuple[str, int]] = set()
    for source_index, source_node in enumerate(structural_source_nodes):
        source_id = source_node.source_node_id
        folded_region_sources = folded_region_sources_by_table_id.get(
            source_id,
            (),
        )
        if len(folded_region_sources) == 1:
            region_source = folded_region_sources[0]
            region_identity_key = f"source:{region_source.source_node_id}"
            region_id = stable_structure_node_id(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureNodeKind.SECTION,
                identity_key=region_identity_key,
            )
            structure_nodes.append(
                StructureNode(
                    structure_node_id=region_id,
                    identity_key=region_identity_key,
                    kind=StructureNodeKind.SECTION,
                    parent_structure_node_id=root_id,
                    source_order=next_order,
                    source_refs=(_node_ref(region_source.source_node_id),),
                    boundary_basis=StructureBoundaryBasis.NATIVE_BLOCK,
                )
            )
            structure_kind_by_id[region_id] = StructureNodeKind.SECTION
            current_document_region_id = region_id
            current_section_id = region_id
            current_clause_id = None
            current_list_scopes.clear()
            current_parenthesized_outline_scope = None
            inside_parenthesized_outline_section = False
            section_stack = []
            last_section_by_label = {}
            next_order += 1
        retained_parent_source_id = _nearest_retained_parent_source_id(
            source_node,
            source_nodes_by_id=source_nodes_by_id,
            folded_source_node_ids=folded_source_node_ids,
        )
        explicit_label = explicit_labels[source_id]
        list_ordinal = list_ordinals[source_id]
        primary_kind = source_primary_kind[source_id]
        is_furniture = (
            source_id in furniture_source_ids
            or (
                source_node.native_locator.page_number in untrusted_native_pages
                and source_node.kind is SourceNodeKind.PARAGRAPH
            )
            or _is_page_canvas_image(source_node)
            or source_id in toc_source_ids
            or source_node.kind
            in {
                SourceNodeKind.HEADER,
                SourceNodeKind.FOOTER,
                SourceNodeKind.WATERMARK,
                SourceNodeKind.TOC_ENTRY,
            }
        )

        embedded_segments = embedded_section_segments.get(source_id, ())
        split_document_region = bool(
            embedded_segments
            and source_id in document_region_heading_source_ids
            and retained_parent_source_id is None
            and not is_furniture
        )
        if split_document_region:
            region_id = source_primary_id[source_id]
            structure_nodes.append(
                StructureNode(
                    structure_node_id=region_id,
                    identity_key=source_primary_identity[source_id],
                    kind=StructureNodeKind.SECTION,
                    parent_structure_node_id=root_id,
                    source_order=next_order,
                    source_refs=(_node_ref(source_id),),
                    boundary_basis=StructureBoundaryBasis.NATIVE_BLOCK,
                )
            )
            structure_kind_by_id[region_id] = StructureNodeKind.SECTION
            current_document_region_id = region_id
            current_section_id = region_id
            current_clause_id = None
            current_list_scopes.clear()
            current_parenthesized_outline_scope = None
            inside_parenthesized_outline_section = False
            section_stack = []
            last_section_by_label = {}
            next_order += 1
        if (
            embedded_segments
            and retained_parent_source_id is None
            and not is_furniture
            and source_id not in folded_pdf_table_nodes
            and source_id not in toc_groups_by_anchor
        ):
            source_ref = _node_ref(source_id)
            prefix_end_offset = embedded_segments[0].start_offset
            if source_node.raw_text[:prefix_end_offset].strip():
                followup_ordinal = embedded_peer_followup_ordinals.get(source_id)
                prefix_kind = (
                    StructureNodeKind.CLAUSE
                    if followup_ordinal is not None
                    else StructureNodeKind.CONTENT
                )
                followup_marker = (
                    _parse_alphabetic_parenthesized_marker(
                        source_node.raw_text[:prefix_end_offset],
                        require_payload=False,
                    )
                    if followup_ordinal is not None
                    else None
                )
                if followup_ordinal is not None and followup_marker is None:
                    raise StructureResolutionRejected(
                        StructureResolutionRejectionCode.SOURCE_CATALOG_MISMATCH,
                        "An embedded peer follow-up must retain its exact source marker.",
                    )
                if followup_ordinal is not None:
                    assert followup_marker is not None
                    prefix_indent = _source_text_offset_indent(
                        source_node,
                        offset=followup_marker.start_offset,
                    )
                    parent_resolution = _list_parent_structure_node_id(
                        current_scopes=tuple(current_list_scopes),
                        current_section_id=current_section_id,
                        root_id=current_document_region_id or root_id,
                        marker_family=(
                            _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED
                        ),
                        ordinal=followup_ordinal,
                        indent=prefix_indent,
                    )
                    prefix_parent_id = parent_resolution.parent_structure_node_id
                    current_list_scopes[:] = parent_resolution.retained_scopes
                else:
                    prefix_indent = None
                    prefix_parent_id = (
                        current_clause_id or current_section_id or root_id
                    )
                prefix_identity_key = f"source:{source_id}:span:0:{prefix_end_offset}"
                prefix_id = stable_structure_node_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    kind=prefix_kind,
                    identity_key=prefix_identity_key,
                )
                prefix_span = StructureSourceSpan(
                    structure_source_span_id=stable_structure_source_span_id(
                        source_ref=source_ref,
                        start_offset=0,
                        end_offset=prefix_end_offset,
                        boundary_basis=(
                            StructureSourceSpanBoundaryBasis.EXPLICIT_NUMBER
                        ),
                    ),
                    source_ref=source_ref,
                    start_offset=0,
                    end_offset=prefix_end_offset,
                    boundary_basis=(StructureSourceSpanBoundaryBasis.EXPLICIT_NUMBER),
                )
                structure_nodes.append(
                    StructureNode(
                        structure_node_id=prefix_id,
                        identity_key=prefix_identity_key,
                        kind=prefix_kind,
                        parent_structure_node_id=prefix_parent_id,
                        source_order=next_order,
                        source_refs=(source_ref,),
                        semantic_source_spans=(prefix_span,),
                        boundary_basis=(
                            StructureBoundaryBasis.EXPLICIT_NUMBER
                            if followup_ordinal is not None
                            else StructureBoundaryBasis.NATIVE_BLOCK
                        ),
                        list_ordinal=followup_ordinal,
                    )
                )
                structure_kind_by_id[prefix_id] = prefix_kind
                if not split_document_region:
                    source_primary_id[source_id] = prefix_id
                if followup_ordinal is not None:
                    current_clause_id = prefix_id
                    current_scope = _ListScope(
                        structure_node_id=prefix_id,
                        parent_structure_node_id=prefix_parent_id,
                        marker_family=(
                            _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED
                        ),
                        ordinal=followup_ordinal,
                        indent=prefix_indent,
                        peer_count=parent_resolution.peer_count,
                    )
                    current_list_scopes.append(current_scope)
                    if (
                        current_parenthesized_outline_scope is not None
                        and all(
                            scope.structure_node_id
                            != current_parenthesized_outline_scope.structure_node_id
                            for scope in current_list_scopes
                        )
                    ):
                        current_parenthesized_outline_scope = None
                    inside_parenthesized_outline_section = False
                next_order += 1
            for segment in embedded_segments:
                identity_key = (
                    f"source:{source_id}:span:"
                    f"{segment.start_offset}:{segment.end_offset}"
                )
                segment_id = stable_structure_node_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    kind=StructureNodeKind.SECTION,
                    identity_key=identity_key,
                )
                segment_text = source_node.raw_text[
                    segment.start_offset : segment.end_offset
                ]
                parenthesized_outline_child = (
                    _parenthesized_section_descends_from_outline_scope(
                        segment_text,
                        explicit_label=segment.label,
                        outline_scope=current_parenthesized_outline_scope,
                    )
                )
                if (
                    parenthesized_outline_child
                    and current_parenthesized_outline_scope is not None
                    and len(_logical_outline_parts(segment.label)) == 2
                ):
                    parent_id = current_parenthesized_outline_scope.structure_node_id
                    missing_parent_label = None
                else:
                    parent_id, missing_parent_label = _section_parent(
                        segment.label,
                        section_stack=section_stack,
                        last_section_by_label=last_section_by_label,
                        root_id=current_document_region_id or root_id,
                    )
                if missing_parent_label is not None:
                    finding_identity = (
                        "finding:"
                        + hashlib.sha256(
                            (
                                f"{source_id}:{segment.start_offset}:"
                                "section_ancestor_missing"
                            ).encode("utf-8")
                        ).hexdigest()
                    )
                    finding_code = "section_ancestor_missing"
                    findings.append(
                        StructureFinding(
                            finding_id=stable_structure_finding_id(
                                source_ir_id=snapshot.source_ir_id,
                                resolver_id=resolver_id,
                                resolver_version=resolver_version,
                                code=finding_code,
                                identity_key=finding_identity,
                            ),
                            identity_key=finding_identity,
                            code=finding_code,
                            severity=StructureFindingSeverity.WARNING,
                            blocking=False,
                            message_key="structure.section_ancestor_missing",
                            structure_node_id=segment_id,
                            source_refs=(source_ref,),
                        )
                    )
                source_span = StructureSourceSpan(
                    structure_source_span_id=stable_structure_source_span_id(
                        source_ref=source_ref,
                        start_offset=segment.start_offset,
                        end_offset=segment.end_offset,
                        boundary_basis=(
                            StructureSourceSpanBoundaryBasis.EXPLICIT_NUMBER
                        ),
                    ),
                    source_ref=source_ref,
                    start_offset=segment.start_offset,
                    end_offset=segment.end_offset,
                    boundary_basis=(StructureSourceSpanBoundaryBasis.EXPLICIT_NUMBER),
                )
                structure_nodes.append(
                    StructureNode(
                        structure_node_id=segment_id,
                        identity_key=identity_key,
                        kind=StructureNodeKind.SECTION,
                        parent_structure_node_id=parent_id,
                        source_order=next_order,
                        source_refs=(source_ref,),
                        semantic_source_spans=(source_span,),
                        boundary_basis=StructureBoundaryBasis.EXPLICIT_NUMBER,
                        explicit_label=segment.label,
                    )
                )
                structure_kind_by_id[segment_id] = StructureNodeKind.SECTION
                if not split_document_region:
                    source_primary_id[source_id] = segment_id
                current_section_id = segment_id
                current_clause_id = None
                current_list_scopes.clear()
                inside_parenthesized_outline_section = parenthesized_outline_child
                if not inside_parenthesized_outline_section:
                    current_parenthesized_outline_scope = None
                section_stack = _updated_section_stack(
                    segment.label,
                    segment_id,
                    section_stack=section_stack,
                )
                last_section_by_label[segment.label] = segment_id
                next_order += 1
            emitted_source_ids.add(source_id)
            continue

        embedded_peer_run = embedded_peer_segments.get(source_id, ())
        if (
            embedded_peer_run
            and retained_parent_source_id is None
            and not is_furniture
            and source_id not in folded_pdf_table_nodes
            and source_id not in toc_groups_by_anchor
        ):
            source_ref = _node_ref(source_id)
            prefix_end_offset = embedded_peer_run[0].start_offset
            primary_assigned = False
            if source_node.raw_text[:prefix_end_offset].strip():
                prefix_identity_key = f"source:{source_id}:span:0:{prefix_end_offset}"
                prefix_id = stable_structure_node_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    kind=StructureNodeKind.CONTENT,
                    identity_key=prefix_identity_key,
                )
                prefix_span = _exact_structure_source_span(
                    source_ref=source_ref,
                    start_offset=0,
                    end_offset=prefix_end_offset,
                )
                structure_nodes.append(
                    StructureNode(
                        structure_node_id=prefix_id,
                        identity_key=prefix_identity_key,
                        kind=StructureNodeKind.CONTENT,
                        parent_structure_node_id=(
                            current_clause_id or current_section_id or root_id
                        ),
                        source_order=next_order,
                        source_refs=(source_ref,),
                        semantic_source_spans=(prefix_span,),
                        boundary_basis=StructureBoundaryBasis.NATIVE_BLOCK,
                    )
                )
                structure_kind_by_id[prefix_id] = StructureNodeKind.CONTENT
                source_primary_id[source_id] = prefix_id
                primary_assigned = True
                next_order += 1

            base_indent = _source_node_indent(source_node)
            for segment in embedded_peer_run:
                indent = (
                    None
                    if base_indent is None
                    else base_indent + segment.text_indent
                )
                parent_resolution = _list_parent_structure_node_id(
                    current_scopes=tuple(current_list_scopes),
                    current_section_id=current_section_id,
                    root_id=current_document_region_id or root_id,
                    marker_family=segment.marker_family,
                    ordinal=segment.ordinal,
                    indent=indent,
                )
                parent_id = parent_resolution.parent_structure_node_id
                current_list_scopes[:] = parent_resolution.retained_scopes
                identity_key = (
                    f"source:{source_id}:span:"
                    f"{segment.start_offset}:{segment.end_offset}"
                )
                segment_id = stable_structure_node_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    kind=StructureNodeKind.CLAUSE,
                    identity_key=identity_key,
                )
                segment_span = _exact_structure_source_span(
                    source_ref=source_ref,
                    start_offset=segment.start_offset,
                    end_offset=segment.end_offset,
                )
                structure_nodes.append(
                    StructureNode(
                        structure_node_id=segment_id,
                        identity_key=identity_key,
                        kind=StructureNodeKind.CLAUSE,
                        parent_structure_node_id=parent_id,
                        source_order=next_order,
                        source_refs=(source_ref,),
                        semantic_source_spans=(segment_span,),
                        boundary_basis=StructureBoundaryBasis.EXPLICIT_NUMBER,
                        list_ordinal=segment.ordinal,
                    )
                )
                structure_kind_by_id[segment_id] = StructureNodeKind.CLAUSE
                if not primary_assigned:
                    source_primary_id[source_id] = segment_id
                    primary_assigned = True
                current_clause_id = segment_id
                current_scope = _ListScope(
                    structure_node_id=segment_id,
                    parent_structure_node_id=parent_id,
                    marker_family=segment.marker_family,
                    ordinal=segment.ordinal,
                    indent=indent,
                    peer_count=parent_resolution.peer_count,
                )
                current_list_scopes.append(current_scope)
                if (
                    segment.marker_family
                    is _OutlineMarkerFamily.NUMERIC_PARENTHESIZED
                ):
                    current_parenthesized_outline_scope = current_scope
                elif current_parenthesized_outline_scope is not None and all(
                    scope.structure_node_id
                    != current_parenthesized_outline_scope.structure_node_id
                    for scope in current_list_scopes
                ):
                    current_parenthesized_outline_scope = None
                inside_parenthesized_outline_section = False
                next_order += 1
            emitted_source_ids.add(source_id)
            continue

        if retained_parent_source_id is not None:
            if retained_parent_source_id not in emitted_source_ids:
                raise StructureResolutionRejected(
                    StructureResolutionRejectionCode.SOURCE_CATALOG_MISMATCH,
                    "A native SourceIR parent must precede its retained child.",
                )
            parent_id = source_primary_id[retained_parent_source_id]
        elif is_furniture:
            parent_id = root_id
        elif (
            primary_kind is StructureNodeKind.TABLE
            and source_id in document_flow_table_source_ids
        ):
            # A page-layout table remains immutable evidence but is not the
            # semantic owner of the full-width document flow it encloses.
            # Root it as a support fact; the restored native paragraphs bind
            # naturally through the ordinary section stack.
            parent_id = root_id
        elif primary_kind is StructureNodeKind.SECTION:
            if source_id in document_region_heading_source_ids:
                parent_id = root_id
                missing_parent_label = None
                section_stack = []
                last_section_by_label = {}
            else:
                parenthesized_outline_child = (
                    _parenthesized_section_descends_from_outline_scope(
                        source_node.raw_text,
                        explicit_label=explicit_label,
                        outline_scope=current_parenthesized_outline_scope,
                    )
                )
                if (
                    parenthesized_outline_child
                    and current_parenthesized_outline_scope is not None
                    and explicit_label is not None
                    and len(_logical_outline_parts(explicit_label)) == 2
                ):
                    parent_id = current_parenthesized_outline_scope.structure_node_id
                    missing_parent_label = None
                else:
                    parent_id, missing_parent_label = _section_parent(
                        explicit_label,
                        section_stack=section_stack,
                        last_section_by_label=last_section_by_label,
                        root_id=current_document_region_id or root_id,
                    )
            if missing_parent_label is not None:
                finding_identity = (
                    "finding:"
                    + hashlib.sha256(
                        f"{source_id}:section_ancestor_missing".encode("utf-8")
                    ).hexdigest()
                )
                finding_code = "section_ancestor_missing"
                findings.append(
                    StructureFinding(
                        finding_id=stable_structure_finding_id(
                            source_ir_id=snapshot.source_ir_id,
                            resolver_id=resolver_id,
                            resolver_version=resolver_version,
                            code=finding_code,
                            identity_key=finding_identity,
                        ),
                        identity_key=finding_identity,
                        code=finding_code,
                        severity=StructureFindingSeverity.WARNING,
                        blocking=False,
                        message_key="structure.section_ancestor_missing",
                        structure_node_id=source_primary_id[source_id],
                        source_refs=(_node_ref(source_id),),
                    )
                )
            current_section_id = source_primary_id[source_id]
            current_clause_id = None
            current_list_scopes.clear()
            inside_parenthesized_outline_section = (
                parenthesized_outline_child
                if source_id not in document_region_heading_source_ids
                else False
            )
            if not inside_parenthesized_outline_section:
                current_parenthesized_outline_scope = None
            if source_id in document_region_heading_source_ids:
                current_document_region_id = current_section_id
            section_stack = _updated_section_stack(
                explicit_label,
                current_section_id,
                section_stack=section_stack,
            )
            if explicit_label is not None:
                last_section_by_label[explicit_label] = current_section_id
        elif primary_kind is StructureNodeKind.CLAUSE:
            marker_family = outline_marker_families[source_id]
            if (
                marker_family is None
                or marker_family is _OutlineMarkerFamily.DOTTED_OUTLINE
                or list_ordinal is None
            ):
                raise StructureResolutionRejected(
                    StructureResolutionRejectionCode.SOURCE_CATALOG_MISMATCH,
                    "A list clause must retain one accepted marker family.",
                )
            indent = _source_node_indent(source_node)
            continues_parenthesized_outline = bool(
                inside_parenthesized_outline_section
                and marker_family is _OutlineMarkerFamily.NUMERIC_PARENTHESIZED
                and current_parenthesized_outline_scope is not None
                and list_ordinal == current_parenthesized_outline_scope.ordinal + 1
                and _list_indents_share_level(
                    indent,
                    current_parenthesized_outline_scope.indent,
                )
            )
            if (
                continues_parenthesized_outline
                and current_parenthesized_outline_scope is not None
            ):
                parent_id = current_parenthesized_outline_scope.parent_structure_node_id
                peer_count = current_parenthesized_outline_scope.peer_count + 1
                current_list_scopes.clear()
            else:
                parent_resolution = _list_parent_structure_node_id(
                    current_scopes=tuple(current_list_scopes),
                    current_section_id=current_section_id,
                    root_id=current_document_region_id or root_id,
                    marker_family=marker_family,
                    ordinal=list_ordinal,
                    indent=indent,
                )
                parent_id = parent_resolution.parent_structure_node_id
                peer_count = parent_resolution.peer_count
                current_list_scopes[:] = parent_resolution.retained_scopes
            current_clause_id = source_primary_id[source_id]
            current_scope = _ListScope(
                structure_node_id=current_clause_id,
                parent_structure_node_id=parent_id,
                marker_family=marker_family,
                ordinal=list_ordinal,
                indent=indent,
                peer_count=peer_count,
            )
            current_list_scopes.append(current_scope)
            if marker_family is _OutlineMarkerFamily.NUMERIC_PARENTHESIZED:
                # Documents commonly express one outline level as ``6）`` and
                # its descendants as ``6.1）`` / ``6.1.1）``.  The printed
                # parenthesized item is therefore a real ancestry anchor for
                # later dotted labels, not a disposable list-style detail.
                # Keep that temporary list scope separate from the explicit
                # section-label index.  Reusing the same map would let a
                # nested ``N）`` item overwrite the real root section ``N``
                # and trap a later ``N.x`` sibling inside the list.
                current_parenthesized_outline_scope = current_scope
            elif current_parenthesized_outline_scope is not None and all(
                scope.structure_node_id
                != current_parenthesized_outline_scope.structure_node_id
                for scope in current_list_scopes
            ):
                # A competing equal/higher marker closed the old numeric
                # outline.  A later ``2）`` must bind to the active scope, not
                # jump back across intervening ``6、`` / ``7、`` peers.
                current_parenthesized_outline_scope = None
            inside_parenthesized_outline_section = False
        else:
            active_list_scope = current_list_scopes[-1] if current_list_scopes else None
            previous_source_node = (
                structural_source_nodes[source_index - 1] if source_index > 0 else None
            )
            following_source_node = (
                structural_source_nodes[source_index + 1]
                if source_index + 1 < len(structural_source_nodes)
                else None
            )
            closes_active_list_scope = (
                current_clause_id is not None
                and active_list_scope is not None
                and current_clause_id == active_list_scope.structure_node_id
                and _list_scope_closes_before_adjacent_table_introduction(
                    active_scope=active_list_scope,
                    previous_source_node=previous_source_node,
                    introduction_source_node=source_node,
                    following_source_node=following_source_node,
                )
            )
            if closes_active_list_scope and active_list_scope is not None:
                parent_id = active_list_scope.parent_structure_node_id
                current_list_scopes.pop()
                if (
                    current_parenthesized_outline_scope is not None
                    and current_parenthesized_outline_scope.structure_node_id
                    == active_list_scope.structure_node_id
                ):
                    current_parenthesized_outline_scope = None
                current_clause_id = (
                    parent_id
                    if structure_kind_by_id.get(parent_id) is StructureNodeKind.CLAUSE
                    else None
                )
            else:
                parent_id = current_clause_id or current_section_id or root_id

        primary_source_refs = (
            (_node_ref(source_id),)
            + tuple(
                _node_ref(node.source_node_id)
                for node in toc_groups_by_anchor.get(source_id, ())[1:]
            )
            + tuple(
                _node_ref(node.source_node_id)
                for node in folded_pdf_table_nodes.get(source_id, ())
                if node.source_node_id not in authority_superseded_source_ids
            )
        )
        structure_nodes.append(
            StructureNode(
                structure_node_id=source_primary_id[source_id],
                identity_key=source_primary_identity[source_id],
                kind=primary_kind,
                parent_structure_node_id=parent_id,
                source_order=next_order,
                source_refs=primary_source_refs,
                semantic_source_refs=(
                    ()
                    if source_node.kind is SourceNodeKind.TABLE
                    and source_node.native_locator.page_number
                    in untrusted_native_pages
                    else None
                ),
                boundary_basis=_primary_boundary_basis(
                    source_node,
                    explicit_label=explicit_label,
                    list_ordinal=list_ordinal,
                    toc_block=source_id in toc_groups_by_anchor,
                ),
                explicit_label=explicit_label,
                list_ordinal=list_ordinal,
            )
        )
        emitted_source_ids.add(source_id)
        structure_kind_by_id[source_primary_id[source_id]] = primary_kind
        next_order += 1

        page_number = source_node.native_locator.page_number
        if (
            current_section_id is not None
            and page_number is not None
            and not is_furniture
            and primary_kind
            not in {
                StructureNodeKind.SECTION,
                StructureNodeKind.DOCUMENT,
            }
            and source_node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        ):
            previous = last_continuation_anchor_by_section.get(current_section_id)
            page_key = (current_section_id, page_number)
            previous_kind = (
                structure_kind_by_id.get(previous[1]) if previous is not None else None
            )
            if (
                previous is not None
                and previous[0] < page_number
                and page_key not in continued_pages_by_section
                and not (
                    previous_kind is StructureNodeKind.TABLE
                    and primary_kind is StructureNodeKind.TABLE
                )
                and not (
                    previous_kind is StructureNodeKind.TABLE
                    and _is_explicit_table_continuation_caption(
                        source_node.raw_text
                    )
                )
            ):
                continuation_specs.append(
                    _ContinuationSpec(
                        source_structure_node_id=previous[1],
                        target_structure_node_id=source_primary_id[source_id],
                        source_refs=primary_source_refs,
                    )
                )
                continued_pages_by_section.add(page_key)
            continuation_anchor_id = current_clause_id or source_primary_id[source_id]
            last_continuation_anchor_by_section[current_section_id] = (
                page_number,
                continuation_anchor_id,
            )

        if source_node.kind is not SourceNodeKind.TABLE:
            continue
        table = source_node.table_payload
        if table is None:  # SourceIR validates this; keep the boundary fail closed.
            raise StructureResolutionRejected(
                StructureResolutionRejectionCode.INVALID_TABLE_GROUNDING,
                "A SourceIR table node has no TableIR payload.",
            )
        table_uses_visual_text_authority = (
            source_node.native_locator.page_number in untrusted_native_pages
        )

        row_ids: dict[int, str] = {}
        for row_index in range(table.row_count):
            row_identity_key = f"table:{source_node.source_node_id}:row:{row_index}"
            row_id = stable_structure_node_id(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureNodeKind.TABLE_ROW,
                identity_key=row_identity_key,
            )
            row_ids[row_index] = row_id
            covering_cells = _cells_covering_row(table.cells, row_index)
            folded_cell_nodes = folded_table_cell_nodes.get(
                (source_node.source_node_id, row_index),
                (),
            )
            folded_cell_nodes = tuple(
                node
                for node in folded_cell_nodes
                if node.source_node_id not in authority_superseded_source_ids
            )
            pdf_text_selections = folded_pdf_table_row_selections.get(
                (source_node.source_node_id, row_index),
                (),
            )
            pdf_text_selections = tuple(
                selection
                for selection in pdf_text_selections
                if selection.source_node.source_node_id
                not in authority_superseded_source_ids
            )
            folded_pdf_nodes = tuple(
                selection.source_node for selection in pdf_text_selections
            )
            folded_nodes = tuple(
                sorted(
                    (*folded_cell_nodes, *folded_pdf_nodes),
                    key=lambda item: (item.source_order, item.source_node_id),
                )
            )
            row_refs = tuple(
                _cell_ref(cell.cell_id) for cell in covering_cells
            ) + tuple(_node_ref(node.source_node_id) for node in folded_nodes)
            semantic_row_refs = (
                ()
                if table_uses_visual_text_authority
                else _table_row_semantic_source_refs(
                    covering_cells=covering_cells,
                    folded_cell_nodes=folded_cell_nodes,
                    pdf_text_selections=pdf_text_selections,
                )
            )
            if not row_refs:
                # A sparse/empty native row is still locatable by its table fact
                # plus table_row_index; no synthetic cell or text is invented.
                row_refs = (_node_ref(source_node.source_node_id),)
                semantic_row_refs = row_refs
            structure_nodes.append(
                StructureNode(
                    structure_node_id=row_id,
                    identity_key=row_identity_key,
                    kind=StructureNodeKind.TABLE_ROW,
                    parent_structure_node_id=source_primary_id[
                        source_node.source_node_id
                    ],
                    source_order=next_order,
                    source_refs=row_refs,
                    semantic_source_refs=(
                        semantic_row_refs if semantic_row_refs != row_refs else None
                    ),
                    boundary_basis=StructureBoundaryBasis.TABLE_ROW,
                    table_row_index=row_index,
                )
            )
            next_order += 1

            explicit_peer_group = (
                None
                if table_uses_visual_text_authority
                else _explicit_table_cell_peer_group(
                    covering_cells,
                    row_index=row_index,
                )
            )
            if explicit_peer_group is not None:
                peer_cell, peer_items = explicit_peer_group
                peer_ref = _cell_ref(peer_cell.cell_id)
                for peer_item in peer_items:
                    peer_identity_key = (
                        f"table:{source_node.source_node_id}:row:{row_index}:"
                        f"cell:{peer_cell.cell_id}:peer:{peer_item.ordinal}"
                    )
                    peer_id = stable_structure_node_id(
                        source_ir_id=snapshot.source_ir_id,
                        resolver_id=resolver_id,
                        resolver_version=resolver_version,
                        kind=StructureNodeKind.CLAUSE,
                        identity_key=peer_identity_key,
                    )
                    structure_nodes.append(
                        StructureNode(
                            structure_node_id=peer_id,
                            identity_key=peer_identity_key,
                            kind=StructureNodeKind.CLAUSE,
                            parent_structure_node_id=row_id,
                            source_order=next_order,
                            source_refs=(peer_ref,),
                            boundary_basis=StructureBoundaryBasis.EXPLICIT_NUMBER,
                            list_ordinal=peer_item.ordinal,
                        )
                    )
                    next_order += 1

        cells_by_anchor = {(cell.row, cell.column): cell for cell in table.cells}
        for merged_region in sorted(
            table.merged_regions,
            key=lambda item: (
                item.row,
                item.column,
                item.row_span,
                item.column_span,
            ),
        ):
            anchor = cells_by_anchor.get((merged_region.row, merged_region.column))
            if anchor is None:
                raise StructureResolutionRejected(
                    StructureResolutionRejectionCode.INVALID_TABLE_GROUNDING,
                    "A merged region has no SourceIR anchor cell.",
                )
            shared_identity_key = (
                f"table:{source_node.source_node_id}:merged:"
                f"{merged_region.row}:{merged_region.column}:"
                f"{merged_region.row_span}:{merged_region.column_span}"
            )
            shared_id = stable_structure_node_id(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureNodeKind.CONTENT,
                identity_key=shared_identity_key,
            )
            anchor_ref = _cell_ref(anchor.cell_id)
            structure_nodes.append(
                StructureNode(
                    structure_node_id=shared_id,
                    identity_key=shared_identity_key,
                    kind=StructureNodeKind.CONTENT,
                    parent_structure_node_id=source_primary_id[
                        source_node.source_node_id
                    ],
                    source_order=next_order,
                    source_refs=(anchor_ref,),
                    semantic_source_refs=(
                        () if table_uses_visual_text_authority else None
                    ),
                    boundary_basis=StructureBoundaryBasis.MERGED_RANGE,
                )
            )
            next_order += 1
            for covered_row in range(
                merged_region.row,
                merged_region.row + merged_region.row_span,
            ):
                applies_to_specs.append(
                    _AppliesToSpec(
                        source_structure_node_id=shared_id,
                        target_structure_node_id=row_ids[covered_row],
                        source_refs=(anchor_ref,),
                    )
                )

        if not table_uses_visual_text_authority:
            applies_to_specs.extend(
                _table_note_applies_to_specs(
                    source_node=source_node,
                    row_ids=row_ids,
                )
            )

    next_order = _append_visual_structure(
        snapshot=snapshot,
        observations=visual_text_observations,
        root_id=root_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        structure_nodes=structure_nodes,
        continuation_specs=continuation_specs,
        findings=findings,
        next_order=next_order,
    )
    structure_nodes, region_reparented_table_ids = _reconcile_document_region_tables(
        snapshot=snapshot,
        structure_nodes=structure_nodes,
        document_region_heading_source_ids=document_region_heading_source_ids,
    )
    if region_reparented_table_ids:
        continuation_specs = [
            spec
            for spec in continuation_specs
            if spec.target_structure_node_id not in region_reparented_table_ids
        ]
    structure_nodes = _reconcile_visual_page_tables(
        snapshot=snapshot,
        structure_nodes=structure_nodes,
    )
    structure_nodes = _stable_parent_first_reading_order(
        snapshot=snapshot,
        structure_nodes=structure_nodes,
    )
    findings.extend(
        _pdf_table_text_channel_findings(
            snapshot=snapshot,
            row_selections=folded_pdf_table_row_selections,
            structure_nodes=tuple(structure_nodes),
            resolver_id=resolver_id,
            resolver_version=resolver_version,
        )
    )
    visual_table_cell_text_bindings = _visual_table_cell_text_bindings(
        specs=visual_table_cell_binding_specs,
        structure_nodes=tuple(structure_nodes),
    )
    findings.extend(
        _visual_table_fact_findings(
            snapshot=snapshot,
            issues=visual_table_fact_issues,
            structure_nodes=tuple(structure_nodes),
            resolver_id=resolver_id,
            resolver_version=resolver_version,
        )
    )
    cell_placement_specs, cell_placement_findings = _pdf_image_cell_placement_specs(
        snapshot=snapshot,
        structure_nodes=tuple(structure_nodes),
        resolver_id=resolver_id,
        resolver_version=resolver_version,
    )
    findings.extend(cell_placement_findings)
    reference_specs = _figure_reference_specs(
        snapshot=snapshot,
        structure_nodes=tuple(structure_nodes),
    )
    findings.extend(
        _external_reference_content_findings(
            snapshot=snapshot,
            structure_nodes=tuple(structure_nodes),
            resolver_id=resolver_id,
            resolver_version=resolver_version,
        )
    )
    findings.extend(
        _unstructured_visual_requirement_findings(
            snapshot=snapshot,
            structure_nodes=tuple(structure_nodes),
            reference_specs=reference_specs,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
        )
    )
    option_state_specs, option_state_findings = _option_state_specs(
        snapshot=snapshot,
        structure_nodes=tuple(structure_nodes),
        resolver_id=resolver_id,
        resolver_version=resolver_version,
    )
    findings.extend(option_state_findings)
    findings.extend(
        _sequence_findings(
            snapshot=snapshot,
            structure_nodes=tuple(structure_nodes),
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            existing_findings=tuple(findings),
        )
    )
    findings.extend(
        _duplicate_table_caption_findings(
            snapshot=snapshot,
            structure_nodes=tuple(structure_nodes),
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            existing_findings=tuple(findings),
        )
    )
    continuation_specs.extend(
        _native_wrapped_content_specs(
            snapshot=snapshot,
            structure_nodes=tuple(structure_nodes),
        )
    )
    continuation_specs.extend(
        _continued_table_specs(
            snapshot=snapshot,
            structure_nodes=tuple(structure_nodes),
        )
    )
    continuation_specs = list(
        {
            (
                spec.source_structure_node_id,
                spec.target_structure_node_id,
            ): spec
            for spec in continuation_specs
        }.values()
    )
    (
        document_language_policy,
        bilingual_counterpart_groups,
        bilingual_mismatch_signals,
        translation_relations,
        bilingual_findings,
    ) = _resolve_bilingual_counterparts(
        snapshot=snapshot,
        structure_nodes=tuple(structure_nodes),
        root_structure_node_id=root_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
    )
    findings.extend(bilingual_findings)

    relations: list[StructureRelation] = []
    for structure_node in structure_nodes:
        if structure_node.parent_structure_node_id is None:
            continue
        relations.append(
            _relation(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureRelationKind.CONTAINS,
                source_structure_node_id=structure_node.parent_structure_node_id,
                target_structure_node_id=structure_node.structure_node_id,
                source_refs=structure_node.source_refs,
            )
        )
    for spec in applies_to_specs:
        relations.append(
            _relation(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureRelationKind.APPLIES_TO,
                source_structure_node_id=spec.source_structure_node_id,
                target_structure_node_id=spec.target_structure_node_id,
                source_refs=spec.source_refs,
            )
        )
    for spec in cell_placement_specs:
        relations.append(
            _relation(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=spec.kind,
                source_structure_node_id=spec.source_structure_node_id,
                target_structure_node_id=spec.target_structure_node_id,
                source_refs=spec.source_refs,
            )
        )
    for spec in continuation_specs:
        relations.append(
            _relation(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureRelationKind.CONTINUES,
                source_structure_node_id=spec.source_structure_node_id,
                target_structure_node_id=spec.target_structure_node_id,
                source_refs=spec.source_refs,
            )
        )
    for spec in reference_specs:
        relations.append(
            _relation(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureRelationKind.REFERENCES,
                source_structure_node_id=spec.source_structure_node_id,
                target_structure_node_id=spec.target_structure_node_id,
                source_refs=spec.source_refs,
            )
        )
    for spec in option_state_specs:
        relations.append(
            _relation(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureRelationKind.OPTION_STATE,
                source_structure_node_id=spec.source_structure_node_id,
                target_structure_node_id=spec.target_structure_node_id,
                source_refs=spec.source_refs,
                option_state=spec.option_state,
            )
        )
    relations.extend(translation_relations)

    source_cell_ids = tuple(
        sorted(
            cell.cell_id
            for source_node in snapshot.nodes
            if source_node.table_payload is not None
            for cell in source_node.table_payload.cells
        )
    )
    tree = StructureTreeSnapshot(
        structure_snapshot_id=structure_snapshot_id,
        source_ir_id=snapshot.source_ir_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        source_node_ids=tuple(sorted(node.source_node_id for node in snapshot.nodes)),
        source_cell_ids=source_cell_ids,
        visual_text_observation_ids=tuple(
            sorted(
                observation.observation_id
                for observation in all_visual_text_observations
            )
        ),
        visual_table_cell_ids=tuple(
            sorted(
                cell.visual_table_cell_id
                for visual_table_snapshot in snapshot.visual_table_snapshots
                for cell in visual_table_snapshot.cells
            )
        ),
        nodes=tuple(structure_nodes),
        relations=tuple(relations),
        document_language_policy=document_language_policy,
        bilingual_counterpart_groups=bilingual_counterpart_groups,
        bilingual_mismatch_signals=bilingual_mismatch_signals,
        visual_table_cell_text_bindings=visual_table_cell_text_bindings,
        findings=tuple(findings),
    )
    return tree


def _resolve_bilingual_counterparts(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    root_structure_node_id: str,
    resolver_id: str,
    resolver_version: str,
) -> tuple[
    DocumentLanguagePolicy | None,
    tuple[BilingualCounterpartGroup, ...],
    tuple[BilingualMismatchSignal, ...],
    tuple[StructureRelation, ...],
    tuple[StructureFinding, ...],
]:
    """Resolve only native, conservative bilingual evidence.

    This layer does not decide whether a group is a requirement.  It records
    counterpart identity and exact source intervals so semantic projection can
    merge only items that are independently classified as requirements.
    """

    native_formats = {node.native_locator.format for node in snapshot.nodes}
    if NativeDocumentFormat.PDF in native_formats:
        return _resolve_pdf_bilingual_counterparts(
            snapshot=snapshot,
            structure_nodes=structure_nodes,
            root_structure_node_id=root_structure_node_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
        )
    if NativeDocumentFormat.DOCX not in native_formats:
        return None, (), (), (), ()

    eligible_body_nodes = _eligible_bilingual_body_nodes(snapshot)
    has_bilingual_evidence = (
        any(node.docx_inline_spans for node in snapshot.nodes)
        or _has_adjacent_bilingual_body_evidence(eligible_body_nodes)
        or _has_reviewed_table_bilingual_evidence(snapshot, structure_nodes)
        or any(
            _has_ambiguous_english_then_chinese_text(node.raw_text)
            for node in snapshot.nodes
        )
    )
    policy, policy_findings, policy_source_ids = _resolve_document_language_policy(
        snapshot=snapshot,
        root_structure_node_id=root_structure_node_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        has_bilingual_evidence=has_bilingual_evidence,
    )
    if policy is None:
        return None, (), (), (), policy_findings

    nodes_by_id = {node.structure_node_id: node for node in structure_nodes}
    source_nodes_by_id = {node.source_node_id: node for node in snapshot.nodes}
    owner_ids_by_source_id: dict[str, list[str]] = {}
    for structure_node in structure_nodes:
        if structure_node.kind is StructureNodeKind.DOCUMENT:
            continue
        for source_ref in structure_node.source_refs:
            if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
                owner_ids_by_source_id.setdefault(source_ref.source_id, []).append(
                    structure_node.structure_node_id
                )

    groups: list[BilingualCounterpartGroup] = []
    translation_relations: list[StructureRelation] = []
    findings = list(policy_findings)
    assigned_owner_ids: set[str] = set()
    assigned_source_ids: set[str] = set(policy_source_ids)

    # A2: one native paragraph with an explicit DOCX run boundary.
    for source_node in sorted(
        snapshot.nodes,
        key=lambda item: (item.source_order, item.source_node_id),
    ):
        if (
            not source_node.docx_inline_spans
            or source_node.source_node_id in assigned_source_ids
        ):
            continue
        owner_id = _single_bilingual_owner_id(
            source_node.source_node_id,
            owner_ids_by_source_id=owner_ids_by_source_id,
            nodes_by_id=nodes_by_id,
        )
        if owner_id is None or owner_id in assigned_owner_ids:
            continue
        english_spans: list[BilingualSourceSpan] = []
        chinese_spans: list[BilingualSourceSpan] = []
        for source_span in source_node.docx_inline_spans:
            if (
                source_span.boundary_basis
                is not DocxInlineBoundaryBasis.DOCX_RUN_BOUNDARY
            ):
                continue
            language_evidence = (
                BilingualLanguageEvidence.EN
                if source_span.language_evidence is DocxInlineLanguageEvidence.EN
                else BilingualLanguageEvidence.ZH_CN
            )
            span = _bilingual_source_span(
                source_node=source_node,
                start_offset=source_span.start_offset,
                end_offset=source_span.end_offset,
                language_evidence=language_evidence,
                boundary_basis=BilingualSpanBoundaryBasis.DOCX_RUN_BOUNDARY,
            )
            if language_evidence is BilingualLanguageEvidence.EN:
                english_spans.append(span)
            else:
                chinese_spans.append(span)
        if not english_spans or not chinese_spans:
            continue
        group, relation = _build_bilingual_counterpart_group(
            source_ir_id=snapshot.source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            document_language_policy=policy,
            canonical_structure_node_id=owner_id,
            member_structure_node_ids=(owner_id,),
            english_spans=tuple(english_spans),
            chinese_spans=tuple(chinese_spans),
        )
        groups.append(group)
        if relation is not None:
            translation_relations.append(relation)
        assigned_owner_ids.add(owner_id)
        assigned_source_ids.add(source_node.source_node_id)

    # A3: the reviewed one-owner table shape, E/Z/E/Z (or more alternating
    # pairs), within one physical DOCX cell.  Two-paragraph table cells remain
    # outside this automatic scope until separately reviewed.
    for row_node in structure_nodes:
        if (
            row_node.kind is not StructureNodeKind.TABLE_ROW
            or row_node.structure_node_id in assigned_owner_ids
        ):
            continue
        source_paragraphs = tuple(
            source_nodes_by_id[source_ref.source_id]
            for source_ref in row_node.source_refs
            if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
            and source_ref.source_id in source_nodes_by_id
            and source_nodes_by_id[source_ref.source_id].native_locator.format
            is NativeDocumentFormat.DOCX
            and source_nodes_by_id[source_ref.source_id].native_locator.table_index
            is not None
            and source_nodes_by_id[source_ref.source_id].native_locator.row_index
            == row_node.table_row_index
        )
        paragraphs_by_cell: dict[tuple[int, int, int], list[SourceNode]] = {}
        for source_node in source_paragraphs:
            locator = source_node.native_locator
            assert locator.table_index is not None
            assert locator.row_index is not None
            assert locator.column_index is not None
            paragraphs_by_cell.setdefault(
                (locator.table_index, locator.row_index, locator.column_index),
                [],
            ).append(source_node)
        for cell_paragraphs in paragraphs_by_cell.values():
            ordered = tuple(
                sorted(
                    cell_paragraphs,
                    key=lambda item: (item.source_order, item.source_node_id),
                )
            )
            sides = tuple(_source_language_side(item.raw_text) for item in ordered)
            if (
                len(ordered) < 4
                or len(ordered) % 2
                or any(item.docx_inline_spans for item in ordered)
                or any(
                    side
                    is not (
                        BilingualLanguageEvidence.EN
                        if index % 2 == 0
                        else BilingualLanguageEvidence.ZH_CN
                    )
                    for index, side in enumerate(sides)
                )
                or any(item.source_node_id in assigned_source_ids for item in ordered)
            ):
                continue
            english_spans = tuple(
                _full_bilingual_source_span(
                    source_node,
                    language_evidence=BilingualLanguageEvidence.EN,
                )
                for source_node in ordered[0::2]
            )
            chinese_spans = tuple(
                _full_bilingual_source_span(
                    source_node,
                    language_evidence=BilingualLanguageEvidence.ZH_CN,
                )
                for source_node in ordered[1::2]
            )
            group, relation = _build_bilingual_counterpart_group(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                document_language_policy=policy,
                canonical_structure_node_id=row_node.structure_node_id,
                member_structure_node_ids=(row_node.structure_node_id,),
                english_spans=english_spans,
                chinese_spans=chinese_spans,
            )
            groups.append(group)
            if relation is not None:
                translation_relations.append(relation)
            assigned_owner_ids.add(row_node.structure_node_id)
            assigned_source_ids.update(item.source_node_id for item in ordered)
            break

    # A1: adjacent top-level EN then zh-CN blocks under one structural parent.
    for english_node, chinese_node in zip(
        eligible_body_nodes,
        eligible_body_nodes[1:],
    ):
        if (
            english_node.source_order + 1 != chinese_node.source_order
            or english_node.source_node_id in assigned_source_ids
            or chinese_node.source_node_id in assigned_source_ids
            or _source_language_side(english_node.raw_text)
            is not BilingualLanguageEvidence.EN
            or _source_language_side(chinese_node.raw_text)
            is not BilingualLanguageEvidence.ZH_CN
        ):
            continue
        english_owner_id = _single_bilingual_owner_id(
            english_node.source_node_id,
            owner_ids_by_source_id=owner_ids_by_source_id,
            nodes_by_id=nodes_by_id,
        )
        chinese_owner_id = _single_bilingual_owner_id(
            chinese_node.source_node_id,
            owner_ids_by_source_id=owner_ids_by_source_id,
            nodes_by_id=nodes_by_id,
        )
        if (
            english_owner_id is None
            or chinese_owner_id is None
            or english_owner_id == chinese_owner_id
            or english_owner_id in assigned_owner_ids
            or chinese_owner_id in assigned_owner_ids
            or nodes_by_id[english_owner_id].parent_structure_node_id
            != nodes_by_id[chinese_owner_id].parent_structure_node_id
        ):
            continue
        english_span = _full_bilingual_source_span(
            english_node,
            language_evidence=BilingualLanguageEvidence.EN,
        )
        chinese_span = _full_bilingual_source_span(
            chinese_node,
            language_evidence=BilingualLanguageEvidence.ZH_CN,
        )
        group, relation = _build_bilingual_counterpart_group(
            source_ir_id=snapshot.source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            document_language_policy=policy,
            canonical_structure_node_id=english_owner_id,
            member_structure_node_ids=(english_owner_id, chinese_owner_id),
            english_spans=(english_span,),
            chinese_spans=(chinese_span,),
        )
        groups.append(group)
        assert relation is not None
        translation_relations.append(relation)
        assigned_owner_ids.update((english_owner_id, chinese_owner_id))
        assigned_source_ids.update(
            (english_node.source_node_id, chinese_node.source_node_id)
        )

    # Ambiguous same-run mixed text remains a no-op plus a nonblocking finding.
    for source_node in snapshot.nodes:
        if (
            source_node.source_node_id in assigned_source_ids
            or source_node.docx_inline_spans
            or not _has_ambiguous_english_then_chinese_text(source_node.raw_text)
        ):
            continue
        owner_id = _single_bilingual_owner_id(
            source_node.source_node_id,
            owner_ids_by_source_id=owner_ids_by_source_id,
            nodes_by_id=nodes_by_id,
        )
        if owner_id is None:
            continue
        findings.append(
            _bilingual_structure_finding(
                snapshot=snapshot,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                code="bilingual_inline_boundary_ambiguous",
                severity=StructureFindingSeverity.WARNING,
                blocking=False,
                structure_node_id=owner_id,
                source_refs=(_node_ref(source_node.source_node_id),),
            )
        )

    return (
        policy,
        tuple(sorted(groups, key=lambda item: item.counterpart_group_id)),
        (),
        tuple(translation_relations),
        tuple(findings),
    )


@dataclass(frozen=True)
class _PdfBilingualPairEvidence:
    """One numbered PDF owner with a source-contiguous opposite-language zone."""

    owner: StructureNode
    owner_source: SourceNode
    counterpart_sources: tuple[SourceNode, ...]
    owner_language: BilingualLanguageEvidence


def _resolve_pdf_bilingual_counterparts(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    root_structure_node_id: str,
    resolver_id: str,
    resolver_version: str,
) -> tuple[
    DocumentLanguagePolicy | None,
    tuple[BilingualCounterpartGroup, ...],
    tuple[BilingualMismatchSignal, ...],
    tuple[StructureRelation, ...],
    tuple[StructureFinding, ...],
]:
    """Resolve repeated, layout-proven PDF bilingual pairs conservatively.

    A language guess or a single adjacent paragraph is never enough.  The
    evidence must repeat across at least three consecutive numbered siblings,
    with every opposite-language block already owned as continuous direct
    CONTENT by the same explicit clause and with a stable two-column/indent
    layout signature across the run.
    """

    paired_runs = _pdf_bilingual_paired_runs(
        snapshot=snapshot,
        structure_nodes=structure_nodes,
    )
    if not paired_runs:
        return None, (), (), (), ()

    policy, policy_findings, _ = _resolve_document_language_policy(
        snapshot=snapshot,
        root_structure_node_id=root_structure_node_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        has_bilingual_evidence=True,
        eligible_formats=frozenset({NativeDocumentFormat.PDF}),
        allow_unknown_authority=True,
    )
    if policy is None:
        return None, (), (), (), policy_findings

    groups: list[BilingualCounterpartGroup] = []
    mismatch_signals: list[BilingualMismatchSignal] = []
    for run in paired_runs:
        for evidence in run:
            owner_span = _full_bilingual_source_span(
                evidence.owner_source,
                language_evidence=evidence.owner_language,
            )
            counterpart_language = (
                BilingualLanguageEvidence.EN
                if evidence.owner_language is BilingualLanguageEvidence.ZH_CN
                else BilingualLanguageEvidence.ZH_CN
            )
            counterpart_spans = tuple(
                _full_bilingual_source_span(
                    source_node,
                    language_evidence=counterpart_language,
                )
                for source_node in evidence.counterpart_sources
            )
            english_spans = (
                counterpart_spans
                if counterpart_language is BilingualLanguageEvidence.EN
                else (owner_span,)
            )
            chinese_spans = (
                (owner_span,)
                if evidence.owner_language is BilingualLanguageEvidence.ZH_CN
                else counterpart_spans
            )
            group, relation = _build_bilingual_counterpart_group(
                source_ir_id=snapshot.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                document_language_policy=policy,
                canonical_structure_node_id=evidence.owner.structure_node_id,
                member_structure_node_ids=(evidence.owner.structure_node_id,),
                english_spans=english_spans,
                chinese_spans=chinese_spans,
            )
            assert relation is None
            groups.append(group)
            if _pdf_bilingual_pair_has_numeric_mismatch(evidence):
                english_span_ids = tuple(
                    span.bilingual_span_id for span in english_spans
                )
                chinese_span_ids = tuple(
                    span.bilingual_span_id for span in chinese_spans
                )
                mismatch_signals.append(
                    BilingualMismatchSignal(
                        mismatch_signal_id=stable_bilingual_mismatch_signal_id(
                            counterpart_group_id=group.counterpart_group_id,
                            english_span_ids=english_span_ids,
                            chinese_span_ids=chinese_span_ids,
                        ),
                        counterpart_group_id=group.counterpart_group_id,
                        english_span_ids=english_span_ids,
                        chinese_span_ids=chinese_span_ids,
                    )
                )

    return (
        policy,
        tuple(sorted(groups, key=lambda item: item.counterpart_group_id)),
        tuple(
            sorted(
                mismatch_signals,
                key=lambda item: item.mismatch_signal_id,
            )
        ),
        (),
        policy_findings,
    )


def _pdf_bilingual_paired_runs(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
) -> tuple[tuple[_PdfBilingualPairEvidence, ...], ...]:
    source_nodes_by_id = {node.source_node_id: node for node in snapshot.nodes}
    children_by_parent: dict[str, list[StructureNode]] = {}
    for node in structure_nodes:
        if node.parent_structure_node_id is not None:
            children_by_parent.setdefault(
                node.parent_structure_node_id,
                [],
            ).append(node)
    for children in children_by_parent.values():
        children.sort(key=lambda item: (item.source_order, item.structure_node_id))

    runs: list[tuple[_PdfBilingualPairEvidence, ...]] = []
    for siblings in children_by_parent.values():
        current: list[_PdfBilingualPairEvidence] = []

        def flush() -> None:
            if len(current) >= _PDF_BILINGUAL_PAIRED_RUN_MIN_SIZE:
                runs.append(tuple(current))
            current.clear()

        for node in siblings:
            evidence = _pdf_bilingual_pair_evidence(
                node=node,
                direct_children=tuple(
                    children_by_parent.get(node.structure_node_id, ())
                ),
                source_nodes_by_id=source_nodes_by_id,
            )
            if evidence is None:
                flush()
                continue
            if current and not _pdf_bilingual_pair_follows_run(
                current,
                evidence,
            ):
                flush()
            current.append(evidence)
        flush()
    return tuple(runs)


def _pdf_bilingual_pair_evidence(
    *,
    node: StructureNode,
    direct_children: tuple[StructureNode, ...],
    source_nodes_by_id: dict[str, SourceNode],
) -> _PdfBilingualPairEvidence | None:
    if (
        node.kind is not StructureNodeKind.CLAUSE
        or node.boundary_basis is not StructureBoundaryBasis.EXPLICIT_NUMBER
        or node.list_ordinal is None
        or not direct_children
        or any(child.kind is not StructureNodeKind.CONTENT for child in direct_children)
        or tuple(child.source_order for child in direct_children)
        != tuple(
            range(
                node.source_order + 1,
                node.source_order + 1 + len(direct_children),
            )
        )
    ):
        return None
    owner_sources = _pdf_text_sources_for_structure_node(
        node,
        source_nodes_by_id=source_nodes_by_id,
    )
    if len(owner_sources) != 1:
        return None
    counterpart_sources_by_child = tuple(
        _pdf_text_sources_for_structure_node(
            child,
            source_nodes_by_id=source_nodes_by_id,
        )
        for child in direct_children
    )
    if any(len(sources) != 1 for sources in counterpart_sources_by_child):
        return None
    owner_source = owner_sources[0]
    counterpart_sources = tuple(
        sources[0] for sources in counterpart_sources_by_child
    )
    all_source_ids = tuple(
        source.source_node_id for source in (owner_source, *counterpart_sources)
    )
    if (
        len(all_source_ids) != len(set(all_source_ids))
        or tuple(source.source_order for source in counterpart_sources)
        != tuple(sorted(source.source_order for source in counterpart_sources))
        or any(
            source.source_order <= owner_source.source_order
            for source in counterpart_sources
        )
    ):
        return None
    owner_language = _source_language_side(owner_source.raw_text)
    if owner_language is None:
        return None
    counterpart_language = (
        BilingualLanguageEvidence.EN
        if owner_language is BilingualLanguageEvidence.ZH_CN
        else BilingualLanguageEvidence.ZH_CN
    )
    if any(
        _source_language_side(source.raw_text) is not counterpart_language
        for source in counterpart_sources
    ):
        return None
    evidence = _PdfBilingualPairEvidence(
        owner=node,
        owner_source=owner_source,
        counterpart_sources=counterpart_sources,
        owner_language=owner_language,
    )
    return evidence if _pdf_bilingual_pair_layout_is_proven(evidence) else None


def _pdf_text_sources_for_structure_node(
    node: StructureNode,
    *,
    source_nodes_by_id: dict[str, SourceNode],
) -> tuple[SourceNode, ...]:
    source_refs = tuple(
        source_ref
        for source_ref in node.source_refs
        if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
    )
    if len(source_refs) != len(node.source_refs):
        return ()
    sources = tuple(
        source_nodes_by_id[source_ref.source_id]
        for source_ref in source_refs
        if source_ref.source_id in source_nodes_by_id
    )
    if len(sources) != len(source_refs) or any(
        source.native_locator.format is not NativeDocumentFormat.PDF
        or source.native_locator.bbox is None
        or source.kind not in {SourceNodeKind.PARAGRAPH, SourceNodeKind.LIST_ITEM}
        or not source.raw_text.strip()
        for source in sources
    ):
        return ()
    return sources


def _pdf_bilingual_pair_layout_is_proven(
    evidence: _PdfBilingualPairEvidence,
) -> bool:
    ordered = (evidence.owner_source, *evidence.counterpart_sources)
    for previous, current in zip(ordered, ordered[1:]):
        previous_locator = previous.native_locator
        current_locator = current.native_locator
        assert previous_locator.page_number is not None
        assert current_locator.page_number is not None
        assert previous_locator.bbox is not None
        assert current_locator.bbox is not None
        page_delta = current_locator.page_number - previous_locator.page_number
        if page_delta not in {0, 1}:
            return False
        if abs(current_locator.bbox[0] - previous_locator.bbox[0]) > (
            _PDF_BILINGUAL_PAIR_MAX_X_SHIFT_POINTS
        ):
            return False
        if page_delta == 0:
            vertical_gap = current_locator.bbox[1] - previous_locator.bbox[3]
            if not (
                -1.0
                <= vertical_gap
                <= _PDF_BILINGUAL_PAIR_MAX_VERTICAL_GAP_POINTS
            ):
                return False
    return True


def _pdf_bilingual_pair_follows_run(
    current: list[_PdfBilingualPairEvidence],
    candidate: _PdfBilingualPairEvidence,
) -> bool:
    previous = current[-1]
    assert previous.owner.list_ordinal is not None
    assert candidate.owner.list_ordinal is not None
    if (
        candidate.owner.list_ordinal != previous.owner.list_ordinal + 1
        or candidate.owner_language is not previous.owner_language
    ):
        return False
    reference = current[0]
    reference_owner_bbox = reference.owner_source.native_locator.bbox
    candidate_owner_bbox = candidate.owner_source.native_locator.bbox
    reference_counterpart_bbox = (
        reference.counterpart_sources[0].native_locator.bbox
    )
    candidate_counterpart_bbox = (
        candidate.counterpart_sources[0].native_locator.bbox
    )
    assert reference_owner_bbox is not None
    assert candidate_owner_bbox is not None
    assert reference_counterpart_bbox is not None
    assert candidate_counterpart_bbox is not None
    return (
        abs(candidate_owner_bbox[0] - reference_owner_bbox[0])
        <= _PDF_BILINGUAL_OWNER_X_TOLERANCE_POINTS
        and abs(candidate_counterpart_bbox[0] - reference_counterpart_bbox[0])
        <= _PDF_BILINGUAL_COUNTERPART_X_TOLERANCE_POINTS
    )


def _pdf_bilingual_pair_has_numeric_mismatch(
    evidence: _PdfBilingualPairEvidence,
) -> bool:
    owner_tokens = _bilingual_numeric_tokens(evidence.owner_source.raw_text)
    counterpart_tokens = frozenset(
        token
        for source in evidence.counterpart_sources
        for token in _bilingual_numeric_tokens(source.raw_text)
    )
    return bool(
        owner_tokens
        and counterpart_tokens
        and owner_tokens != counterpart_tokens
    )


def _bilingual_numeric_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _BILINGUAL_LEADING_ORDINAL.sub("", normalized, count=1)
    tokens: set[str] = set()
    for match in _BILINGUAL_ARABIC_NUMBER.finditer(normalized):
        try:
            number = Decimal(match.group(0).replace(",", ""))
        except InvalidOperation:
            continue
        tokens.add(format(number.normalize(), "f"))
    return frozenset(tokens)


def _resolve_document_language_policy(
    *,
    snapshot: SourceIRSnapshot,
    root_structure_node_id: str,
    resolver_id: str,
    resolver_version: str,
    has_bilingual_evidence: bool,
    eligible_formats: frozenset[NativeDocumentFormat] = frozenset(
        {NativeDocumentFormat.DOCX}
    ),
    allow_unknown_authority: bool = False,
) -> tuple[
    DocumentLanguagePolicy | None,
    tuple[StructureFinding, ...],
    frozenset[str],
]:
    eligible_nodes = _eligible_bilingual_body_nodes(
        snapshot,
        eligible_formats=eligible_formats,
    )
    english_declarations = tuple(
        node
        for node in eligible_nodes
        if _is_valid_english_language_policy(node.raw_text)
    )
    chinese_declarations = tuple(
        node
        for node in eligible_nodes
        if _is_valid_chinese_language_policy(node.raw_text)
    )
    conflicting_declarations = tuple(
        node
        for node in eligible_nodes
        if _declares_non_chinese_conflict_precedence(node.raw_text)
    )
    policy_like_nodes = tuple(
        node
        for node in eligible_nodes
        if _looks_like_language_policy_declaration(node.raw_text)
    )
    policy_source_ids = frozenset(
        node.source_node_id
        for node in (
            *english_declarations,
            *chinese_declarations,
            *conflicting_declarations,
            *policy_like_nodes,
        )
    )

    if (
        len(english_declarations) == 1
        and len(chinese_declarations) == 1
        and not conflicting_declarations
    ):
        english_span = _full_bilingual_source_span(
            english_declarations[0],
            language_evidence=BilingualLanguageEvidence.EN,
        )
        chinese_span = _full_bilingual_source_span(
            chinese_declarations[0],
            language_evidence=BilingualLanguageEvidence.ZH_CN,
        )
        policy_id = stable_document_language_policy_id(
            english_declaration_spans=(english_span,),
            chinese_declaration_spans=(chinese_span,),
            equal_validity=True,
            conflict_precedence="zh-CN",
        )
        return (
            DocumentLanguagePolicy(
                document_language_policy_id=policy_id,
                equal_validity=True,
                conflict_precedence="zh-CN",
                english_declaration_spans=(english_span,),
                chinese_declaration_spans=(chinese_span,),
            ),
            (),
            policy_source_ids,
        )

    if allow_unknown_authority and not policy_like_nodes:
        policy_id = stable_document_language_policy_id(
            english_declaration_spans=(),
            chinese_declaration_spans=(),
            equal_validity=None,
            conflict_precedence="UNKNOWN",
            authority_basis="UNKNOWN",
            policy_scope_source_ir_id=snapshot.source_ir_id,
        )
        return (
            DocumentLanguagePolicy(
                document_language_policy_id=policy_id,
                authority_basis="UNKNOWN",
                policy_scope_source_ir_id=snapshot.source_ir_id,
                equal_validity=None,
                conflict_precedence="UNKNOWN",
            ),
            (),
            policy_source_ids,
        )

    if not has_bilingual_evidence and not policy_like_nodes:
        return None, (), policy_source_ids
    if conflicting_declarations:
        code = "document_language_policy_conflict"
    elif len(english_declarations) > 1 or len(chinese_declarations) > 1:
        code = "document_language_policy_multiple"
    else:
        code = "document_language_policy_missing"
    source_refs = tuple(_node_ref(source_id) for source_id in sorted(policy_source_ids))
    finding = _bilingual_structure_finding(
        snapshot=snapshot,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        code=code,
        severity=StructureFindingSeverity.ERROR,
        blocking=True,
        structure_node_id=root_structure_node_id,
        source_refs=source_refs,
    )
    return None, (finding,), policy_source_ids


def _build_bilingual_counterpart_group(
    *,
    source_ir_id: str,
    resolver_id: str,
    resolver_version: str,
    document_language_policy: DocumentLanguagePolicy,
    canonical_structure_node_id: str,
    member_structure_node_ids: tuple[str, ...],
    english_spans: tuple[BilingualSourceSpan, ...],
    chinese_spans: tuple[BilingualSourceSpan, ...],
) -> tuple[BilingualCounterpartGroup, StructureRelation | None]:
    relation: StructureRelation | None = None
    source_refs = tuple(
        dict.fromkeys(span.source_ref for span in (*english_spans, *chinese_spans))
    )
    if len(member_structure_node_ids) == 2:
        other_member = next(
            member_id
            for member_id in member_structure_node_ids
            if member_id != canonical_structure_node_id
        )
        relation = _relation(
            source_ir_id=source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            kind=StructureRelationKind.TRANSLATION_OF,
            source_structure_node_id=canonical_structure_node_id,
            target_structure_node_id=other_member,
            source_refs=source_refs,
        )
    group_id = stable_bilingual_counterpart_group_id(
        document_language_policy_id=(
            document_language_policy.document_language_policy_id
        ),
        canonical_structure_node_id=canonical_structure_node_id,
        member_structure_node_ids=member_structure_node_ids,
        english_spans=english_spans,
        chinese_spans=chinese_spans,
    )
    return (
        BilingualCounterpartGroup(
            counterpart_group_id=group_id,
            document_language_policy_id=(
                document_language_policy.document_language_policy_id
            ),
            canonical_structure_node_id=canonical_structure_node_id,
            member_structure_node_ids=member_structure_node_ids,
            english_spans=english_spans,
            chinese_spans=chinese_spans,
            relation_kind=StructureRelationKind.TRANSLATION_OF,
            translation_relation_id=(
                relation.relation_id if relation is not None else None
            ),
        ),
        relation,
    )


def _bilingual_source_span(
    *,
    source_node: SourceNode,
    start_offset: int,
    end_offset: int,
    language_evidence: BilingualLanguageEvidence,
    boundary_basis: BilingualSpanBoundaryBasis,
) -> BilingualSourceSpan:
    source_ref = _node_ref(source_node.source_node_id)
    span_id = stable_bilingual_source_span_id(
        source_ref=source_ref,
        start_offset=start_offset,
        end_offset=end_offset,
        language_evidence=language_evidence,
        boundary_basis=boundary_basis,
    )
    return BilingualSourceSpan(
        bilingual_span_id=span_id,
        source_ref=source_ref,
        start_offset=start_offset,
        end_offset=end_offset,
        language_evidence=language_evidence,
        boundary_basis=boundary_basis,
    )


def _full_bilingual_source_span(
    source_node: SourceNode,
    *,
    language_evidence: BilingualLanguageEvidence,
) -> BilingualSourceSpan:
    return _bilingual_source_span(
        source_node=source_node,
        start_offset=0,
        end_offset=len(source_node.raw_text),
        language_evidence=language_evidence,
        boundary_basis=BilingualSpanBoundaryBasis.SOURCE_BLOCK_BOUNDARY,
    )


def _eligible_bilingual_body_nodes(
    snapshot: SourceIRSnapshot,
    *,
    eligible_formats: frozenset[NativeDocumentFormat] = frozenset(
        {NativeDocumentFormat.DOCX}
    ),
) -> tuple[SourceNode, ...]:
    return tuple(
        sorted(
            (
                node
                for node in snapshot.nodes
                if node.native_locator.format in eligible_formats
                and node.parent_source_node_id is None
                and node.native_locator.table_index is None
                and node.kind
                in {
                    SourceNodeKind.PARAGRAPH,
                    SourceNodeKind.LIST_ITEM,
                }
            ),
            key=lambda item: (item.source_order, item.source_node_id),
        )
    )


def _has_adjacent_bilingual_body_evidence(
    nodes: tuple[SourceNode, ...],
) -> bool:
    return any(
        english.source_order + 1 == chinese.source_order
        and _source_language_side(english.raw_text) is BilingualLanguageEvidence.EN
        and _source_language_side(chinese.raw_text) is BilingualLanguageEvidence.ZH_CN
        for english, chinese in zip(nodes, nodes[1:])
    )


def _has_reviewed_table_bilingual_evidence(
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
) -> bool:
    source_nodes_by_id = {node.source_node_id: node for node in snapshot.nodes}
    for row_node in structure_nodes:
        if row_node.kind is not StructureNodeKind.TABLE_ROW:
            continue
        paragraphs_by_cell: dict[tuple[int, int, int], list[SourceNode]] = {}
        for source_ref in row_node.source_refs:
            if (
                source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
                or source_ref.source_id not in source_nodes_by_id
            ):
                continue
            source_node = source_nodes_by_id[source_ref.source_id]
            locator = source_node.native_locator
            if (
                locator.format is not NativeDocumentFormat.DOCX
                or locator.table_index is None
                or locator.row_index != row_node.table_row_index
                or locator.column_index is None
            ):
                continue
            paragraphs_by_cell.setdefault(
                (locator.table_index, locator.row_index, locator.column_index),
                [],
            ).append(source_node)
        for paragraphs in paragraphs_by_cell.values():
            ordered = tuple(sorted(paragraphs, key=lambda item: item.source_order))
            sides = tuple(_source_language_side(item.raw_text) for item in ordered)
            if (
                len(sides) >= 4
                and len(sides) % 2 == 0
                and all(
                    side
                    is (
                        BilingualLanguageEvidence.EN
                        if index % 2 == 0
                        else BilingualLanguageEvidence.ZH_CN
                    )
                    for index, side in enumerate(sides)
                )
            ):
                return True
    return False


def _single_bilingual_owner_id(
    source_node_id: str,
    *,
    owner_ids_by_source_id: dict[str, list[str]],
    nodes_by_id: dict[str, StructureNode],
) -> str | None:
    eligible = tuple(
        owner_id
        for owner_id in dict.fromkeys(owner_ids_by_source_id.get(source_node_id, ()))
        if nodes_by_id[owner_id].kind
        in {
            StructureNodeKind.CONTENT,
            StructureNodeKind.CLAUSE,
            StructureNodeKind.TABLE_ROW,
        }
    )
    return eligible[0] if len(eligible) == 1 else None


def _source_language_side(value: str) -> BilingualLanguageEvidence | None:
    normalized = unicodedata.normalize("NFKC", value)
    han_count = _han_character_count(normalized)
    if han_count:
        first_han_index = next(
            index for index, character in enumerate(normalized) if _is_han(character)
        )
        if _strong_english_language_evidence(normalized[:first_han_index]):
            return None
        return BilingualLanguageEvidence.ZH_CN if han_count >= 2 else None
    return (
        BilingualLanguageEvidence.EN
        if _strong_english_language_evidence(normalized)
        else None
    )


def _has_ambiguous_english_then_chinese_text(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    if not _contains_han(normalized):
        return False
    first_han_index = next(
        index for index, character in enumerate(normalized) if _is_han(character)
    )
    return (
        _strong_english_language_evidence(normalized[:first_han_index])
        and _han_character_count(normalized[first_han_index:]) >= 2
    )


def _strong_english_language_evidence(value: str) -> bool:
    if _contains_han(value):
        return False
    latin_letters = sum(
        1 for character in value if character.isascii() and character.isalpha()
    )
    latin_words = tuple(
        token
        for token in "".join(
            character if character.isascii() and character.isalpha() else " "
            for character in value
        ).split()
        if token
    )
    return latin_letters >= 12 and len(latin_words) >= 2


def _contains_han(value: str) -> bool:
    return any(_is_han(character) for character in value)


def _han_character_count(value: str) -> int:
    return sum(1 for character in value if _is_han(character))


def _is_han(character: str) -> bool:
    return (
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
    )


def _normalized_policy_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _is_valid_english_language_policy(value: str) -> bool:
    normalized = _normalized_policy_text(value)
    folded = normalized.casefold()
    return (
        "english" in folded
        and "chinese" in folded
        and _has_english_equal_validity_signal(normalized)
        and _ENGLISH_LANGUAGE_POLICY_CONFLICT.search(normalized) is not None
        and _ENGLISH_CHINESE_PRECEDENCE.search(normalized) is not None
        and _ENGLISH_ENGLISH_PRECEDENCE.search(normalized) is None
    )


def _is_valid_chinese_language_policy(value: str) -> bool:
    normalized = _normalized_policy_text(value)
    return (
        _has_chinese_equal_validity_signal(normalized)
        and _CHINESE_LANGUAGE_POLICY_CONFLICT.search(normalized) is not None
        and _CHINESE_CHINESE_PRECEDENCE.search(normalized) is not None
        and _CHINESE_ENGLISH_PRECEDENCE.search(normalized) is None
    )


def _declares_non_chinese_conflict_precedence(value: str) -> bool:
    normalized = _normalized_policy_text(value)
    return (
        _ENGLISH_ENGLISH_PRECEDENCE.search(normalized) is not None
        or _CHINESE_ENGLISH_PRECEDENCE.search(normalized) is not None
    )


def _looks_like_language_policy_declaration(value: str) -> bool:
    normalized = _normalized_policy_text(value)
    folded = normalized.casefold()
    english_like = (
        "english" in folded
        and "chinese" in folded
        and (
            _has_english_equal_validity_signal(normalized)
            or _ENGLISH_LANGUAGE_POLICY_CONFLICT.search(normalized) is not None
        )
    )
    chinese_like = (
        "中英文" in normalized or ("中文" in normalized and "英文" in normalized)
    ) and (
        _has_chinese_equal_validity_signal(normalized)
        or _CHINESE_LANGUAGE_POLICY_CONFLICT.search(normalized) is not None
    )
    return english_like or chinese_like


def _has_english_equal_validity_signal(value: str) -> bool:
    folded = value.casefold()
    return _ENGLISH_LANGUAGE_POLICY_EQUALITY.search(value) is not None or (
        "equal" in folded and ("valid" in folded or "binding" in folded)
    )


def _has_chinese_equal_validity_signal(value: str) -> bool:
    return _CHINESE_LANGUAGE_POLICY_EQUALITY.search(value) is not None or (
        ("同等" in value or "相同" in value)
        and ("有效" in value or "效力" in value or "约束" in value)
    )


def _bilingual_structure_finding(
    *,
    snapshot: SourceIRSnapshot,
    resolver_id: str,
    resolver_version: str,
    code: str,
    severity: StructureFindingSeverity,
    blocking: bool,
    structure_node_id: str,
    source_refs: tuple[StructureSourceRef, ...],
) -> StructureFinding:
    identity_payload = ":".join(
        (
            code,
            structure_node_id,
            *(source_ref.source_id for source_ref in source_refs),
        )
    )
    identity_key = (
        "finding:" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
    )
    return StructureFinding(
        finding_id=stable_structure_finding_id(
            source_ir_id=snapshot.source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            code=code,
            identity_key=identity_key,
        ),
        identity_key=identity_key,
        code=code,
        severity=severity,
        blocking=blocking,
        message_key=f"structure.{code}",
        structure_node_id=structure_node_id,
        source_refs=source_refs,
    )


def validate_structure_tree_against_source_ir(
    tree: StructureTreeSnapshot,
    snapshot: SourceIRSnapshot,
) -> None:
    """Fail closed unless all declared grounding matches the actual SourceIR.

    ``StructureTreeSnapshot`` is portable and therefore carries a compact
    source catalog.  This boundary check must run before persistence or serving
    so a self-reported/fabricated catalog cannot masquerade as SourceIR facts.
    """

    if not isinstance(tree, StructureTreeSnapshot):
        raise TypeError("tree must be a StructureTreeSnapshot")
    if not isinstance(snapshot, SourceIRSnapshot):
        raise TypeError("snapshot must be a SourceIRSnapshot")
    expected_node_ids = tuple(sorted(node.source_node_id for node in snapshot.nodes))
    expected_cell_ids = tuple(
        sorted(
            cell.cell_id
            for node in snapshot.nodes
            if node.table_payload is not None
            for cell in node.table_payload.cells
        )
    )
    expected_visual_text_observation_ids = tuple(
        sorted(
            observation.observation_id
            for visual_snapshot in snapshot.visual_text_snapshots
            for observation in visual_snapshot.observations
        )
    )
    expected_visual_table_cell_ids = tuple(
        sorted(
            cell.visual_table_cell_id
            for visual_table_snapshot in snapshot.visual_table_snapshots
            for cell in visual_table_snapshot.cells
        )
    )
    if (
        tree.source_ir_id != snapshot.source_ir_id
        or tree.source_node_ids != expected_node_ids
        or tree.source_cell_ids != expected_cell_ids
        or tree.visual_text_observation_ids != expected_visual_text_observation_ids
        or tree.visual_table_cell_ids != expected_visual_table_cell_ids
    ):
        raise StructureResolutionRejected(
            StructureResolutionRejectionCode.SOURCE_CATALOG_MISMATCH,
            "StructureTree grounding catalog does not match its SourceIR snapshot.",
        )

    if snapshot.status is SourceIRStatus.FAILED:
        raise StructureResolutionRejected(
            StructureResolutionRejectionCode.SOURCE_IR_FAILED,
            "A failed SourceIR observation cannot validate a StructureTree snapshot.",
        )

    # Re-derive the complete deterministic graph instead of trusting a
    # self-reported catalog or duplicating only part of the resolver rules in a
    # second validator.  This catches parent, outline, list, continuation,
    # table, relation, finding, and resolver-identity tampering fail closed.
    expected = _derive_structure_tree(
        snapshot,
        resolver_id=tree.resolver_id,
        resolver_version=tree.resolver_version,
    )
    if tree != expected:
        _reject_source_catalog_mismatch()


def _reject_invalid_table_grounding() -> None:
    raise StructureResolutionRejected(
        StructureResolutionRejectionCode.INVALID_TABLE_GROUNDING,
        "StructureTree table-row grounding does not match SourceIR.",
    )


def _reject_source_catalog_mismatch() -> None:
    raise StructureResolutionRejected(
        StructureResolutionRejectionCode.SOURCE_CATALOG_MISMATCH,
        "StructureTree source grounding does not match SourceIR.",
    )


def _primary_structure_kind(
    source_node: SourceNode,
    *,
    explicit_label: str | None,
    list_ordinal: int | None,
    toc_block: bool = False,
    document_region_heading: bool = False,
) -> StructureNodeKind:
    if toc_block:
        return StructureNodeKind.GROUP
    if (
        source_node.kind is SourceNodeKind.HEADING
        or explicit_label is not None
        or document_region_heading
    ):
        return StructureNodeKind.SECTION
    if list_ordinal is not None:
        return StructureNodeKind.CLAUSE
    if source_node.kind is SourceNodeKind.TABLE:
        return StructureNodeKind.TABLE
    return StructureNodeKind.CONTENT


def _append_visual_structure(
    *,
    snapshot: SourceIRSnapshot,
    observations: tuple[VisualTextObservation, ...],
    root_id: str,
    resolver_id: str,
    resolver_version: str,
    structure_nodes: list[StructureNode],
    continuation_specs: list[_ContinuationSpec],
    findings: list[StructureFinding],
    next_order: int,
) -> int:
    if not observations:
        return next_order

    section_stack: list[tuple[str, str]] = []
    last_section_by_label: dict[str, str] = {}
    current_section_id: str | None = None
    current_clause_id: str | None = None
    last_continuation_anchor_by_section: dict[str, tuple[int, str]] = {}
    continued_pages_by_section: set[tuple[str, int]] = set()
    furniture_ids = set(visual_pdf_page_furniture_ids(snapshot.visual_text_snapshots))

    for group in _visual_observation_groups(
        observations,
        furniture_ids=furniture_ids,
    ):
        observation = group.first
        is_toc = any(item.role == "TOC" for item in group.observations)
        is_furniture = any(
            item.observation_id in furniture_ids for item in group.observations
        )
        explicit_label = (
            None
            if is_furniture or is_toc
            else _explicit_section_label_from_text(
                group.raw_text,
                native_heading=False,
            )
        )
        list_ordinal = (
            None
            if is_furniture or is_toc or explicit_label is not None
            else _explicit_list_ordinal_from_text(group.raw_text)
        )
        if is_toc:
            kind = StructureNodeKind.GROUP
            boundary_basis = StructureBoundaryBasis.TOC_BLOCK
        elif is_furniture:
            kind = StructureNodeKind.CONTENT
            boundary_basis = StructureBoundaryBasis.PAGE_FURNITURE
        elif explicit_label is not None:
            kind = StructureNodeKind.SECTION
            boundary_basis = StructureBoundaryBasis.EXPLICIT_NUMBER
        elif list_ordinal is not None:
            kind = StructureNodeKind.CLAUSE
            boundary_basis = StructureBoundaryBasis.EXPLICIT_NUMBER
        else:
            kind = StructureNodeKind.CONTENT
            boundary_basis = StructureBoundaryBasis.VISUAL_TEXT

        identity_key = f"visual:{observation.observation_id}"
        node_id = stable_structure_node_id(
            source_ir_id=snapshot.source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            kind=kind,
            identity_key=identity_key,
        )
        source_refs = group.source_refs

        if is_toc or is_furniture:
            parent_id = root_id
        elif kind is StructureNodeKind.SECTION:
            parent_id, missing_parent_label = _section_parent(
                explicit_label,
                section_stack=section_stack,
                last_section_by_label=last_section_by_label,
                root_id=root_id,
            )
            if missing_parent_label is not None:
                finding_identity = (
                    "finding:"
                    + hashlib.sha256(
                        (
                            f"{observation.observation_id}:section_ancestor_missing"
                        ).encode("utf-8")
                    ).hexdigest()
                )
                finding_code = "section_ancestor_missing"
                findings.append(
                    StructureFinding(
                        finding_id=stable_structure_finding_id(
                            source_ir_id=snapshot.source_ir_id,
                            resolver_id=resolver_id,
                            resolver_version=resolver_version,
                            code=finding_code,
                            identity_key=finding_identity,
                        ),
                        identity_key=finding_identity,
                        code=finding_code,
                        severity=StructureFindingSeverity.WARNING,
                        blocking=False,
                        message_key="structure.section_ancestor_missing",
                        structure_node_id=node_id,
                        source_refs=source_refs,
                    )
                )
            current_section_id = node_id
            current_clause_id = None
            section_stack = _updated_section_stack(
                explicit_label,
                current_section_id,
                section_stack=section_stack,
            )
            if explicit_label is not None:
                last_section_by_label[explicit_label] = current_section_id
        elif kind is StructureNodeKind.CLAUSE:
            parent_id = current_section_id or root_id
            current_clause_id = node_id
        else:
            parent_id = current_clause_id or current_section_id or root_id

        structure_nodes.append(
            StructureNode(
                structure_node_id=node_id,
                identity_key=identity_key,
                kind=kind,
                parent_structure_node_id=parent_id,
                source_order=next_order,
                source_refs=source_refs,
                boundary_basis=boundary_basis,
                explicit_label=explicit_label,
                list_ordinal=list_ordinal,
            )
        )
        next_order += 1

        if (
            current_section_id is not None
            and not is_furniture
            and not is_toc
            and kind is not StructureNodeKind.SECTION
        ):
            previous = last_continuation_anchor_by_section.get(current_section_id)
            page_key = (current_section_id, observation.page_number)
            if (
                previous is not None
                and previous[0] < observation.page_number
                and page_key not in continued_pages_by_section
            ):
                continuation_specs.append(
                    _ContinuationSpec(
                        source_structure_node_id=previous[1],
                        target_structure_node_id=node_id,
                        source_refs=source_refs,
                    )
                )
                continued_pages_by_section.add(page_key)
            continuation_anchor_id = current_clause_id or node_id
            last_continuation_anchor_by_section[current_section_id] = (
                observation.page_number,
                continuation_anchor_id,
            )
    return next_order


def _primary_boundary_basis(
    source_node: SourceNode,
    *,
    explicit_label: str | None,
    list_ordinal: int | None,
    toc_block: bool = False,
) -> StructureBoundaryBasis:
    if toc_block:
        return StructureBoundaryBasis.TOC_BLOCK
    if source_node.kind in {
        SourceNodeKind.HEADER,
        SourceNodeKind.FOOTER,
        SourceNodeKind.WATERMARK,
    } or _is_pdf_image_furniture_node(source_node):
        return StructureBoundaryBasis.PAGE_FURNITURE
    if source_node.kind is SourceNodeKind.HEADING:
        return StructureBoundaryBasis.NATIVE_HEADING
    if explicit_label is not None or list_ordinal is not None:
        return StructureBoundaryBasis.EXPLICIT_NUMBER
    if source_node.kind is SourceNodeKind.TABLE:
        return StructureBoundaryBasis.TABLE_SPAN
    return StructureBoundaryBasis.NATIVE_BLOCK


def _is_pdf_image_furniture_node(source_node: SourceNode) -> bool:
    return bool(
        source_node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        and pdf_image_furniture_kind_from_diagnostic_codes(
            diagnostic.code for diagnostic in source_node.extraction_diagnostics
        )
        is not None
    )


def _explicit_section_label(source_node: SourceNode) -> str | None:
    if source_node.kind not in {SourceNodeKind.HEADING, SourceNodeKind.PARAGRAPH}:
        return None
    return _explicit_section_label_from_text(
        source_node.raw_text,
        native_heading=source_node.kind is SourceNodeKind.HEADING,
    )


def _credible_bare_dotted_section_labels(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
) -> dict[str, str]:
    """Recover a split heading only inside an exact sibling bracket.

    A payload-free dotted number is ambiguous with measurements and page
    furniture.  Promotion therefore requires all independent facts together:
    the immediately following PDF block is a close title-shaped continuation,
    the nearest same-parent labels are exactly the previous and next ordinals,
    and all three labels share the same left-edge layout.  Customer vocabulary,
    document identity, and fixed clause numbers are deliberately absent.
    """

    recovered: dict[str, str] = {}
    for index, candidate in enumerate(source_nodes[:-1]):
        source_id = candidate.source_node_id
        label = _bare_dotted_section_label_from_text(candidate.raw_text)
        if (
            source_id in excluded_source_ids
            or explicit_labels.get(source_id) is not None
            or label is None
            or candidate.kind is not SourceNodeKind.PARAGRAPH
        ):
            continue
        title = source_nodes[index + 1]
        if (
            title.source_node_id in excluded_source_ids
            or explicit_labels.get(title.source_node_id) is not None
            or not _bare_label_and_title_share_heading_layout(candidate, title)
        ):
            continue
        candidate_parts = tuple(int(part) for part in label.split("."))
        parent_parts = candidate_parts[:-1]
        ordinal = candidate_parts[-1]
        previous = _nearest_same_or_higher_outline_boundary(
            reversed(source_nodes[:index]),
            explicit_labels=explicit_labels,
            candidate_depth=len(candidate_parts),
        )
        following = _nearest_same_or_higher_outline_boundary(
            iter(source_nodes[index + 2 :]),
            explicit_labels=explicit_labels,
            candidate_depth=len(candidate_parts),
        )
        if previous is None or following is None:
            continue
        previous_label = explicit_labels[previous.source_node_id]
        following_label = explicit_labels[following.source_node_id]
        assert previous_label is not None and following_label is not None
        previous_parts = _outline_peer_parts(
            previous_label,
            parent_parts=parent_parts,
        )
        following_parts = _outline_peer_parts(
            following_label,
            parent_parts=parent_parts,
        )
        if (
            previous_parts is None
            or following_parts is None
            or previous_parts[-1] != ordinal - 1
            or following_parts[-1] != ordinal + 1
            or not _bare_section_peer_window_is_bounded(
                previous,
                candidate,
                following,
            )
        ):
            continue
        recovered[source_id] = label
    return recovered


def _nearest_same_or_higher_outline_boundary(
    nodes: Iterable[SourceNode],
    *,
    explicit_labels: dict[str, str | None],
    candidate_depth: int,
) -> SourceNode | None:
    for node in nodes:
        label = explicit_labels.get(node.source_node_id)
        if label is not None and len(label.split(".")) <= candidate_depth:
            return node
    return None


def _outline_peer_parts(
    label: str,
    *,
    parent_parts: tuple[int, ...],
) -> tuple[int, ...] | None:
    parts = tuple(int(part) for part in label.split("."))
    if len(parts) != len(parent_parts) + 1 or parts[:-1] != parent_parts:
        return None
    return parts


def _bare_label_and_title_share_heading_layout(
    label: SourceNode,
    title: SourceNode,
) -> bool:
    title_text = " ".join(title.raw_text.split())
    if (
        label.native_locator.format is not NativeDocumentFormat.PDF
        or title.native_locator.format is not NativeDocumentFormat.PDF
        or label.native_locator.page_number is None
        or label.native_locator.page_number != title.native_locator.page_number
        or label.native_locator.bbox is None
        or title.native_locator.bbox is None
        or title.kind
        not in {
            SourceNodeKind.PARAGRAPH,
            SourceNodeKind.LIST,
            SourceNodeKind.LIST_ITEM,
        }
        or not title_text
        or len(title_text) > 160
        or _SENTENCE_TERMINATOR.search(title_text)
        or _EXPLICIT_SECTION_PREFIX.match(title.raw_text) is not None
        or _EXPLICIT_LIST_PREFIX.match(title.raw_text) is not None
        or _is_high_confidence_caption_boundary(title.raw_text)
    ):
        return False
    label_bbox = label.native_locator.bbox
    title_bbox = title.native_locator.bbox
    label_height = label_bbox[3] - label_bbox[1]
    title_height = title_bbox[3] - title_bbox[1]
    gap = title_bbox[1] - label_bbox[3]
    return bool(
        label_height > 0.0
        and title_height > 0.0
        and -1.0 <= gap <= max(12.0, min(label_height, title_height) * 1.5)
        and label_bbox[0] - 2.0 <= title_bbox[0] <= label_bbox[2] + 16.0
        and title_bbox[2] > label_bbox[2]
    )


def _bare_section_peer_window_is_bounded(
    previous: SourceNode,
    candidate: SourceNode,
    following: SourceNode,
) -> bool:
    locators = (
        previous.native_locator,
        candidate.native_locator,
        following.native_locator,
    )
    if any(
        locator.format is not NativeDocumentFormat.PDF
        or locator.page_number is None
        or locator.bbox is None
        for locator in locators
    ):
        return False
    previous_locator, candidate_locator, following_locator = locators
    assert previous_locator.page_number is not None
    assert candidate_locator.page_number is not None
    assert following_locator.page_number is not None
    assert previous_locator.bbox is not None
    assert candidate_locator.bbox is not None
    assert following_locator.bbox is not None
    return bool(
        0 <= candidate_locator.page_number - previous_locator.page_number <= 2
        and 0 <= following_locator.page_number - candidate_locator.page_number <= 2
        and candidate.source_order - previous.source_order <= 32
        and following.source_order - candidate.source_order <= 32
        and abs(previous_locator.bbox[0] - candidate_locator.bbox[0]) <= 12.0
        and abs(following_locator.bbox[0] - candidate_locator.bbox[0]) <= 12.0
    )


def _credible_embedded_peer_segments(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
    embedded_section_segments: dict[str, tuple[_ExplicitSectionSegment, ...]],
) -> tuple[
    dict[str, tuple[_EmbeddedPeerSegment, ...]],
    dict[str, int],
]:
    """Promote only exact-cover peer runs with independent closure evidence."""

    resolved: dict[str, tuple[_EmbeddedPeerSegment, ...]] = {}
    followup_ordinals: dict[str, int] = {}
    for index, node in enumerate(source_nodes):
        source_id = node.source_node_id
        if (
            source_id in excluded_source_ids
            or source_id in embedded_section_segments
            or explicit_labels.get(source_id) is not None
            or node.kind
            not in {
                SourceNodeKind.PARAGRAPH,
                SourceNodeKind.LIST,
                SourceNodeKind.LIST_ITEM,
            }
        ):
            continue
        runs = _embedded_parenthesized_peer_sequences(node.raw_text)
        if len(runs) != 1:
            continue
        run = runs[0]
        external_followup = None
        closed_pair = False
        if len(run) == 2:
            external_followup = _embedded_pair_external_followup(
                source_nodes,
                source_index=index,
                run=run,
                explicit_labels=explicit_labels,
                excluded_source_ids=excluded_source_ids,
                embedded_section_segments=embedded_section_segments,
            )
            closed_pair = _embedded_pair_has_outline_closure(
                source_nodes,
                source_index=index,
                explicit_labels=explicit_labels,
            )
        if len(run) < 3 and external_followup is None and not closed_pair:
            continue
        marker_family = (
            _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED
            if run[0].family == "alphabetic_parenthesized"
            else _OutlineMarkerFamily.NUMERIC_PARENTHESIZED
        )
        segments: list[_EmbeddedPeerSegment] = []
        segment_starts = tuple(
            node.raw_text.rfind("\n", 0, marker.start_offset) + 1
            for marker in run
        )
        if segment_starts and not node.raw_text[: segment_starts[0]].strip():
            segment_starts = (0, *segment_starts[1:])
        for marker_index, marker in enumerate(run):
            start_offset = segment_starts[marker_index]
            end_offset = (
                segment_starts[marker_index + 1]
                if marker_index + 1 < len(run)
                else len(node.raw_text)
            )
            if end_offset <= marker.end_offset:
                segments = []
                break
            segments.append(
                _EmbeddedPeerSegment(
                    marker_family=marker_family,
                    ordinal=marker.ordinal,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    text_indent=marker.text_indent,
                )
            )
        if not segments:
            continue
        resolved[source_id] = tuple(segments)
        if (
            external_followup is not None
            and marker_family is _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED
        ):
            followup_source_id, ordinal = external_followup
            followup_ordinals[followup_source_id] = ordinal
    return resolved, followup_ordinals


def _embedded_pair_external_followup(
    source_nodes: tuple[SourceNode, ...],
    *,
    source_index: int,
    run: tuple[_EmbeddedPeerMarkerEvidence, ...],
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
    embedded_section_segments: dict[str, tuple[_ExplicitSectionSegment, ...]],
) -> tuple[str, int] | None:
    first = run[0]
    last = run[-1]
    family = first.family
    expected_ordinal = last.ordinal + 1
    uppercase = first.uppercase
    anchor = source_nodes[source_index]
    anchor_indent = _source_text_offset_indent(
        anchor,
        offset=first.start_offset,
    )
    anchor_page = anchor.native_locator.page_number
    # Scan until a real outline/content boundary, not for a fixed number of
    # SourceIR nodes.  A single physical table may legitimately contribute
    # many excluded cell-text facts, so a node-count window makes the result
    # depend on table density rather than document structure.  The page and
    # first-substantive-fact guards below keep this search bounded and closed.
    for candidate in source_nodes[source_index + 1 :]:
        page_number = candidate.native_locator.page_number
        if (
            anchor_page is not None
            and page_number is not None
            and page_number - anchor_page > 1
        ):
            return None
        candidate_embedded_sections = embedded_section_segments.get(
            candidate.source_node_id,
            (),
        )
        candidate_prefix_end = (
            candidate_embedded_sections[0].start_offset
            if candidate_embedded_sections
            else len(candidate.raw_text)
        )
        candidate_prefix = candidate.raw_text[:candidate_prefix_end]
        # A peer may share one immutable PDF block with the next numbered
        # section.  The exact embedded-section offset closes the peer payload;
        # do not discard the whole source fact merely because its tail starts a
        # new outline boundary.
        if candidate_embedded_sections:
            if family != "alphabetic_parenthesized":
                return None
            marker = _parse_alphabetic_parenthesized_marker(
                candidate_prefix,
                require_payload=False,
            )
            candidate_indent = _source_text_offset_indent(
                candidate,
                offset=marker.start_offset if marker is not None else 0,
            )
            if (
                marker is not None
                and marker.ordinal == expected_ordinal
                and marker.uppercase == uppercase
                and candidate_prefix[marker.end_offset :].strip()
                and _list_indents_share_level(candidate_indent, anchor_indent)
            ):
                return candidate.source_node_id, marker.ordinal
            return None
        if explicit_labels.get(candidate.source_node_id) is not None:
            return None
        if candidate.source_node_id in excluded_source_ids:
            continue
        if candidate.kind is SourceNodeKind.TABLE or _is_high_confidence_caption_boundary(
            candidate.raw_text
        ):
            continue
        if candidate.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}:
            continue
        if candidate.kind is SourceNodeKind.HEADING:
            return None
        if family == "alphabetic_parenthesized":
            marker = _parse_alphabetic_parenthesized_marker(
                candidate.raw_text,
                require_payload=False,
            )
            if (
                marker is None
                or not candidate.raw_text[marker.end_offset :].strip()
            ):
                return None
            ordinal = marker.ordinal
            same_case = marker.uppercase == uppercase
        else:
            ordinal = _explicit_list_ordinal_from_text(candidate.raw_text)
            same_case = True
            if ordinal is None:
                return None
        candidate_indent = _source_text_offset_indent(
            candidate,
            offset=marker.start_offset if family == "alphabetic_parenthesized" else 0,
        )
        if (
            ordinal == expected_ordinal
            and same_case
            and _list_indents_share_level(candidate_indent, anchor_indent)
        ):
            return candidate.source_node_id, ordinal
        return None
    return None


def _embedded_pair_has_outline_closure(
    source_nodes: tuple[SourceNode, ...],
    *,
    source_index: int,
    explicit_labels: dict[str, str | None],
) -> bool:
    owner_label = next(
        (
            explicit_labels.get(node.source_node_id)
            for node in reversed(source_nodes[:source_index])
            if explicit_labels.get(node.source_node_id) is not None
        ),
        None,
    )
    if owner_label is None:
        return False
    owner_parts = tuple(int(part) for part in owner_label.split("."))
    owner_page = source_nodes[source_index].native_locator.page_number
    for candidate in source_nodes[source_index + 1 : source_index + 13]:
        candidate_label = explicit_labels.get(candidate.source_node_id)
        if candidate_label is None:
            continue
        candidate_page = candidate.native_locator.page_number
        if (
            owner_page is not None
            and candidate_page is not None
            and candidate_page - owner_page > 2
        ):
            return False
        candidate_parts = tuple(int(part) for part in candidate_label.split("."))
        return bool(
            candidate_parts[0] == owner_parts[0]
            and not (
                len(candidate_parts) > len(owner_parts)
                and candidate_parts[: len(owner_parts)] == owner_parts
            )
        )
    return False


def _veto_native_continuation_section_labels(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
) -> dict[str, str | None]:
    """Keep a wrapped cross-reference line inside its physical paragraph.

    Native PDF blocks can split one sentence immediately before a numbered
    cross-reference.  Treating every line that starts with ``2.4.4`` as a
    section boundary moves that continuation into an older outline branch.
    A prior physical occurrence of the same numeric label, a substantially
    longer preceding line, geometry, and sentence completion are the
    authoritative evidence here.  Requiring a prior occurrence keeps a
    first-seen short heading intact; customer wording and the referenced
    number are deliberately ignored.
    """

    resolved = dict(explicit_labels)
    previous: SourceNode | None = None
    seen_labels: set[str] = set()
    for current in source_nodes:
        current_id = current.source_node_id
        if current_id in excluded_source_ids:
            previous = None
            continue
        current_label = resolved.get(current_id)
        if (
            current_label is not None
            and previous is not None
            and _native_blocks_split_numbered_reference(
                previous,
                current,
                label=current_label,
            )
        ):
            resolved[current_id] = None
        elif (
            current_label is not None
            and current_label in seen_labels
            and previous is not None
            and resolved.get(previous.source_node_id) is None
            and _EXPLICIT_SECTION_PREFIX.match(current.raw_text) is not None
            and _native_blocks_share_incomplete_paragraph(previous, current)
        ):
            resolved[current_id] = None
        elif current_label is not None:
            seen_labels.add(current_label)
        previous = current
    return resolved


def _native_blocks_split_numbered_reference(
    previous: SourceNode,
    current: SourceNode,
    *,
    label: str,
) -> bool:
    """Recognize a numeric value/reference wrapped onto the next PDF line.

    A line such as ``operating mode `` followed by ``2.1 to determine ...`` is
    ordinary sentence continuation, even though the second physical block
    starts like an outline label.  The lexical prefix and adjacent native
    geometry together are stronger evidence than the number alone.
    """

    if (
        previous.kind is not SourceNodeKind.PARAGRAPH
        or current.kind is not SourceNodeKind.PARAGRAPH
        or previous.native_locator.format is not NativeDocumentFormat.PDF
        or current.native_locator.format is not NativeDocumentFormat.PDF
        or previous.native_locator.page_number is None
        or previous.native_locator.page_number != current.native_locator.page_number
        or not _NATIVE_NUMBER_REFERENCE_TRAILER.search(previous.raw_text.rstrip())
    ):
        return False
    match = _EXPLICIT_SECTION_PREFIX.match(current.raw_text)
    if match is None or re.sub(r"[ \t]", "", match.group("label")) != label:
        return False
    previous_bbox = previous.native_locator.bbox
    current_bbox = current.native_locator.bbox
    if previous_bbox is None or current_bbox is None:
        return False
    previous_height = previous_bbox[3] - previous_bbox[1]
    current_height = current_bbox[3] - current_bbox[1]
    gap = current_bbox[1] - previous_bbox[3]
    if previous_height <= 0.0 or current_height <= 0.0 or gap < -1.0:
        return False
    max_gap = max(
        3.0,
        min(previous_height, current_height) * _NATIVE_CONTINUATION_MAX_GAP_RATIO,
    )
    horizontal_overlap = max(
        0.0,
        min(previous_bbox[2], current_bbox[2]) - max(previous_bbox[0], current_bbox[0]),
    )
    minimum_width = min(
        previous_bbox[2] - previous_bbox[0],
        current_bbox[2] - current_bbox[0],
    )
    return (
        gap <= max_gap
        and abs(previous_bbox[0] - current_bbox[0]) <= _NATIVE_CONTINUATION_MAX_X_SHIFT
        and minimum_width > 0.0
        and horizontal_overlap / minimum_width >= 0.35
    )


def _native_blocks_share_incomplete_paragraph(
    previous: SourceNode,
    current: SourceNode,
) -> bool:
    if (
        previous.kind is not SourceNodeKind.PARAGRAPH
        or current.kind is not SourceNodeKind.PARAGRAPH
        or previous.native_locator.format is not NativeDocumentFormat.PDF
        or current.native_locator.format is not NativeDocumentFormat.PDF
        or previous.native_locator.page_number is None
        or previous.native_locator.page_number != current.native_locator.page_number
        or _SENTENCE_TERMINATOR.search(previous.raw_text)
        or not _SENTENCE_TERMINATOR.search(current.raw_text)
    ):
        return False
    previous_bbox = previous.native_locator.bbox
    current_bbox = current.native_locator.bbox
    if previous_bbox is None or current_bbox is None:
        return False
    previous_height = previous_bbox[3] - previous_bbox[1]
    current_height = current_bbox[3] - current_bbox[1]
    previous_width = previous_bbox[2] - previous_bbox[0]
    current_width = current_bbox[2] - current_bbox[0]
    gap = current_bbox[1] - previous_bbox[3]
    if (
        previous_height <= 0.0
        or current_height <= 0.0
        or previous_width <= 0.0
        or current_width <= 0.0
        or gap < -1.0
    ):
        return False
    max_gap = max(
        3.0,
        min(previous_height, current_height) * _NATIVE_CONTINUATION_MAX_GAP_RATIO,
    )
    horizontal_overlap = max(
        0.0,
        min(previous_bbox[2], current_bbox[2]) - max(previous_bbox[0], current_bbox[0]),
    )
    minimum_width = min(
        previous_bbox[2] - previous_bbox[0],
        current_bbox[2] - current_bbox[0],
    )
    return (
        gap <= max_gap
        and abs(previous_bbox[0] - current_bbox[0]) <= _NATIVE_CONTINUATION_MAX_X_SHIFT
        and previous_width >= current_width * 1.5
        and minimum_width > 0.0
        and horizontal_overlap / minimum_width >= 0.35
    )


def _parenthesized_section_descends_from_outline_scope(
    raw_text: str,
    *,
    explicit_label: str | None,
    outline_scope: _ListScope | None,
) -> bool:
    """Join ``N.x）`` to its exact source-backed ``N）`` outline anchor."""

    if explicit_label is None or outline_scope is None:
        return False
    match = _EXPLICIT_SECTION_PREFIX.match(raw_text)
    if match is None or not _match_has_parenthesized_suffix(raw_text, match):
        return False
    normalized_label = re.sub(r"[ \t]", "", match.group("label"))
    parts = normalized_label.split(".")
    return (
        normalized_label == explicit_label
        and len(parts) > 1
        and parts[0] == str(outline_scope.ordinal)
    )


def _embedded_explicit_section_segments(
    raw_text: str,
) -> tuple[_ExplicitSectionSegment, ...]:
    """Split coherent outline runs inside one immutable native text fact.

    PDF extraction may preserve an entire page paragraph as one SourceNode.
    Multiple line-leading dotted labels inside that immutable fact are still
    authoritative boundaries.  Isolated numeric values may occur between real
    labels, so one outlier must not veto an otherwise coherent run.  Duplicate
    labels remain ambiguous and are excluded.  The resolver records exact
    character spans instead of copying or rewriting the customer text.
    """

    if not raw_text:
        return ()
    matches = tuple(_EMBEDDED_EXPLICIT_SECTION_PREFIX.finditer(raw_text))
    if len(matches) < 2:
        return ()

    labels = tuple(_normalized_embedded_section_label(match) for match in matches)
    label_counts = {label: labels.count(label) for label in set(labels)}
    candidates = tuple(
        (
            match,
            label,
            tuple(int(part) for part in label.split(".")),
        )
        for match, label in zip(matches, labels, strict=True)
        if label_counts[label] == 1
        and _embedded_section_marker_has_payload(raw_text, match)
        and not _embedded_section_marker_is_wrapped_reference(raw_text, match)
    )
    if len(candidates) < 2:
        return ()

    runs: list[list[tuple[re.Match[str], str, tuple[int, ...]]]] = []
    current: list[tuple[re.Match[str], str, tuple[int, ...]]] = []
    for candidate in candidates:
        if current and not _coherent_embedded_outline_transition(
            current[-1][2],
            candidate[2],
        ):
            runs.append(current)
            current = []
        current.append(candidate)
    if current:
        runs.append(current)
    selected = tuple(candidate for run in runs if len(run) >= 2 for candidate in run)
    if not selected:
        return ()

    segments: list[_ExplicitSectionSegment] = []
    for index, (match, label, _) in enumerate(selected):
        start_offset = match.start("label")
        if index == 0 and not raw_text[:start_offset].strip():
            start_offset = 0
        end_offset = (
            selected[index + 1][0].start("label")
            if index + 1 < len(selected)
            else len(raw_text)
        )
        if end_offset <= match.end():
            return ()
        segments.append(
            _ExplicitSectionSegment(
                label=label,
                start_offset=start_offset,
                end_offset=end_offset,
            )
        )
    return tuple(segments)


def _normalized_embedded_section_label(match: re.Match[str]) -> str:
    return re.sub(
        r"[ \t]",
        "",
        match.group("label").replace("．", "."),
    )


def _credible_document_region_root_segments(
    source_nodes: tuple[SourceNode, ...],
    *,
    document_region_heading_source_ids: set[str],
    embedded_section_segments: dict[str, tuple[_ExplicitSectionSegment, ...]],
    explicit_labels: dict[str, str | None],
    table_outline_source_ids: frozenset[str],
) -> dict[str, tuple[_ExplicitSectionSegment, ...]]:
    """Recover single-dot roots only inside an already proven region.

    Single-dot numbering is also ordinary list syntax, so it is never promoted
    globally.  A region-local run must be immediate, its first root must own a
    grounded dotted child, and a transition to another immutable fact is
    allowed only when table arbitration retained that fact as a full-width
    outline row.
    """

    region_indices = tuple(
        index
        for index, node in enumerate(source_nodes)
        if node.source_node_id in document_region_heading_source_ids
    )
    if not region_indices:
        return {}

    selected_matches: dict[str, list[re.Match[str]]] = {}
    for region_position, region_start in enumerate(region_indices):
        region_end = (
            region_indices[region_position + 1]
            if region_position + 1 < len(region_indices)
            else len(source_nodes)
        )
        candidates = tuple(
            (index, node, match, int(match.group("label")))
            for index, node in enumerate(
                source_nodes[region_start:region_end],
                start=region_start,
            )
            for match in _credible_single_dot_root_matches(node.raw_text)
        )
        if len(candidates) < 2:
            continue

        runs: list[list[tuple[int, SourceNode, re.Match[str], int]]] = []
        current: list[tuple[int, SourceNode, re.Match[str], int]] = []
        for candidate in candidates:
            if current and not _region_root_candidates_are_immediate(
                current[-1],
                candidate,
                table_outline_source_ids=table_outline_source_ids,
            ):
                runs.append(current)
                current = []
            current.append(candidate)
        if current:
            runs.append(current)

        credible_runs = tuple(
            run
            for run in runs
            if len(run) >= 2
            and run[1][1].source_node_id == run[0][1].source_node_id
            and _region_root_run_starts_at_allowed_fact(
                run,
                source_nodes=source_nodes,
                region_start=region_start,
                region_end=region_end,
                table_outline_source_ids=table_outline_source_ids,
            )
            and _region_root_run_has_first_child(
                run,
                source_nodes=source_nodes,
                embedded_section_segments=embedded_section_segments,
                explicit_labels=explicit_labels,
            )
        )
        if len(credible_runs) != 1:
            continue
        for _, node, match, _ in credible_runs[0]:
            selected_matches.setdefault(node.source_node_id, []).append(match)

    return {
        source_id: _merge_explicit_section_boundaries(
            next(
                node.raw_text
                for node in source_nodes
                if node.source_node_id == source_id
            ),
            existing_segments=embedded_section_segments.get(source_id, ()),
            root_matches=tuple(matches),
        )
        for source_id, matches in selected_matches.items()
    }


def _region_root_run_starts_at_allowed_fact(
    run: list[tuple[int, SourceNode, re.Match[str], int]],
    *,
    source_nodes: tuple[SourceNode, ...],
    region_start: int,
    region_end: int,
    table_outline_source_ids: frozenset[str],
) -> bool:
    first_index, first_node, _, _ = run[0]
    region_source = source_nodes[region_start]
    if first_node.source_node_id == region_source.source_node_id:
        return True
    first_body_index = next(
        (
            index
            for index in range(region_start + 1, region_end)
            if source_nodes[index].kind is not SourceNodeKind.TABLE
            and source_nodes[index].raw_text.strip()
        ),
        None,
    )
    return (
        first_body_index == first_index
        and first_node.source_node_id not in table_outline_source_ids
    )


def _region_root_candidates_are_immediate(
    previous: tuple[int, SourceNode, re.Match[str], int],
    current: tuple[int, SourceNode, re.Match[str], int],
    *,
    table_outline_source_ids: frozenset[str],
) -> bool:
    _, previous_node, _, previous_label = previous
    _, current_node, _, current_label = current
    if current_label != previous_label + 1:
        return False
    if (
        current_node.source_node_id != previous_node.source_node_id
        and current_node.source_node_id not in table_outline_source_ids
    ):
        return False
    previous_page = previous_node.native_locator.page_number
    current_page = current_node.native_locator.page_number
    return not (
        previous_page is not None
        and current_page is not None
        and not 0 <= current_page - previous_page <= 1
    )


def _region_root_run_has_first_child(
    run: list[tuple[int, SourceNode, re.Match[str], int]],
    *,
    source_nodes: tuple[SourceNode, ...],
    embedded_section_segments: dict[str, tuple[_ExplicitSectionSegment, ...]],
    explicit_labels: dict[str, str | None],
) -> bool:
    first_index, first_node, first_match, first_label = run[0]
    second_index, second_node, second_match, _ = run[1]
    for index in range(first_index, second_index + 1):
        node = source_nodes[index]
        labels_and_offsets = tuple(
            (segment.label, segment.start_offset)
            for segment in embedded_section_segments.get(node.source_node_id, ())
        )
        if not labels_and_offsets:
            explicit_label = explicit_labels.get(node.source_node_id)
            labels_and_offsets = (
                ((explicit_label, 0),) if explicit_label is not None else ()
            )
        for label, start_offset in labels_and_offsets:
            parts = label.split(".")
            if len(parts) < 2 or parts[0] != str(first_label):
                continue
            if node.source_node_id == first_node.source_node_id and (
                start_offset <= first_match.start("label")
            ):
                continue
            if node.source_node_id == second_node.source_node_id and (
                start_offset >= second_match.start("label")
            ):
                continue
            return True
    return False


def _merge_explicit_section_boundaries(
    raw_text: str,
    *,
    existing_segments: tuple[_ExplicitSectionSegment, ...],
    root_matches: tuple[re.Match[str], ...],
) -> tuple[_ExplicitSectionSegment, ...]:
    boundaries = {segment.start_offset: segment.label for segment in existing_segments}
    boundaries.update(
        {match.start("label"): match.group("label") for match in root_matches}
    )
    ordered = tuple(sorted(boundaries.items()))
    merged: list[_ExplicitSectionSegment] = []
    for index, (start_offset, label) in enumerate(ordered):
        end_offset = (
            ordered[index + 1][0] if index + 1 < len(ordered) else len(raw_text)
        )
        if end_offset <= start_offset + len(label):
            return ()
        merged.append(
            _ExplicitSectionSegment(
                label=label,
                start_offset=start_offset,
                end_offset=end_offset,
            )
        )
    return tuple(merged)


def _embedded_section_marker_has_payload(
    raw_text: str,
    match: re.Match[str],
) -> bool:
    line_end = raw_text.find("\n", match.end())
    if line_end < 0:
        line_end = len(raw_text)
    remainder = " ".join(raw_text[match.end() : line_end].split())
    return bool(remainder) and _ROOT_MEASUREMENT_PREFIX.match(remainder) is None


def _coherent_embedded_outline_transition(
    previous: tuple[int, ...],
    current: tuple[int, ...],
) -> bool:
    if current <= previous or previous[0] != current[0]:
        return False
    common_prefix_length = 0
    for previous_part, current_part in zip(previous, current):
        if previous_part != current_part:
            break
        common_prefix_length += 1
    return common_prefix_length >= max(1, min(len(previous), len(current)) - 1)


def _credible_trailing_section_segments(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    embedded_section_segments: dict[str, tuple[_ExplicitSectionSegment, ...]],
    excluded_source_ids: set[str],
) -> dict[str, tuple[_ExplicitSectionSegment, ...]]:
    """Recover source-bound internal labels using local sequence evidence.

    Some born-digital PDFs place the number for the *next* definition at the
    end of the preceding physical text block or merge a complete title into
    the preceding paragraph.  Three immediate peers under an observed parent
    remain sufficient.  A shorter one- or two-item run is accepted only when
    the nearest explicit outline labels on both sides prove the exact gap or
    close the candidate parent.  Customer text is not moved: exact half-open
    spans split the immutable source fact.
    """

    candidates: list[tuple[int, SourceNode, str, int]] = []
    observed_parent_labels: set[str] = set()
    parent_labels_before_index: dict[int, frozenset[str]] = {}
    indexed_explicit_labels: list[tuple[int, SourceNode, str]] = []
    for index, node in enumerate(source_nodes):
        label = explicit_labels.get(node.source_node_id)
        existing_segments = embedded_section_segments.get(node.source_node_id, ())
        observed_labels = (
            tuple(segment.label for segment in existing_segments)
            if existing_segments
            else (label,)
            if label is not None
            else ()
        )
        for observed_label in observed_labels:
            observed_parent_labels.add(observed_label)
            indexed_explicit_labels.append((index, node, observed_label))
        parent_labels_before_index[index] = frozenset(observed_parent_labels)
        if (
            node.source_node_id in excluded_source_ids
            or node.kind not in {SourceNodeKind.HEADING, SourceNodeKind.PARAGRAPH}
            or label is not None
        ):
            continue
        raw_text = node.raw_text
        embedded_matches = tuple(
            match
            for match in _EMBEDDED_EXPLICIT_SECTION_PREFIX.finditer(raw_text)
            if match.start("label") > 0
            and raw_text[: raw_text.rfind("\n", 0, match.start("label")) + 1].strip()
            and _embedded_section_marker_has_payload(raw_text, match)
            and not _embedded_section_marker_is_wrapped_reference(raw_text, match)
        )
        match = embedded_matches[0] if len(embedded_matches) == 1 else None
        if match is None:
            trailing_match = _TRAILING_EXPLICIT_SECTION_LABEL.search(raw_text)
            if trailing_match is not None and _marker_starts_physical_line(
                raw_text,
                trailing_match.start("label"),
            ):
                match = trailing_match
        if match is None:
            continue
        candidate_label = _normalized_embedded_section_label(match)
        start_offset = match.start("label")
        if start_offset < 0 or not raw_text[start_offset:].strip():
            continue
        candidates.append((index, node, candidate_label, start_offset))

    if not candidates:
        return {}

    runs: list[list[tuple[int, SourceNode, str, int]]] = []
    current: list[tuple[int, SourceNode, str, int]] = []
    for candidate in candidates:
        if current and not _immediate_outline_peer_transition(
            current[-1],
            candidate,
        ):
            if current:
                runs.append(current)
            current = []
        current.append(candidate)
    if current:
        runs.append(current)

    result: dict[str, tuple[_ExplicitSectionSegment, ...]] = {}
    for run in runs:
        first_index, _, first_label, _ = run[0]
        last_index, _, last_label, _ = run[-1]
        expected_parent = ".".join(first_label.split(".")[:-1])
        if expected_parent not in parent_labels_before_index[first_index]:
            continue
        if len(run) < 3 and not _short_embedded_run_is_outline_bounded(
            first_index=first_index,
            first_label=first_label,
            last_index=last_index,
            last_label=last_label,
            indexed_explicit_labels=tuple(indexed_explicit_labels),
        ):
            continue
        for _, node, label, start_offset in run:
            end_offset = len(node.raw_text)
            if end_offset <= start_offset:
                continue
            result[node.source_node_id] = (
                _ExplicitSectionSegment(
                    label=label,
                    start_offset=start_offset,
                    end_offset=end_offset,
                ),
            )
    return result


def _marker_starts_physical_line(raw_text: str, start_offset: int) -> bool:
    line_start = raw_text.rfind("\n", 0, start_offset) + 1
    return (
        bool(raw_text[:line_start].strip())
        and not raw_text[line_start:start_offset].strip()
    )


def _short_embedded_run_is_outline_bounded(
    *,
    first_index: int,
    first_label: str,
    last_index: int,
    last_label: str,
    indexed_explicit_labels: tuple[tuple[int, SourceNode, str], ...],
) -> bool:
    previous = next(
        (item for item in reversed(indexed_explicit_labels) if item[0] < first_index),
        None,
    )
    following = next(
        (item for item in indexed_explicit_labels if item[0] > last_index),
        None,
    )
    if previous is None or following is None:
        return False
    if not _outline_anchor_pages_are_local(previous[1], following[1]):
        return False

    previous_label = previous[2]
    following_label = following[2]
    preceded_by_peer = _immediate_outline_peer_labels(
        previous_label,
        first_label,
    )
    followed_by_peer = _immediate_outline_peer_labels(
        last_label,
        following_label,
    )
    parent_label = ".".join(first_label.split(".")[:-1])
    opened_by_parent = (
        previous_label == parent_label and first_label.split(".")[-1] == "1"
    )
    closed_by_parent_peer = _immediate_outline_peer_labels(
        parent_label,
        following_label,
    )
    return (preceded_by_peer and (followed_by_peer or closed_by_parent_peer)) or (
        opened_by_parent and followed_by_peer
    )


def _outline_anchor_pages_are_local(
    previous: SourceNode,
    following: SourceNode,
) -> bool:
    previous_page = previous.native_locator.page_number
    following_page = following.native_locator.page_number
    return not (
        previous_page is not None
        and following_page is not None
        and not 0 <= following_page - previous_page <= 2
    )


def _immediate_outline_peer_labels(previous_label: str, current_label: str) -> bool:
    previous_parts = tuple(int(part) for part in previous_label.split("."))
    current_parts = tuple(int(part) for part in current_label.split("."))
    return (
        len(previous_parts) == len(current_parts)
        and previous_parts[:-1] == current_parts[:-1]
        and current_parts[-1] == previous_parts[-1] + 1
    )


def _immediate_outline_peer_transition(
    previous: tuple[int, SourceNode, str, int],
    current: tuple[int, SourceNode, str, int],
) -> bool:
    previous_index, previous_node, previous_label, _ = previous
    current_index, current_node, current_label, _ = current
    if current_index - previous_index > _TRAILING_SECTION_MAX_SOURCE_GAP:
        return False
    previous_page = previous_node.native_locator.page_number
    current_page = current_node.native_locator.page_number
    if (
        previous_page is not None
        and current_page is not None
        and not 0 <= current_page - previous_page <= 1
    ):
        return False
    return len(previous_label.split(".")) >= 2 and _immediate_outline_peer_labels(
        previous_label,
        current_label,
    )


def _credible_body_appendix_heading_source_ids(
    source_nodes: tuple[SourceNode, ...],
    *,
    excluded_source_ids: set[str],
    untrusted_native_pages: set[int],
) -> frozenset[str]:
    """Select the first trustworthy occurrence of each explicit appendix."""

    first_by_identity: dict[str, SourceNode] = {}
    for node in sorted(
        source_nodes,
        key=lambda item: (item.source_order, item.source_node_id),
    ):
        page_number = node.native_locator.page_number
        if (
            node.source_node_id in excluded_source_ids
            or page_number in untrusted_native_pages
        ):
            continue
        identity = _body_appendix_identity(node.raw_text)
        if identity is not None:
            first_by_identity.setdefault(identity, node)
    return frozenset(node.source_node_id for node in first_by_identity.values())


def _folded_document_region_sources_by_table(
    *,
    document_region_heading_source_ids: set[str],
    table_level_nodes: dict[str, tuple[SourceNode, ...]],
    row_level_selections: dict[
        tuple[str, int],
        tuple[_PdfTableTextSelection, ...],
    ],
) -> dict[str, tuple[SourceNode, ...]]:
    """Locate directory-proven region titles folded into page-layout tables."""

    collected: dict[str, dict[str, SourceNode]] = {}
    for table_source_id, nodes in table_level_nodes.items():
        for node in nodes:
            if node.source_node_id in document_region_heading_source_ids:
                collected.setdefault(table_source_id, {})[node.source_node_id] = node
    for (table_source_id, _), selections in row_level_selections.items():
        for selection in selections:
            node = selection.source_node
            if node.source_node_id in document_region_heading_source_ids:
                collected.setdefault(table_source_id, {})[node.source_node_id] = node
    return {
        table_source_id: tuple(
            sorted(
                nodes.values(),
                key=lambda item: (item.source_order, item.source_node_id),
            )
        )
        for table_source_id, nodes in collected.items()
    }


def _credible_attachment_directory_region_headings(
    snapshot: SourceIRSnapshot,
    *,
    structural_source_nodes: tuple[SourceNode, ...],
    excluded_source_ids: set[str],
) -> frozenset[str]:
    """Resolve unnumbered attachment starts from directory and layout evidence.

    Some specifications print an attachment directory in the main body but
    omit ``附件 N`` on the attachment pages themselves. A page-top title is
    promoted only when at least two later titles follow the same ordered
    directory with strong normalized-title similarity. No directory wording
    or synthetic attachment number is copied into the resulting node.
    """

    structural_ids = {
        node.source_node_id
        for node in structural_source_nodes
        if node.source_node_id not in excluded_source_ids
    }
    ordered_text_nodes = tuple(
        sorted(
            (
                node
                for node in snapshot.nodes
                if node.kind
                in {
                    SourceNodeKind.HEADING,
                    SourceNodeKind.PARAGRAPH,
                    SourceNodeKind.LIST,
                    SourceNodeKind.LIST_ITEM,
                }
                and node.raw_text.strip()
            ),
            key=lambda node: (node.source_order, node.source_node_id),
        )
    )
    if not ordered_text_nodes:
        return frozenset()
    page_extents = _pdf_page_extents(snapshot.nodes)
    resolved: set[str] = set()
    for anchor_index, anchor in enumerate(ordered_text_nodes):
        if (
            anchor.source_node_id not in structural_ids
            or not _is_attachment_directory_heading(anchor.raw_text)
        ):
            continue
        entries = _attachment_directory_entries(
            ordered_text_nodes,
            anchor_index=anchor_index,
        )
        if len(entries) < _ATTACHMENT_DIRECTORY_MIN_MATCHED_REGIONS:
            continue
        anchor_page = anchor.native_locator.page_number
        later_titles = tuple(
            node
            for node in snapshot.nodes
            if node.source_node_id not in excluded_source_ids
            and node.parent_source_node_id is None
            and node.kind
            in {
                SourceNodeKind.HEADING,
                SourceNodeKind.PARAGRAPH,
                SourceNodeKind.LIST,
                SourceNodeKind.LIST_ITEM,
            }
            and node.raw_text.strip()
            and node.source_order > entries[-1][2]
            and (
                anchor_page is None
                or node.native_locator.page_number is None
                or node.native_locator.page_number > anchor_page
            )
            and _credible_page_top_region_title(
                node,
                page_extents=page_extents,
            )
        )
        matched = _ordered_attachment_title_matches(entries, later_titles)
        if len(matched) >= _ATTACHMENT_DIRECTORY_MIN_MATCHED_REGIONS:
            resolved.update(node.source_node_id for node in matched)
    return frozenset(resolved)


def _is_attachment_directory_heading(raw_text: str) -> bool:
    normalized = " ".join(unicodedata.normalize("NFKC", raw_text).split())
    ideographic = _IDEOGRAPHIC_NUMBERED_PREFIX.match(normalized)
    if ideographic is not None:
        normalized = ideographic.group("title").strip()
    else:
        explicit = _EXPLICIT_SECTION_PREFIX.match(normalized)
        if explicit is not None:
            normalized = normalized[explicit.end() :].strip()
    return _ATTACHMENT_DIRECTORY_TITLE.fullmatch(normalized) is not None


def _attachment_directory_entries(
    source_nodes: tuple[SourceNode, ...],
    *,
    anchor_index: int,
) -> tuple[tuple[int, str, int], ...]:
    anchor = source_nodes[anchor_index]
    anchor_page = anchor.native_locator.page_number
    run: list[tuple[int, str, int]] = []
    for node in source_nodes[
        anchor_index + 1 : anchor_index + 1 + _ATTACHMENT_DIRECTORY_MAX_ROW_SCAN
    ]:
        page_number = node.native_locator.page_number
        if (
            anchor_page is not None
            and page_number is not None
            and page_number != anchor_page
        ):
            break
        match = _ATTACHMENT_DIRECTORY_ROW.fullmatch(
            unicodedata.normalize("NFKC", node.raw_text)
        )
        if match is None:
            continue
        ordinal = int(match.group("ordinal"))
        title = next(
            (
                line.strip()
                for line in match.group("title").splitlines()
                if line.strip()
            ),
            "",
        )
        if not title or len(title) > _ATTACHMENT_REGION_TITLE_MAX_CHARACTERS:
            continue
        if not run:
            if ordinal != 1:
                continue
        elif ordinal != run[-1][0] + 1:
            break
        run.append((ordinal, title, node.source_order))
    return tuple(run)


def _pdf_page_extents(
    source_nodes: tuple[SourceNode, ...],
) -> dict[int, tuple[float, float]]:
    extents: dict[int, tuple[float, float]] = {}
    for node in source_nodes:
        page_number = node.native_locator.page_number
        bbox = node.native_locator.bbox
        if page_number is None or bbox is None:
            continue
        width, height = extents.get(page_number, (0.0, 0.0))
        extents[page_number] = (max(width, bbox[2]), max(height, bbox[3]))
    return extents


def _credible_page_top_region_title(
    node: SourceNode,
    *,
    page_extents: dict[int, tuple[float, float]],
) -> bool:
    if _body_appendix_heading(node.raw_text):
        # An explicit appendix marker is itself a source-bound region
        # boundary even when a page-layout table places it below the top band.
        return True
    normalized = _attachment_candidate_title(node.raw_text)
    if (
        not normalized
        or len(normalized) > _ATTACHMENT_REGION_TITLE_MAX_CHARACTERS
        or (
            _explicit_section_label(node) is not None
            and "\n" not in node.raw_text.strip()
        )
        or _explicit_list_ordinal(node) is not None
        or (
            _IDEOGRAPHIC_NUMBERED_PREFIX.match(node.raw_text) is not None
            and "\n" not in node.raw_text.strip()
        )
    ):
        return False
    if node.kind is SourceNodeKind.HEADING:
        return True
    page_number = node.native_locator.page_number
    bbox = node.native_locator.bbox
    if page_number is None or bbox is None:
        return False
    page_width, page_height = page_extents.get(page_number, (0.0, 0.0))
    if page_width <= 0.0 or page_height <= 0.0:
        return False
    center_x = (bbox[0] + bbox[2]) / 2.0
    return (
        bbox[1] <= max(90.0, page_height * _ATTACHMENT_REGION_TITLE_TOP_RATIO)
        and abs(center_x - page_width / 2.0) <= page_width * 0.18
        and bbox[2] - bbox[0] <= page_width * _ATTACHMENT_REGION_TITLE_MAX_WIDTH_RATIO
    )


def _ordered_attachment_title_matches(
    entries: tuple[tuple[int, str, int], ...],
    later_titles: tuple[SourceNode, ...],
) -> tuple[SourceNode, ...]:
    matches: list[SourceNode] = []
    entry_cursor = 0
    for node in sorted(
        later_titles,
        key=lambda item: (item.source_order, item.source_node_id),
    ):
        best_index: int | None = None
        best_score = 0.0
        for index in range(
            entry_cursor,
            min(
                len(entries),
                entry_cursor + _ATTACHMENT_DIRECTORY_MAX_ENTRY_SKIP + 1,
            ),
        ):
            score = _attachment_title_similarity(
                entries[index][1],
                node.raw_text,
            )
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is None or best_score < 0.82:
            continue
        matches.append(node)
        entry_cursor = best_index + 1
        if entry_cursor >= len(entries):
            break
    return tuple(matches)


def _attachment_title_similarity(first: str, second: str) -> float:
    first_key = _normalized_attachment_title(first)
    second_key = _normalized_attachment_title(_attachment_candidate_title(second))
    if not first_key or not second_key:
        return 0.0
    if first_key == second_key:
        return 1.0
    shorter, longer = sorted((first_key, second_key), key=len)
    if len(shorter) >= 4 and shorter in longer and len(shorter) / len(longer) >= 0.35:
        return 0.9
    matcher = SequenceMatcher(None, first_key, second_key, autojunk=False)
    ratio = matcher.ratio()
    longest = matcher.find_longest_match().size
    if longest >= 4 and longest / len(shorter) >= 0.70:
        return max(ratio, 0.86)
    if longest >= 4 and longest / len(shorter) >= 0.55:
        return max(ratio, 0.84)
    return ratio


def _attachment_candidate_title(raw_text: str) -> str:
    """Extract only the page-start title line from a merged native block."""

    lines = tuple(
        line.strip()
        for line in unicodedata.normalize("NFKC", raw_text).splitlines()
        if line.strip()
    )
    first_line = lines[0] if lines else ""
    if not first_line:
        return ""
    body_appendix = _BODY_APPENDIX_HEADING.fullmatch(first_line)
    if body_appendix is not None:
        tail = body_appendix.group("tail").lstrip("-—:： ").strip()
        return tail or body_appendix.group("label")
    explicit = _EXPLICIT_SECTION_PREFIX.match(first_line)
    if explicit is not None:
        remainder = first_line[explicit.end() :].strip()
        if remainder:
            return remainder
    ideographic = _IDEOGRAPHIC_NUMBERED_PREFIX.match(first_line)
    if ideographic is not None:
        return ideographic.group("title").strip()
    compact_multiline = " ".join(lines)
    if len(lines) <= 6 and len(compact_multiline) <= 100:
        return compact_multiline
    return first_line


def _normalized_attachment_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _credible_ideographic_chapter_labels(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
) -> dict[str, str]:
    """Disambiguate ``N、标题`` as a chapter only from outline continuity.

    The same marker is also a common requirement-list item.  It is a root
    chapter when it is the immediate successor of the current numeric root, or
    when its own dotted child is observed before the next such marker.  Other
    occurrences remain candidates for the list resolver.
    """

    indexed_explicit_labels = tuple(
        (index, label)
        for index, node in enumerate(source_nodes)
        if (label := explicit_labels.get(node.source_node_id)) is not None
    )
    candidates = tuple(
        (
            index,
            node,
            int(match.group("ordinal")),
        )
        for index, node in enumerate(source_nodes)
        if node.source_node_id not in excluded_source_ids
        and explicit_labels.get(node.source_node_id) is None
        and (match := _IDEOGRAPHIC_NUMBERED_PREFIX.match(node.raw_text)) is not None
    )
    if not candidates:
        return {}

    groups: list[list[tuple[int, SourceNode, int]]] = []
    current_group: list[tuple[int, SourceNode, int]] = []
    for candidate in candidates:
        if current_group and not _local_ideographic_peer_transition(
            current_group[-1],
            candidate,
        ):
            groups.append(current_group)
            current_group = []
        current_group.append(candidate)
    if current_group:
        groups.append(current_group)

    result: dict[str, str] = {}
    for group in groups:
        first_index, first_node, first_ordinal = group[0]
        preceding_root = _nearest_preceding_explicit_root(
            first_index,
            indexed_explicit_labels=indexed_explicit_labels,
        )
        continues_root = (
            preceding_root is not None and first_ordinal == preceding_root + 1
        )
        coherent_chapter_run = len(group) >= 2 and continues_root
        for index, node, ordinal in group:
            candidate_preceding_root = _nearest_preceding_explicit_root(
                index,
                indexed_explicit_labels=indexed_explicit_labels,
            )
            has_local_child = _has_local_dotted_child_evidence(
                index,
                ordinal=ordinal,
                indexed_explicit_labels=indexed_explicit_labels,
            ) and (
                candidate_preceding_root is None
                or ordinal == candidate_preceding_root + 1
            )
            native_single_chapter = (
                len(group) == 1
                and continues_root
                and first_node.kind is SourceNodeKind.HEADING
            )
            if coherent_chapter_run or has_local_child or native_single_chapter:
                result[node.source_node_id] = str(ordinal)
    return result


def _local_ideographic_peer_transition(
    previous: tuple[int, SourceNode, int],
    current: tuple[int, SourceNode, int],
) -> bool:
    previous_index, previous_node, previous_ordinal = previous
    current_index, current_node, current_ordinal = current
    if (
        current_ordinal != previous_ordinal + 1
        or current_index - previous_index > _IDEOGRAPHIC_CHAPTER_MAX_SOURCE_GAP
    ):
        return False
    previous_page = previous_node.native_locator.page_number
    current_page = current_node.native_locator.page_number
    return not (
        previous_page is not None
        and current_page is not None
        and not 0 <= current_page - previous_page <= 1
    )


def _nearest_preceding_explicit_root(
    source_index: int,
    *,
    indexed_explicit_labels: tuple[tuple[int, str], ...],
) -> int | None:
    for index, label in reversed(indexed_explicit_labels):
        if index >= source_index:
            continue
        parts = _logical_outline_parts(label)
        if parts and parts[0].isdigit():
            return int(parts[0])
    return None


def _has_local_dotted_child_evidence(
    source_index: int,
    *,
    ordinal: int,
    indexed_explicit_labels: tuple[tuple[int, str], ...],
) -> bool:
    for index, label in indexed_explicit_labels:
        if index <= source_index:
            continue
        if index - source_index > _IDEOGRAPHIC_CHILD_EVIDENCE_MAX_SOURCE_GAP:
            return False
        parts = _logical_outline_parts(label)
        if len(parts) == 1:
            return False
        if parts[0].isdigit():
            return int(parts[0]) == ordinal
    return False


def _credible_ideographic_list_ordinals(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
) -> dict[str, int]:
    """Return only ``N、`` markers participating in a repeated peer sequence."""

    resolved: dict[str, int] = {}
    run: list[tuple[str, int]] = []

    def flush() -> None:
        if len(run) >= 2:
            resolved.update(run)
        run.clear()

    for node in source_nodes:
        source_id = node.source_node_id
        if explicit_labels.get(source_id) is not None:
            flush()
            continue
        if source_id in excluded_source_ids:
            continue
        match = _IDEOGRAPHIC_NUMBERED_PREFIX.match(node.raw_text)
        if match is None:
            continue
        ordinal = int(match.group("ordinal"))
        if run and ordinal != run[-1][1] + 1:
            flush()
        run.append((source_id, ordinal))
    flush()
    return resolved


def _explicit_list_ordinal(source_node: SourceNode) -> int | None:
    if source_node.kind not in {
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST,
        SourceNodeKind.LIST_ITEM,
    }:
        return None
    return _explicit_list_ordinal_from_text(source_node.raw_text)


def _explicit_list_ordinal_from_text(raw_text: str) -> int | None:
    match = _EXPLICIT_LIST_PREFIX.match(raw_text)
    return int(match.group("ordinal")) if match is not None else None


def _accepted_outline_marker_family(
    *,
    explicit_label: str | None,
    list_ordinal: int | None,
    ideographic_numeric: bool,
    alphabetic_parenthesized: bool,
) -> _OutlineMarkerFamily | None:
    """Classify only markers already accepted by their evidence prepass."""

    if explicit_label is not None:
        return _OutlineMarkerFamily.DOTTED_OUTLINE
    if list_ordinal is None:
        return None
    if ideographic_numeric:
        return _OutlineMarkerFamily.NUMERIC_IDEOGRAPHIC
    if alphabetic_parenthesized:
        return _OutlineMarkerFamily.ALPHABETIC_PARENTHESIZED
    return _OutlineMarkerFamily.NUMERIC_PARENTHESIZED


def _source_node_indent(source_node: SourceNode) -> float | None:
    bbox = source_node.native_locator.bbox
    return float(bbox[0]) if bbox is not None else None


def _source_text_offset_indent(
    source_node: SourceNode,
    *,
    offset: int,
) -> float | None:
    """Return the native line indent for one exact source-text offset.

    A PDF block can contain a list item followed by a less-indented numbered
    section.  Its union bbox therefore cannot represent the list marker's
    indentation.  Prefer the source-bound line geometry and retain the block
    bbox only as the format-neutral fallback.
    """

    for line in source_node.pdf_text_lines:
        if line.start_offset <= offset < line.end_offset:
            return float(line.bbox[0])
    return _source_node_indent(source_node)


def _list_indents_share_level(
    current_indent: float | None,
    previous_indent: float | None,
) -> bool:
    return (
        current_indent is None
        or previous_indent is None
        or abs(current_indent - previous_indent) <= _LIST_SCOPE_INDENT_TOLERANCE_POINTS
    )


def _list_indent_can_descend(
    child_indent: float | None,
    parent_indent: float | None,
) -> bool:
    return (
        child_indent is None
        or parent_indent is None
        or child_indent >= parent_indent - _LIST_SCOPE_INDENT_TOLERANCE_POINTS
    )


def _list_scope_closes_before_adjacent_table_introduction(
    *,
    active_scope: _ListScope,
    previous_source_node: SourceNode | None,
    introduction_source_node: SourceNode,
    following_source_node: SourceNode | None,
) -> bool:
    """Close only a proven peer list before a separate table introduction.

    PDF body text often uses the same left edge for list items and the
    paragraph that introduces a following table. Indentation alone therefore
    cannot decide ownership. The list is released only when a same-level peer
    sequence already exists, the preceding item is grammatically closed, and
    a separate colon-terminated paragraph is immediately followed by a nearby
    native table. No customer wording, clause number, or marker family is
    encoded here.
    """

    if (
        active_scope.peer_count < 2
        or previous_source_node is None
        or following_source_node is None
        or previous_source_node.kind
        not in {
            SourceNodeKind.PARAGRAPH,
            SourceNodeKind.LIST,
            SourceNodeKind.LIST_ITEM,
            SourceNodeKind.NOTE,
        }
        or introduction_source_node.kind is not SourceNodeKind.PARAGRAPH
        or following_source_node.kind is not SourceNodeKind.TABLE
        or not _LIST_ITEM_CLOSING_TERMINATOR.search(
            previous_source_node.raw_text.rstrip()
        )
        or not _TABLE_INTRODUCTION_TERMINATOR.search(
            introduction_source_node.raw_text.rstrip()
        )
        or _EXPLICIT_SECTION_PREFIX.match(introduction_source_node.raw_text) is not None
        or _EXPLICIT_LIST_PREFIX.match(introduction_source_node.raw_text) is not None
    ):
        return False

    previous_locator = previous_source_node.native_locator
    introduction_locator = introduction_source_node.native_locator
    table_locator = following_source_node.native_locator
    if (
        previous_locator.format is not NativeDocumentFormat.PDF
        or introduction_locator.format is not NativeDocumentFormat.PDF
        or table_locator.format is not NativeDocumentFormat.PDF
        or previous_locator.page_number is None
        or previous_locator.page_number != introduction_locator.page_number
        or introduction_locator.page_number != table_locator.page_number
        or previous_locator.bbox is None
        or introduction_locator.bbox is None
        or table_locator.bbox is None
    ):
        return False

    previous_bbox = previous_locator.bbox
    introduction_bbox = introduction_locator.bbox
    table_bbox = table_locator.bbox
    previous_height = previous_bbox[3] - previous_bbox[1]
    introduction_height = introduction_bbox[3] - introduction_bbox[1]
    introduction_width = introduction_bbox[2] - introduction_bbox[0]
    previous_gap = introduction_bbox[1] - previous_bbox[3]
    table_gap = table_bbox[1] - introduction_bbox[3]
    if (
        previous_height <= 0.0
        or introduction_height <= 0.0
        or introduction_width <= 0.0
        or previous_gap < -1.0
        or table_gap < -1.0
        or previous_gap > max(24.0, max(previous_height, introduction_height) * 2.0)
        or table_gap > max(12.0, introduction_height * 1.5)
    ):
        return False

    horizontal_overlap = max(
        0.0,
        min(introduction_bbox[2], table_bbox[2])
        - max(introduction_bbox[0], table_bbox[0]),
    )
    return horizontal_overlap / introduction_width >= 0.50


def _list_parent_structure_node_id(
    *,
    current_scopes: tuple[_ListScope, ...],
    current_section_id: str | None,
    root_id: str,
    marker_family: _OutlineMarkerFamily,
    ordinal: int,
    indent: float | None,
) -> _ListParentResolution:
    """Resolve one marker against the active, bounded list-scope stack.

    Peer continuity wins over family transitions.  This makes an ancestor
    ``6、`` close descendants introduced below ``5、`` while still allowing a
    new ``2）`` immediately below the active ``7、``.  Family transitions are
    intentionally a compact standards grammar, not a customer-template table.
    """

    default_parent = current_section_id or root_id
    if not current_scopes:
        return _ListParentResolution(
            parent_structure_node_id=default_parent,
            retained_scopes=(),
            peer_count=1,
        )

    # A coherent next ordinal resumes the nearest same-family level and closes
    # every descendant scope below it.  This is the principal stale-scope
    # guard: 5、 > 1） > 6、 resolves 6、 beside 5、, not below 1）.
    for index in range(len(current_scopes) - 1, -1, -1):
        scope = current_scopes[index]
        if (
            scope.marker_family is marker_family
            and ordinal == scope.ordinal + 1
            and _list_indents_share_level(indent, scope.indent)
        ):
            return _ListParentResolution(
                parent_structure_node_id=scope.parent_structure_node_id,
                retained_scopes=current_scopes[:index],
                peer_count=scope.peer_count + 1,
            )

    # Repeated/restarted ordinals at the same visual level remain peers.  The
    # source numbering is preserved as printed; no sequence is fabricated.
    for index in range(len(current_scopes) - 1, -1, -1):
        scope = current_scopes[index]
        if scope.marker_family is marker_family and _list_indents_share_level(
            indent, scope.indent
        ):
            return _ListParentResolution(
                parent_structure_node_id=scope.parent_structure_node_id,
                retained_scopes=current_scopes[:index],
                peer_count=scope.peer_count + 1,
            )

    current_scope = current_scopes[-1]
    can_descend_by_family = marker_family in _COMMON_CHILD_MARKER_FAMILIES.get(
        current_scope.marker_family,
        frozenset(),
    )
    same_family_deeper_indent = (
        marker_family is current_scope.marker_family
        and indent is not None
        and current_scope.indent is not None
        and indent > current_scope.indent + _LIST_SCOPE_INDENT_TOLERANCE_POINTS
    )
    if (
        can_descend_by_family or same_family_deeper_indent
    ) and _list_indent_can_descend(indent, current_scope.indent):
        return _ListParentResolution(
            parent_structure_node_id=current_scope.structure_node_id,
            retained_scopes=current_scopes,
            peer_count=1,
        )

    return _ListParentResolution(
        parent_structure_node_id=default_parent,
        retained_scopes=(),
        peer_count=1,
    )


def _structure_reading_key(source_node: SourceNode) -> tuple[object, ...]:
    locator = source_node.native_locator
    if locator.format is NativeDocumentFormat.PDF:
        bbox = locator.bbox
        y0 = bbox[1] if bbox is not None else float("inf")
        x0 = bbox[0] if bbox is not None else float("inf")
        native_index = (
            locator.block_index
            if locator.block_index is not None
            else locator.table_index
            if locator.table_index is not None
            else source_node.source_order
        )
        return (
            0,
            locator.page_number or 0,
            y0,
            x0,
            native_index,
            source_node.source_order,
            source_node.source_node_id,
        )
    return (1, source_node.source_order, source_node.source_node_id)


def _parent_first_source_nodes(
    source_nodes: tuple[SourceNode, ...],
    *,
    source_nodes_by_id: dict[str, SourceNode],
    folded_source_node_ids: set[str],
) -> tuple[SourceNode, ...]:
    """Keep visual order where possible while never placing a child first."""

    pending = list(source_nodes)
    emitted: set[str] = set()
    ordered: list[SourceNode] = []
    while pending:
        progressed = False
        for source_node in tuple(pending):
            parent_id = _nearest_retained_parent_source_id(
                source_node,
                source_nodes_by_id=source_nodes_by_id,
                folded_source_node_ids=folded_source_node_ids,
            )
            if parent_id is not None and parent_id not in emitted:
                continue
            ordered.append(source_node)
            emitted.add(source_node.source_node_id)
            pending.remove(source_node)
            progressed = True
        if not progressed:
            raise StructureResolutionRejected(
                StructureResolutionRejectionCode.SOURCE_CATALOG_MISMATCH,
                "SourceIR native parents could not be ordered before their children.",
            )
    return tuple(ordered)


def _toc_entry_groups(
    source_nodes: tuple[SourceNode, ...],
    *,
    furniture_source_ids: set[str],
) -> dict[str, tuple[SourceNode, ...]]:
    """Fold an explicit, possibly multi-page TOC into one grounded block.

    The source facts remain untouched.  This prepass only prevents directory
    entries from becoming authoritative body sections.  Continuation pages are
    accepted only while most visible text still has an outline-entry shape.
    """

    page_nodes: dict[int, list[SourceNode]] = {}
    for node in source_nodes:
        locator = node.native_locator
        if (
            locator.format is NativeDocumentFormat.PDF
            and locator.page_number is not None
            and node.kind
            in {
                SourceNodeKind.HEADING,
                SourceNodeKind.PARAGRAPH,
                SourceNodeKind.TOC_ENTRY,
            }
            and node.source_node_id not in furniture_source_ids
            and node.raw_text.strip()
        ):
            page_nodes.setdefault(locator.page_number, []).append(node)

    max_pdf_page_number = max(
        (
            node.native_locator.page_number or 0
            for node in source_nodes
            if node.native_locator.format is NativeDocumentFormat.PDF
        ),
        default=0,
    )
    groups: list[list[SourceNode]] = []
    current: list[SourceNode] = []
    continuing = False
    previous_page: int | None = None
    for page_number in sorted(page_nodes):
        nodes = sorted(page_nodes[page_number], key=_structure_reading_key)
        heading_index = next(
            (
                index
                for index, node in enumerate(nodes)
                if _is_toc_heading_text(node.raw_text)
            ),
            None,
        )
        if heading_index is not None:
            if current:
                groups.append(current)
            current, continuing = _toc_heading_page_segment(
                nodes,
                heading_index=heading_index,
            )
            previous_page = page_number
            continue

        if not current:
            headingless_segment = _toc_headingless_page_segment(
                nodes,
                page_number=page_number,
                page_nodes=page_nodes,
                max_pdf_page_number=max_pdf_page_number,
            )
            if headingless_segment is not None:
                current, continuing = headingless_segment
                previous_page = page_number
                continue

        is_adjacent = previous_page is not None and page_number == previous_page + 1
        continuation_segment = (
            _toc_continuation_page_segment(
                nodes,
                allow_weak=(
                    not any(
                        _toc_text_profile(node.raw_text).target_line_count
                        for node in current
                    )
                    and sum(
                        _toc_text_profile(node.raw_text).entry_line_count
                        for node in current
                    )
                    >= _TOC_WEAK_SEED_MIN_ENTRY_LINES
                ),
            )
            if continuing and is_adjacent
            else None
        )
        if continuation_segment is not None:
            selected, continuing = continuation_segment
            current.extend(selected)
            previous_page = page_number
            continue

        if current:
            groups.append(current)
        current = []
        continuing = False
        previous_page = page_number

    if current:
        groups.append(current)
    return {group[0].source_node_id: tuple(group) for group in groups if group}


def _is_toc_heading_text(raw_text: str) -> bool:
    value = re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(raw_text or "")),
    ).casefold()
    return bool(
        re.fullmatch(
            r"(?:目录|目次)(?:[（(]?续[）)]?)?|contents|tableofcontents",
            value,
        )
    )


def _toc_continuation_page_segment(
    nodes: list[SourceNode],
    *,
    allow_weak: bool,
) -> tuple[list[SourceNode], bool] | None:
    """Accept an adjacent page only when its text itself proves TOC shape."""

    scan = _scan_toc_page_prefix(nodes, start_index=0)
    if not scan.nodes:
        return None
    if scan.target_line_count >= _TOC_STRONG_CONTINUATION_MIN_TARGET_LINES:
        return list(scan.nodes), scan.consumed_all
    if (
        allow_weak
        and (scan.consumed_all or scan.layout_break)
        and scan.entry_line_count >= _TOC_WEAK_CONTINUATION_MIN_ENTRY_LINES
        and scan.entry_line_count == scan.line_count
    ):
        return list(scan.nodes), scan.consumed_all
    if len(scan.nodes) == 1 and scan.nodes[0].kind is SourceNodeKind.TOC_ENTRY:
        return list(scan.nodes), scan.consumed_all
    return None


def _toc_headingless_page_segment(
    nodes: list[SourceNode],
    *,
    page_number: int,
    page_nodes: dict[int, list[SourceNode]],
    max_pdf_page_number: int,
) -> tuple[list[SourceNode], bool] | None:
    """Seed a TOC only when repeated target-page evidence is self-sufficient.

    Some PDFs draw the visible ``CONTENTS`` title as paths while preserving the
    entry text.  Requiring at least three dot-leader/target-page lines keeps the
    fallback stricter than a normal continuation page and avoids treating a
    short list of cross-references as a directory.
    """

    scan = _scan_headingless_toc_page_prefix(nodes)
    target_page_numbers = tuple(
        page_number
        for node in scan.nodes
        for page_number in _toc_target_page_numbers(node.raw_text)
    )
    outline_line_count = sum(
        _toc_outline_entry_line_count(node.raw_text) for node in scan.nodes
    )
    if (
        not scan.nodes
        or scan.target_line_count < _TOC_HEADINGLESS_SEED_MIN_TARGET_LINES
        or outline_line_count < _TOC_HEADINGLESS_SEED_MIN_OUTLINE_LINES
        or scan.entry_line_count != scan.line_count
        or not scan.consumed_all
        or not target_page_numbers
        or max_pdf_page_number < 1
        or any(page_number > max_pdf_page_number for page_number in target_page_numbers)
        or not _headingless_toc_has_body_alignment(
            scan.nodes,
            toc_page_number=page_number,
            page_nodes=page_nodes,
        )
    ):
        return None
    return list(scan.nodes), scan.consumed_all


def _scan_headingless_toc_page_prefix(nodes: list[SourceNode]) -> _TocPageScan:
    """Scan only a proven target-page prefix when no TOC title is available.

    Without an explicit title, target-page syntax is not enough to turn a list
    in the middle of a body page into a directory.  The first substantive
    reading-order block must itself be a native TOC entry or consist entirely
    of outline/target-page evidence, and later blocks must remain contiguous.
    """

    selected: list[SourceNode] = []
    line_count = 0
    entry_line_count = 0
    target_line_count = 0
    previous: SourceNode | None = None
    consumed_all = True
    layout_break = False
    for index, node in enumerate(nodes):
        profile = _toc_text_profile(node.raw_text)
        is_native_entry = node.kind is SourceNodeKind.TOC_ENTRY
        is_target_page_text = (
            profile.line_count > 0
            and profile.target_line_count > 0
            and profile.entry_line_count == profile.line_count
        )
        if index == 0 and not is_native_entry and not is_target_page_text:
            consumed_all = False
            break
        if (
            previous is not None
            and entry_line_count >= 2
            and _is_toc_layout_break(previous, node, selected)
        ):
            consumed_all = False
            layout_break = True
            break
        if not is_native_entry and not is_target_page_text:
            consumed_all = False
            break
        selected.append(node)
        line_count += max(profile.line_count, int(is_native_entry))
        entry_line_count += max(profile.entry_line_count, int(is_native_entry))
        target_line_count += profile.target_line_count
        previous = node
    return _TocPageScan(
        nodes=tuple(selected),
        line_count=line_count,
        entry_line_count=entry_line_count,
        target_line_count=target_line_count,
        consumed_all=consumed_all,
        layout_break=layout_break,
    )


def _toc_heading_page_segment(
    nodes: list[SourceNode],
    *,
    heading_index: int,
) -> tuple[list[SourceNode], bool]:
    scan = _scan_toc_page_prefix(nodes, start_index=heading_index + 1)
    if scan.entry_line_count == 0:
        return [], False
    return [nodes[heading_index], *scan.nodes], scan.consumed_all


def _scan_toc_page_prefix(
    nodes: list[SourceNode],
    *,
    start_index: int,
) -> _TocPageScan:
    candidates = nodes[start_index:]
    profiles = tuple(_toc_text_profile(node.raw_text) for node in candidates)
    selected: list[SourceNode] = []
    line_count = 0
    entry_line_count = 0
    target_line_count = 0
    previous: SourceNode | None = None
    consumed_all = True
    layout_break = False
    for node, profile in zip(candidates, profiles):
        if (
            previous is not None
            and entry_line_count >= 2
            and _is_toc_layout_break(previous, node, selected)
        ):
            consumed_all = False
            layout_break = True
            break
        is_native_entry = node.kind is SourceNodeKind.TOC_ENTRY
        is_secondary_heading = _is_toc_heading_text(node.raw_text)
        is_toc_text = (
            is_secondary_heading
            or profile.target_line_count > 0
            or (
                profile.line_count > 0
                and profile.entry_line_count == profile.line_count
            )
        )
        if not is_native_entry and not is_toc_text:
            consumed_all = False
            break
        selected.append(node)
        line_count += (
            0 if is_secondary_heading else max(profile.line_count, int(is_native_entry))
        )
        entry_line_count += max(profile.entry_line_count, int(is_native_entry))
        target_line_count += profile.target_line_count
        previous = node
    return _TocPageScan(
        nodes=tuple(selected),
        line_count=line_count,
        entry_line_count=entry_line_count,
        target_line_count=target_line_count,
        consumed_all=consumed_all,
        layout_break=layout_break,
    )


def _is_toc_layout_break(
    previous: SourceNode,
    current: SourceNode,
    selected: list[SourceNode],
) -> bool:
    previous_bbox = previous.native_locator.bbox
    current_bbox = current.native_locator.bbox
    if previous_bbox is None or current_bbox is None:
        return False
    heights = [
        bbox[3] - bbox[1]
        for candidate in selected
        if (bbox := candidate.native_locator.bbox) is not None and bbox[3] > bbox[1]
    ]
    if not heights:
        return False
    typical_height = sorted(heights)[len(heights) // 2]
    return (
        current_bbox[1] - previous_bbox[3]
        > typical_height * _TOC_LAYOUT_BREAK_LINE_HEIGHT_MULTIPLIER
    )


def _toc_text_profile(raw_text: str) -> _TocTextProfile:
    lines = tuple(
        line.strip()
        for line in unicodedata.normalize("NFKC", str(raw_text or "")).splitlines()
        if line.strip()
    )
    target_line_count = sum(
        bool(_TOC_PAGE_TARGET_SUFFIX.search(line)) for line in lines
    )
    entry_line_count = sum(
        bool(_is_toc_entry_line(line) or _TOC_PAGE_TARGET_SUFFIX.search(line))
        for line in lines
    )
    return _TocTextProfile(
        line_count=len(lines),
        entry_line_count=entry_line_count,
        target_line_count=target_line_count,
    )


def _toc_target_page_numbers(raw_text: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for line in unicodedata.normalize("NFKC", str(raw_text or "")).splitlines():
        match = _TOC_PAGE_TARGET_SUFFIX.search(line.strip())
        if match is None:
            continue
        marker = match.group("target").upper()
        page_number = int(marker) if marker.isdigit() else _roman_numeral_value(marker)
        if page_number is not None:
            numbers.append(page_number)
    return tuple(numbers)


def _headingless_toc_has_body_alignment(
    nodes: tuple[SourceNode, ...],
    *,
    toc_page_number: int,
    page_nodes: dict[int, list[SourceNode]],
) -> bool:
    """Require a title-less TOC seed to point at later outline evidence.

    Dot leaders and plausible page numbers are not enough on their own: a
    full-page calibration or cross-reference list can have the same shape.
    A single exact, non-empty title match is strong enough.  When the native
    text layer has lost the titles, at least two dotted outline labels must
    land on later body pages with the same printed-page-to-PDF-page offset.
    """

    entries = _toc_target_entries(nodes)
    if not entries:
        return False
    body_evidence = _toc_body_outline_evidence(
        page_nodes,
        after_page_number=toc_page_number,
    )
    if not body_evidence:
        return False

    dotted_labels_by_offset: dict[int, set[str]] = {}
    for entry in entries:
        for evidence in body_evidence:
            offset = evidence.page_number - entry.target_page_number
            if (
                entry.title_key
                and evidence.title_key
                and entry.title_key == evidence.title_key
                and entry.label is not None
                and entry.label == evidence.label
            ):
                return True
            if (
                entry.label is not None
                and "." in entry.label
                and entry.label == evidence.label
            ):
                dotted_labels_by_offset.setdefault(offset, set()).add(entry.label)
    return any(
        len(labels) >= _TOC_HEADINGLESS_SEED_MIN_OUTLINE_LINES
        for labels in dotted_labels_by_offset.values()
    )


def _toc_target_entries(nodes: tuple[SourceNode, ...]) -> tuple[_TocTargetEntry, ...]:
    entries: list[_TocTargetEntry] = []
    pending_entry_text = ""
    for node in nodes:
        for raw_line in unicodedata.normalize(
            "NFKC",
            str(node.raw_text or ""),
        ).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            target_match = _TOC_PAGE_TARGET_SUFFIX.search(line)
            if target_match is None:
                pending_entry_text = line if _is_toc_entry_line(line) else ""
                continue
            target_marker = target_match.group("target").upper()
            target_page_number = (
                int(target_marker)
                if target_marker.isdigit()
                else _roman_numeral_value(target_marker)
            )
            entry_text = line[: target_match.start()].strip() or pending_entry_text
            pending_entry_text = ""
            if target_page_number is None or not entry_text:
                continue
            label, title_key = _toc_outline_label_and_title(entry_text)
            entries.append(
                _TocTargetEntry(
                    label=label,
                    title_key=title_key,
                    target_page_number=target_page_number,
                )
            )
    return tuple(entries)


def _toc_body_outline_evidence(
    page_nodes: dict[int, list[SourceNode]],
    *,
    after_page_number: int,
) -> tuple[_TocBodyOutlineEvidence, ...]:
    evidence: set[_TocBodyOutlineEvidence] = set()
    for page_number, nodes in page_nodes.items():
        if page_number <= after_page_number:
            continue
        for node in nodes:
            if (
                node.kind is SourceNodeKind.TOC_ENTRY
                or _toc_text_profile(node.raw_text).target_line_count
            ):
                continue
            for raw_line in unicodedata.normalize(
                "NFKC",
                str(node.raw_text or ""),
            ).splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                label, title_key = _toc_outline_label_and_title(line)
                if label is None and not title_key:
                    continue
                evidence.add(
                    _TocBodyOutlineEvidence(
                        page_number=page_number,
                        label=label,
                        title_key=title_key,
                    )
                )
    return tuple(
        sorted(
            evidence,
            key=lambda item: (
                item.page_number,
                item.label or "",
                item.title_key,
            ),
        )
    )


def _toc_outline_label_and_title(raw_text: str) -> tuple[str | None, str]:
    value = unicodedata.normalize("NFKC", str(raw_text or "")).strip()
    numeric_match = _TOC_NUMERIC_ENTRY_PREFIX.match(value)
    if numeric_match is not None:
        label = re.sub(r"\s+", "", numeric_match.group(0))
        title = value[numeric_match.end() :].strip()
        return label, _toc_title_key(title)
    chinese_match = _CHINESE_SECTION_PREFIX.match(value)
    if chinese_match is not None:
        label = chinese_match.group("ordinal")
        return label, _toc_title_key(chinese_match.group("title"))
    appendix_match = _TOC_APPENDIX_LABEL_PREFIX.match(value)
    if appendix_match is not None:
        label = re.sub(r"\s+", "", appendix_match.group("label")).casefold()
        title = value[appendix_match.end() :].strip()
        return label, _toc_title_key(title)
    return None, _toc_title_key(value)


def _toc_title_key(raw_text: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(raw_text or "")).casefold().split()
    )


def _toc_outline_entry_line_count(raw_text: str) -> int:
    return sum(
        _is_toc_entry_line(line)
        for line in unicodedata.normalize("NFKC", str(raw_text or "")).splitlines()
        if line.strip()
    )


def _is_toc_entry_line(raw_text: str) -> bool:
    value = unicodedata.normalize("NFKC", str(raw_text or "")).strip()
    if not value:
        return False
    return bool(
        _CHINESE_SECTION_PREFIX.match(value)
        or _TOC_NUMERIC_ENTRY_PREFIX.match(value)
        or _TOC_APPENDIX_ENTRY_PREFIX.match(value)
    )


def _credible_chinese_section_labels(
    source_nodes: tuple[SourceNode, ...],
    *,
    excluded_source_ids: set[str],
    toc_title_keys: set[str],
) -> dict[str, str]:
    """Promote Chinese chapter labels only with local structural evidence."""

    candidates: list[tuple[SourceNode, int]] = []
    for node in source_nodes:
        if node.source_node_id in excluded_source_ids:
            continue
        ordinal = _chinese_section_ordinal(node.raw_text)
        if ordinal is None:
            continue
        candidates.append((node, ordinal))

    credible_ids = {
        node.source_node_id
        for node, _ in candidates
        if node.kind is SourceNodeKind.HEADING
        or _normalized_outline_title(node.raw_text) in toc_title_keys
    }
    source_index_by_id = {
        node.source_node_id: index for index, node in enumerate(source_nodes)
    }
    for index, (node, ordinal) in enumerate(candidates):
        start = source_index_by_id[node.source_node_id] + 1
        end = (
            source_index_by_id[candidates[index + 1][0].source_node_id]
            if index + 1 < len(candidates)
            else len(source_nodes)
        )
        for following in source_nodes[start:end]:
            if following.source_node_id in excluded_source_ids:
                continue
            label = _explicit_section_label(following)
            if label is not None and label.split(".", 1)[0] == str(ordinal):
                credible_ids.add(node.source_node_id)
                break

    return {
        node.source_node_id: str(ordinal)
        for node, ordinal in candidates
        if node.source_node_id in credible_ids
    }


def _chinese_section_title_keys(raw_text: str) -> set[str]:
    return {
        _normalized_outline_title(line)
        for line in str(raw_text or "").splitlines()
        if _chinese_section_ordinal(line) is not None
    }


def _normalized_outline_title(raw_text: str) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(raw_text or "")),
    ).casefold()


def _chinese_section_ordinal(raw_text: str) -> int | None:
    match = _CHINESE_SECTION_PREFIX.match(
        unicodedata.normalize("NFKC", str(raw_text or ""))
    )
    if match is None:
        return None
    return _chinese_numeral_value(match.group("ordinal"))


def _chinese_numeral_value(value: str) -> int | None:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100}
    total = 0
    current = 0
    for character in value:
        if character in digits:
            current = digits[character]
            continue
        unit = units.get(character)
        if unit is None:
            return None
        total += (current or 1) * unit
        current = 0
    result = total + current
    return result if 1 <= result <= 999 else None


def _roman_numeral_value(value: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(value):
        number = values.get(character)
        if number is None:
            return None
        total += -number if number < previous else number
        previous = max(previous, number)
    return total if 0 < total <= 3999 and _roman_numeral(total) == value else None


def _roman_numeral(value: int) -> str:
    parts: list[str] = []
    for number, numeral in (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    ):
        count, value = divmod(value, number)
        parts.append(numeral * count)
    return "".join(parts)


def _visual_observation_groups(
    observations: tuple[VisualTextObservation, ...],
    *,
    furniture_ids: set[str],
) -> tuple[_VisualObservationGroup, ...]:
    """Merge only tightly wrapped visual lines into one paragraph boundary."""

    groups: list[list[VisualTextObservation]] = []
    for observation in observations:
        structural = (
            observation.observation_id in furniture_ids
            or _explicit_section_label_from_text(
                observation.raw_text,
                native_heading=False,
            )
            is not None
            or _explicit_list_ordinal_from_text(observation.raw_text) is not None
        )
        if (
            not structural
            and groups
            and _visual_lines_share_paragraph(groups[-1][-1], observation)
            and all(item.observation_id not in furniture_ids for item in groups[-1])
            and _explicit_section_label_from_text(
                groups[-1][0].raw_text,
                native_heading=False,
            )
            is None
            and _explicit_list_ordinal_from_text(groups[-1][0].raw_text) is None
        ):
            groups[-1].append(observation)
        else:
            groups.append([observation])
    return tuple(_VisualObservationGroup(observations=tuple(group)) for group in groups)


def _visual_lines_share_paragraph(
    previous: VisualTextObservation,
    current: VisualTextObservation,
) -> bool:
    if previous.page_number != current.page_number:
        return False
    if _SENTENCE_TERMINATOR.search(previous.raw_text):
        return False
    previous_height = previous.bbox[3] - previous.bbox[1]
    current_height = current.bbox[3] - current.bbox[1]
    gap = current.bbox[1] - previous.bbox[3]
    if previous_height <= 0.0 or current_height <= 0.0 or gap < -1.0:
        return False
    max_gap = max(
        3.0,
        min(previous_height, current_height) * _VISUAL_LINE_MAX_GAP_RATIO,
    )
    horizontal_overlap = max(
        0.0,
        min(previous.bbox[2], current.bbox[2]) - max(previous.bbox[0], current.bbox[0]),
    )
    minimum_width = min(
        previous.bbox[2] - previous.bbox[0],
        current.bbox[2] - current.bbox[0],
    )
    return (
        gap <= max_gap
        and abs(previous.bbox[0] - current.bbox[0]) <= _VISUAL_LINE_MAX_X_SHIFT
        and minimum_width > 0.0
        and horizontal_overlap / minimum_width >= 0.35
    )


def _is_page_canvas_image(source_node: SourceNode) -> bool:
    if source_node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}:
        return False
    bbox = source_node.native_locator.bbox
    if bbox is None:
        return False
    x0, y0, x1, y1 = bbox
    return x0 <= 2.0 and y0 <= 2.0 and x1 - x0 >= 400.0 and y1 - y0 >= 500.0


def _section_parent(
    explicit_label: str | None,
    *,
    section_stack: list[tuple[str, str]],
    last_section_by_label: dict[str, str],
    root_id: str,
) -> tuple[str, str | None]:
    if explicit_label is None:
        return root_id, None
    outline_parts = _logical_outline_parts(explicit_label)
    if len(outline_parts) == 1:
        # A printed ``N.0`` chapter heading is a root heading, not a child of
        # a fabricated ``N`` node.  Its printed label remains unchanged.
        return root_id, None
    expected_parent_parts = outline_parts[:-1]
    expected_parent_label = ".".join(expected_parent_parts)
    for candidate_label in _outline_label_candidates(expected_parent_parts):
        exact_parent_id = last_section_by_label.get(candidate_label)
        if exact_parent_id is not None:
            return exact_parent_id, None
    ancestor_parts = list(expected_parent_parts)
    for depth in range(len(ancestor_parts) - 1, 0, -1):
        for ancestor_label in _outline_label_candidates(tuple(ancestor_parts[:depth])):
            ancestor_id = last_section_by_label.get(ancestor_label)
            if ancestor_id is not None:
                return ancestor_id, expected_parent_label
    for label, structure_node_id in reversed(section_stack):
        if _logical_outline_parts(label) == expected_parent_parts:
            return structure_node_id, None
    for label, structure_node_id in reversed(section_stack):
        label_parts = _logical_outline_parts(label)
        if outline_parts[: len(label_parts)] == label_parts:
            return structure_node_id, expected_parent_label
    return root_id, expected_parent_label


def _updated_section_stack(
    explicit_label: str | None,
    structure_node_id: str,
    *,
    section_stack: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    if explicit_label is None:
        return []
    explicit_parts = _logical_outline_parts(explicit_label)
    depth = len(explicit_parts)
    retained = [
        (label, node_id)
        for label, node_id in section_stack
        if len(_logical_outline_parts(label)) < depth
        and explicit_parts[: len(_logical_outline_parts(label))]
        == _logical_outline_parts(label)
    ]
    retained.append((explicit_label, structure_node_id))
    return retained


def _logical_outline_parts(label: str) -> tuple[str, ...]:
    parts = tuple(label.split("."))
    if len(parts) == 2 and parts[1] == "0":
        return (parts[0],)
    return parts


def _outline_label_candidates(parts: tuple[str, ...]) -> tuple[str, ...]:
    label = ".".join(parts)
    if len(parts) == 1:
        return label, f"{label}.0"
    return (label,)


def _table_note_applies_to_specs(
    *,
    source_node: SourceNode,
    row_ids: dict[int, str],
) -> tuple[_AppliesToSpec, ...]:
    """Resolve only strongly grounded full-width trailing table notes.

    PDF table detectors often keep a visually merged note row as one cell with
    a full-table bbox plus empty placeholder cells.  SourceIR intentionally
    preserves that native matrix without inventing a merged range.  Here the
    explicit note marker, full-width geometry, trailing position, and
    tolerance-like body values together provide enough evidence to express
    ``applies_to`` without copying or rewriting any source text.
    """

    table = source_node.table_payload
    table_bbox = source_node.native_locator.bbox
    if table is None or table_bbox is None or table.row_count < 3:
        return ()
    note_row_index = table.row_count - 1
    note_row_cells = tuple(
        sorted(
            (cell for cell in table.cells if cell.row == note_row_index),
            key=lambda cell: cell.column,
        )
    )
    nonempty_cells = tuple(cell for cell in note_row_cells if cell.raw_text.strip())
    if len(nonempty_cells) != 1:
        return ()
    note_cell = nonempty_cells[0]
    note_bbox = note_cell.native_locator.bbox
    table_width = table_bbox[2] - table_bbox[0]
    if (
        note_cell.column != 0
        or note_bbox is None
        or table_width <= 0.0
        or (note_bbox[2] - note_bbox[0]) / table_width < 0.98
        or abs(note_bbox[0] - table_bbox[0]) > max(1.0, table_width * 0.01)
        or abs(note_bbox[2] - table_bbox[2]) > max(1.0, table_width * 0.01)
        or _TABLE_NOTE_PREFIX.search(note_cell.raw_text) is None
    ):
        return ()

    target_row_indices = tuple(
        row_index
        for row_index in range(1, note_row_index)
        if _table_row_has_parameter_values(table.cells, row_index)
    )
    if not target_row_indices:
        return ()
    note_row_id = row_ids.get(note_row_index)
    if note_row_id is None:
        return ()
    note_ref = _cell_ref(note_cell.cell_id)
    return tuple(
        _AppliesToSpec(
            source_structure_node_id=note_row_id,
            target_structure_node_id=row_ids[row_index],
            source_refs=(note_ref,),
        )
        for row_index in target_row_indices
        if row_index in row_ids
    )


def _table_row_has_parameter_values(
    cells: tuple[TableCellIR, ...],
    row_index: int,
) -> bool:
    values = tuple(
        cell.raw_text.strip()
        for cell in cells
        if cell.row == row_index and cell.column > 0 and cell.raw_text.strip()
    )
    return bool(values) and any(
        _TABLE_PARAMETER_VALUE_PATTERN.search(value) is not None for value in values
    )


def _explicit_table_cell_peer_group(
    covering_cells: tuple[TableCellIR, ...],
    *,
    row_index: int,
) -> tuple[TableCellIR, tuple[ExplicitNumericPeerItem, ...]] | None:
    """Return one unambiguous native-cell peer partition for a logical row."""

    candidates: list[tuple[TableCellIR, tuple[ExplicitNumericPeerItem, ...]]] = []
    for cell in covering_cells:
        if cell.row != row_index or cell.row_span != 1 or cell.column_span != 1:
            continue
        items = parse_explicit_numeric_peer_items(cell.raw_text)
        if items:
            candidates.append((cell, items))
    return candidates[0] if len(candidates) == 1 else None


def _folded_table_cell_nodes(
    snapshot: SourceIRSnapshot,
) -> dict[tuple[str, int], tuple[SourceNode, ...]]:
    """Collect fidelity views already represented by a TableIR logical row.

    DOCX preserves paragraphs and their image placements inside table cells as
    SourceNodes in parallel to TableCellIR. They are additional source
    grounding for the row, not sibling content blocks. The shape check is
    format-neutral and relies on a table ancestor plus native cell coordinates.
    """

    nodes_by_id = {node.source_node_id: node for node in snapshot.nodes}
    collected: dict[tuple[str, int], list[SourceNode]] = {}
    for node in snapshot.nodes:
        locator = node.native_locator
        if (
            locator.row_index is None
            or locator.column_index is None
            or locator.paragraph_index is None
        ):
            continue
        ancestor_id = node.parent_source_node_id
        table_parent: SourceNode | None = None
        while ancestor_id is not None:
            ancestor = nodes_by_id[ancestor_id]
            if ancestor.kind is SourceNodeKind.TABLE:
                table_parent = ancestor
                break
            ancestor_id = ancestor.parent_source_node_id
        if table_parent is None:
            continue
        table = table_parent.table_payload
        if (
            table is None
            or locator.row_index >= table.row_count
            or locator.column_index >= table.column_count
        ):
            raise StructureResolutionRejected(
                StructureResolutionRejectionCode.INVALID_TABLE_GROUNDING,
                "A table-cell SourceNode points outside its parent TableIR.",
            )
        collected.setdefault(
            (table_parent.source_node_id, locator.row_index),
            [],
        ).append(node)
    return {
        key: tuple(
            sorted(nodes, key=lambda node: (node.source_order, node.source_node_id))
        )
        for key, nodes in collected.items()
    }


def _table_row_semantic_source_refs(
    *,
    covering_cells: tuple[TableCellIR, ...],
    folded_cell_nodes: tuple[SourceNode, ...],
    pdf_text_selections: tuple[_PdfTableTextSelection, ...],
) -> tuple[StructureSourceRef, ...]:
    """Keep topology while exposing exactly one native PDF text channel."""

    cell_positions = {
        cell.cell_id: position for position, cell in enumerate(covering_cells)
    }
    selected_cell_ids = {
        cell_id
        for selection in pdf_text_selections
        for cell_id in selection.replaced_cell_ids
    }
    selections_by_position: dict[int, list[_PdfTableTextSelection]] = {}
    for selection in pdf_text_selections:
        positions = tuple(
            cell_positions[cell_id]
            for cell_id in selection.replaced_cell_ids
            if cell_id in cell_positions
        )
        if not positions:
            continue
        selections_by_position.setdefault(min(positions), []).append(selection)

    refs: list[StructureSourceRef] = []
    for position, cell in enumerate(covering_cells):
        refs.extend(
            _node_ref(selection.source_node.source_node_id)
            for selection in sorted(
                selections_by_position.get(position, ()),
                key=lambda item: (
                    item.source_node.source_order,
                    item.source_node.source_node_id,
                ),
            )
        )
        if cell.cell_id not in selected_cell_ids:
            refs.append(_cell_ref(cell.cell_id))
    refs.extend(_node_ref(node.source_node_id) for node in folded_cell_nodes)
    return tuple(dict.fromkeys(refs))


def _pdf_table_text_channel_findings(
    *,
    snapshot: SourceIRSnapshot,
    row_selections: dict[
        tuple[str, int],
        tuple[_PdfTableTextSelection, ...],
    ],
    structure_nodes: tuple[StructureNode, ...],
    resolver_id: str,
    resolver_version: str,
) -> tuple[StructureFinding, ...]:
    cells_by_id = {
        cell.cell_id: cell
        for source_node in snapshot.nodes
        if source_node.table_payload is not None
        for cell in source_node.table_payload.cells
    }
    table_structure_by_source_id = {
        source_ref.source_id: node
        for node in structure_nodes
        if node.kind is StructureNodeKind.TABLE
        for source_ref in node.source_refs
        if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
    }
    rows_by_table_and_index = {
        (node.parent_structure_node_id, node.table_row_index): node
        for node in structure_nodes
        if node.kind is StructureNodeKind.TABLE_ROW
    }
    findings: list[StructureFinding] = []
    for (table_source_node_id, row_index), selections in sorted(
        row_selections.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        selected_cell_ids = tuple(
            dict.fromkeys(
                cell_id
                for selection in selections
                for cell_id in selection.replaced_cell_ids
            )
        )
        source_text_key = _pdf_table_text_key(
            "\n".join(selection.source_node.raw_text for selection in selections)
        )
        cell_text_key = _pdf_table_text_key(
            "\n".join(
                cells_by_id[cell_id].raw_text
                for cell_id in selected_cell_ids
                if cell_id in cells_by_id
            )
        )
        if source_text_key == cell_text_key:
            continue
        table_structure = table_structure_by_source_id.get(table_source_node_id)
        if table_structure is None:
            continue
        target = rows_by_table_and_index.get(
            (table_structure.structure_node_id, row_index)
        )
        if target is None:
            continue
        code = "pdf_table_text_channel_conflict"
        identity_key = (
            "finding:"
            + hashlib.sha256(
                (
                    f"{table_source_node_id}:{row_index}:"
                    + ",".join(
                        selection.source_node.source_node_id for selection in selections
                    )
                    + f":{code}"
                ).encode("utf-8")
            ).hexdigest()
        )
        source_refs = tuple(
            _node_ref(selection.source_node.source_node_id) for selection in selections
        ) + tuple(_cell_ref(cell_id) for cell_id in selected_cell_ids)
        findings.append(
            StructureFinding(
                finding_id=stable_structure_finding_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    code=code,
                    identity_key=identity_key,
                ),
                identity_key=identity_key,
                code=code,
                severity=StructureFindingSeverity.INFO,
                blocking=False,
                message_key=f"structure.{code}",
                structure_node_id=target.structure_node_id,
                source_refs=source_refs,
            )
        )
    return tuple(findings)


def _pdf_table_text_key(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not character.isspace()
    )


def _verified_visual_table_cell_specs(
    snapshot: SourceIRSnapshot,
) -> tuple[
    tuple[_VisualTableCellTextBindingSpec, ...],
    tuple[_VisualTableFactIssue, ...],
]:
    """Admit visual table text only after an all-cell native topology proof."""

    source_nodes = {node.source_node_id: node for node in snapshot.nodes}
    specs: list[_VisualTableCellTextBindingSpec] = []
    issues: list[_VisualTableFactIssue] = []
    verified_table_source_ids: set[str] = set()
    captured_table_source_ids: set[str] = set()
    for visual_table_snapshot in snapshot.visual_table_snapshots:
        captured_table_source_ids.add(visual_table_snapshot.table_source_node_id)
        table_node = source_nodes.get(visual_table_snapshot.table_source_node_id)
        if table_node is None:
            issues.append(
                _VisualTableFactIssue(
                    code="pdf_visual_table_snapshot_binding_unresolved",
                    table_source_node_ids=(visual_table_snapshot.table_source_node_id,),
                )
            )
            continue
        try:
            facts = reconcile_visual_table_snapshot(
                visual_table_snapshot,
                table_node,
            )
        except VisualTableReconciliationRejected as exc:
            issues.append(
                _VisualTableFactIssue(
                    code=(
                        "pdf_visual_table_topology_mismatch"
                        if exc.code.value == "topology_mismatch"
                        else "pdf_visual_table_snapshot_binding_unresolved"
                    ),
                    table_source_node_ids=(table_node.source_node_id,),
                )
            )
            continue
        verified_table_source_ids.add(table_node.source_node_id)
        specs.extend(
            _VisualTableCellTextBindingSpec(
                table_source_node_id=fact.table_source_node_id,
                table_cell_id=fact.native_cell_id,
                visual_table_cell_id=fact.visual_table_cell_id,
            )
            for fact in facts
        )
    authorized_visual_table_pages = set(snapshot.visual_table_page_numbers)
    for table_node in snapshot.nodes:
        locator = table_node.native_locator
        if (
            table_node.kind is not SourceNodeKind.TABLE
            or locator.format is not NativeDocumentFormat.PDF
            or locator.page_number not in authorized_visual_table_pages
            or table_node.table_payload is None
        ):
            continue
        if table_node.source_node_id not in captured_table_source_ids:
            issues.append(
                _VisualTableFactIssue(
                    code="pdf_visual_table_snapshot_missing",
                    table_source_node_ids=(table_node.source_node_id,),
                )
            )
            continue
        if table_node.source_node_id in verified_table_source_ids and (
            locator.bbox is None
            or any(
                cell.native_locator.bbox is None
                for cell in table_node.table_payload.cells
            )
        ):
            issues.append(
                _VisualTableFactIssue(
                    code="pdf_visual_table_geometry_unavailable",
                    table_source_node_ids=(table_node.source_node_id,),
                    blocking=False,
                )
            )
    bound_cell_ids_by_table: dict[str, set[str]] = {}
    for spec in specs:
        bound_cell_ids_by_table.setdefault(spec.table_source_node_id, set()).add(
            spec.table_cell_id
        )
    authority_pages = visual_text_authority_pages(snapshot)
    for table_node in snapshot.nodes:
        table = table_node.table_payload
        if (
            table is None
            or table_node.kind is not SourceNodeKind.TABLE
            or table_node.native_locator.format is not NativeDocumentFormat.PDF
            or table_node.native_locator.page_number not in authority_pages
        ):
            continue
        missing_cell_ids = tuple(
            sorted(
                cell.cell_id
                for cell in table.cells
                if cell.cell_id
                not in bound_cell_ids_by_table.get(table_node.source_node_id, set())
            )
        )
        if missing_cell_ids:
            issues.append(
                _VisualTableFactIssue(
                    code="pdf_ocr_authority_table_text_unresolved",
                    table_source_node_ids=(table_node.source_node_id,),
                    table_cell_ids=missing_cell_ids,
                )
            )
    return (
        tuple(
            sorted(
                specs,
                key=lambda item: (
                    item.table_source_node_id,
                    item.table_cell_id,
                    item.visual_table_cell_id,
                ),
            )
        ),
        tuple(issues),
    )


def _visual_table_cell_text_bindings(
    *,
    specs: tuple[_VisualTableCellTextBindingSpec, ...],
    structure_nodes: tuple[StructureNode, ...],
) -> tuple[VisualTableCellTextBinding, ...]:
    structure_by_source_id = {
        ref.source_id: node
        for node in structure_nodes
        if node.kind is StructureNodeKind.TABLE
        for ref in node.source_refs
        if ref.kind is StructureSourceRefKind.SOURCE_NODE
    }
    bindings = [
        VisualTableCellTextBinding(
            table_structure_node_id=structure_by_source_id[
                spec.table_source_node_id
            ].structure_node_id,
            table_cell_id=spec.table_cell_id,
            visual_table_cell_id=spec.visual_table_cell_id,
        )
        for spec in specs
        if spec.table_source_node_id in structure_by_source_id
    ]
    return tuple(
        sorted(
            bindings,
            key=lambda item: (
                item.table_structure_node_id,
                item.table_cell_id,
            ),
        )
    )


def _visual_table_fact_findings(
    *,
    snapshot: SourceIRSnapshot,
    issues: tuple[_VisualTableFactIssue, ...],
    structure_nodes: tuple[StructureNode, ...],
    resolver_id: str,
    resolver_version: str,
) -> tuple[StructureFinding, ...]:
    structure_by_source_id = {
        ref.source_id: node
        for node in structure_nodes
        if node.kind is StructureNodeKind.TABLE
        for ref in node.source_refs
        if ref.kind is StructureSourceRefKind.SOURCE_NODE
    }
    findings: list[StructureFinding] = []
    for issue in issues:
        identity_payload = ":".join(
            (
                issue.code,
                *issue.table_source_node_ids,
                *issue.table_cell_ids,
            )
        )
        identity_key = (
            "finding:" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
        )
        source_refs = tuple(
            _node_ref(source_id) for source_id in issue.table_source_node_ids
        ) + tuple(_cell_ref(cell_id) for cell_id in issue.table_cell_ids)
        target_structure_node_id = (
            structure_by_source_id[issue.table_source_node_ids[0]].structure_node_id
            if len(issue.table_source_node_ids) == 1
            and issue.table_source_node_ids[0] in structure_by_source_id
            else None
        )
        findings.append(
            StructureFinding(
                finding_id=stable_structure_finding_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    code=issue.code,
                    identity_key=identity_key,
                ),
                identity_key=identity_key,
                code=issue.code,
                severity=(
                    StructureFindingSeverity.ERROR
                    if issue.blocking
                    else StructureFindingSeverity.WARNING
                ),
                blocking=issue.blocking,
                message_key=f"structure.{issue.code}",
                structure_node_id=target_structure_node_id,
                source_refs=source_refs,
            )
        )
    return tuple(findings)


def _merge_folded_table_row_nodes(
    *mappings: dict[tuple[str, int], tuple[SourceNode, ...]],
) -> dict[tuple[str, int], tuple[SourceNode, ...]]:
    merged: dict[tuple[str, int], list[SourceNode]] = {}
    for mapping in mappings:
        for key, nodes in mapping.items():
            merged.setdefault(key, []).extend(nodes)
    return {
        key: tuple(
            sorted(
                {node.source_node_id: node for node in nodes}.values(),
                key=lambda item: (item.source_order, item.source_node_id),
            )
        )
        for key, nodes in merged.items()
    }


def _reconcile_visual_page_tables(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: list[StructureNode],
) -> list[StructureNode]:
    """Attach native table facts to the preceding visual outline boundary."""

    if not snapshot.visual_text_snapshots:
        return structure_nodes
    source_nodes = {node.source_node_id: node for node in snapshot.nodes}
    observations = {
        observation.observation_id: observation
        for visual_snapshot in snapshot.visual_text_snapshots
        for observation in visual_snapshot.observations
    }
    anchors: list[tuple[tuple[int, float, float, int, str], str]] = []
    for node in structure_nodes:
        if node.kind not in {StructureNodeKind.SECTION, StructureNodeKind.CLAUSE}:
            continue
        visual_refs = tuple(
            observations[ref.source_id]
            for ref in node.source_refs
            if ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
            and ref.source_id in observations
        )
        if not visual_refs:
            continue
        first = min(
            visual_refs,
            key=lambda item: (
                item.page_number,
                item.bbox[1],
                item.bbox[0],
                item.observation_index,
                item.observation_id,
            ),
        )
        anchors.append(
            (
                (
                    first.page_number,
                    first.bbox[1],
                    first.bbox[0],
                    first.observation_index,
                    first.observation_id,
                ),
                node.structure_node_id,
            )
        )
    anchors.sort()
    if not anchors:
        return structure_nodes

    visual_pages = {
        visual_snapshot.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
    }
    replacements: dict[str, StructureNode] = {}
    for node in structure_nodes:
        if node.kind is not StructureNodeKind.TABLE:
            continue
        source_table = next(
            (
                source_nodes[ref.source_id]
                for ref in node.source_refs
                if ref.kind is StructureSourceRefKind.SOURCE_NODE
                and ref.source_id in source_nodes
                and source_nodes[ref.source_id].kind is SourceNodeKind.TABLE
            ),
            None,
        )
        if source_table is None:
            continue
        locator = source_table.native_locator
        if locator.page_number not in visual_pages or locator.bbox is None:
            continue
        table_key = (
            locator.page_number,
            locator.bbox[1],
            locator.bbox[0],
            1_000_000_000,
            source_table.source_node_id,
        )
        preceding = tuple(item for item in anchors if item[0] < table_key)
        if not preceding:
            continue
        parent_id = preceding[-1][1]
        if node.parent_structure_node_id != parent_id:
            replacements[node.structure_node_id] = node.model_copy(
                update={"parent_structure_node_id": parent_id}
            )
    if not replacements:
        return structure_nodes
    return [replacements.get(node.structure_node_id, node) for node in structure_nodes]


def _reconcile_document_region_tables(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: list[StructureNode],
    document_region_heading_source_ids: set[str],
) -> tuple[list[StructureNode], frozenset[str]]:
    """Move a page-layout table under the region heading it physically owns.

    PDF table detectors sometimes wrap an entire attachment page in one grid.
    The table source is emitted before an overlaid title block, so a one-pass
    outline resolver initially sees the table as content of the preceding
    region.  A source-bound region title contained by that same grid is
    stronger ownership evidence.  Reparenting the table preserves every cell
    and row while preventing the preceding long section from swallowing the
    next attachment.
    """

    if not document_region_heading_source_ids:
        return structure_nodes, frozenset()
    source_nodes = {node.source_node_id: node for node in snapshot.nodes}
    region_structure_by_source_id = {
        ref.source_id: node
        for node in structure_nodes
        if node.kind is StructureNodeKind.SECTION
        for ref in node.source_refs
        if ref.kind is StructureSourceRefKind.SOURCE_NODE
        and ref.source_id in document_region_heading_source_ids
    }
    if not region_structure_by_source_id:
        return structure_nodes, frozenset()

    region_candidates_by_page: dict[
        int,
        list[tuple[SourceNode, StructureNode]],
    ] = {}
    for source_id, structure_node in region_structure_by_source_id.items():
        source_node = source_nodes.get(source_id)
        if (
            source_node is None
            or source_node.native_locator.page_number is None
            or source_node.native_locator.bbox is None
        ):
            continue
        region_candidates_by_page.setdefault(
            source_node.native_locator.page_number,
            [],
        ).append((source_node, structure_node))

    replacements: dict[str, StructureNode] = {}
    for structure_node in structure_nodes:
        if structure_node.kind is not StructureNodeKind.TABLE:
            continue
        table_source = next(
            (
                source_nodes[ref.source_id]
                for ref in structure_node.source_refs
                if ref.kind is StructureSourceRefKind.SOURCE_NODE
                and ref.source_id in source_nodes
                and source_nodes[ref.source_id].kind is SourceNodeKind.TABLE
            ),
            None,
        )
        if (
            table_source is None
            or table_source.native_locator.page_number is None
            or table_source.native_locator.bbox is None
        ):
            continue
        candidates = tuple(
            (source_node, region_node)
            for source_node, region_node in region_candidates_by_page.get(
                table_source.native_locator.page_number,
                (),
            )
            if source_node.native_locator.bbox is not None
            and _bbox_coverage(
                source_node.native_locator.bbox,
                table_source.native_locator.bbox,
            )
            >= _PDF_TABLE_TEXT_MIN_COVERAGE
        )
        if not candidates:
            continue
        _, owner = min(
            candidates,
            key=lambda item: (
                item[0].native_locator.bbox[1],
                item[0].source_order,
                item[0].source_node_id,
            ),
        )
        if structure_node.parent_structure_node_id != owner.structure_node_id:
            replacements[structure_node.structure_node_id] = structure_node.model_copy(
                update={"parent_structure_node_id": owner.structure_node_id}
            )
    if not replacements:
        return structure_nodes, frozenset()
    return (
        [replacements.get(node.structure_node_id, node) for node in structure_nodes],
        frozenset(replacements),
    )


def _stable_parent_first_reading_order(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: list[StructureNode],
) -> list[StructureNode]:
    """Renumber a reconciled graph in grounded reading order, parent first."""

    reading_keys = _structure_reading_keys(snapshot, structure_nodes)
    children: dict[str | None, list[StructureNode]] = {}
    for node in structure_nodes:
        children.setdefault(node.parent_structure_node_id, []).append(node)
    for siblings in children.values():
        siblings.sort(
            key=lambda item: (
                reading_keys[item.structure_node_id],
                item.source_order,
                item.structure_node_id,
            )
        )
    roots = children.get(None, ())
    if len(roots) != 1:
        return structure_nodes
    ordered: list[StructureNode] = []

    def append_subtree(node: StructureNode) -> None:
        ordered.append(node)
        for child in children.get(node.structure_node_id, ()):
            append_subtree(child)

    append_subtree(roots[0])
    if len(ordered) != len(structure_nodes):
        return structure_nodes
    return [
        node.model_copy(update={"source_order": source_order})
        for source_order, node in enumerate(ordered)
    ]


def _structure_reading_keys(
    snapshot: SourceIRSnapshot,
    structure_nodes: list[StructureNode],
) -> dict[str, tuple[int, float, float, int, str]]:
    source_node_keys = {
        node.source_node_id: _locator_reading_key(
            node.native_locator.page_number,
            node.native_locator.bbox,
            node.source_order,
            node.source_node_id,
        )
        for node in snapshot.nodes
    }
    source_cell_keys = {
        cell.cell_id: _locator_reading_key(
            cell.native_locator.page_number,
            cell.native_locator.bbox,
            cell.row * 10_000 + cell.column,
            cell.cell_id,
        )
        for node in snapshot.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
    }
    visual_keys = {
        observation.observation_id: _locator_reading_key(
            observation.page_number,
            observation.bbox,
            observation.observation_index,
            observation.observation_id,
        )
        for visual_snapshot in snapshot.visual_text_snapshots
        for observation in visual_snapshot.observations
    }

    result: dict[str, tuple[int, float, float, int, str]] = {}
    for node in structure_nodes:
        if node.kind is StructureNodeKind.DOCUMENT:
            result[node.structure_node_id] = (-1, -1.0, -1.0, -1, "document")
            continue
        keys: list[tuple[int, float, float, int, str]] = []
        for ref in node.source_refs:
            if ref.kind is StructureSourceRefKind.SOURCE_NODE:
                key = source_node_keys.get(ref.source_id)
            elif ref.kind is StructureSourceRefKind.TABLE_CELL:
                key = source_cell_keys.get(ref.source_id)
            else:
                key = visual_keys.get(ref.source_id)
            if key is not None:
                keys.append(key)
        result[node.structure_node_id] = (
            min(keys)
            if keys
            else (
                1_000_000_000,
                float("inf"),
                float("inf"),
                node.source_order,
                node.structure_node_id,
            )
        )
    return result


def _locator_reading_key(
    page_number: int | None,
    bbox: tuple[float, float, float, float] | None,
    native_order: int,
    identity: str,
) -> tuple[int, float, float, int, str]:
    return (
        page_number if page_number is not None else 1_000_000_000,
        bbox[1] if bbox is not None else float("inf"),
        bbox[0] if bbox is not None else float("inf"),
        native_order,
        identity,
    )


def _pdf_image_cell_placement_specs(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    resolver_id: str,
    resolver_version: str,
) -> tuple[tuple[_CellPlacementSpec, ...], tuple[StructureFinding, ...]]:
    """Relate one PDF image placement to one proven table-cell owner set.

    PDF image and table observations are parallel SourceIR facts, so this
    resolver must not rewrite native locators or fabricate a SourceIR parent.
    An image remains an independently locatable structure node.  A placement
    relation is added only when a visibly sized image has one stable asset and
    one uniquely dominant table.  One-cell ownership retains the strict or
    bounded-rounding policy, including cells that also contain independently
    grounded native text.  A genuinely multi-cell opaque image must cover a
    balanced, complete, connected native-cell region.  An alpha composite must
    instead carry exact bounded non-zero-alpha RLE; transparent margins never
    vote for a cell.  Missing or ambiguous proof produces a blocking finding
    and is never resolved by nearest/center heuristics.
    """

    structure_by_identity = {node.identity_key: node for node in structure_nodes}
    assets_by_id = {asset.asset_id: asset for asset in snapshot.assets}
    tables_by_page: dict[int, list[SourceNode]] = {}
    for node in snapshot.nodes:
        locator = node.native_locator
        if (
            node.kind is SourceNodeKind.TABLE
            and locator.format is NativeDocumentFormat.PDF
            and locator.page_number is not None
            and node.table_payload is not None
        ):
            tables_by_page.setdefault(locator.page_number, []).append(node)

    specs: list[_CellPlacementSpec] = []
    findings: list[StructureFinding] = []
    for image_node in snapshot.nodes:
        locator = image_node.native_locator
        if (
            image_node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            or locator.format is not NativeDocumentFormat.PDF
            or locator.page_number is None
            or locator.bbox is None
            or image_node.parent_source_node_id is not None
            or _is_page_canvas_image(image_node)
            or _is_pdf_image_furniture_node(image_node)
            or (
                locator.bbox[2] - locator.bbox[0] < _PDF_IMAGE_CELL_MIN_DIMENSION_POINTS
            )
            or (
                locator.bbox[3] - locator.bbox[1] < _PDF_IMAGE_CELL_MIN_DIMENSION_POINTS
            )
        ):
            continue
        image_structure = structure_by_identity.get(
            f"source:{image_node.source_node_id}"
        )
        if image_structure is None:
            continue

        stable_asset_binding = _has_stable_pdf_image_asset_binding(
            image_node,
            assets_by_id=assets_by_id,
        )
        page_tables = tables_by_page.get(locator.page_number, ())
        measurable_tables = tuple(
            table_node
            for table_node in page_tables
            if table_node.native_locator.bbox is not None
        )
        table_coverages = {
            table_node.source_node_id: _bbox_coverage(
                locator.bbox,
                table_node.native_locator.bbox,
            )
            for table_node in measurable_tables
        }
        strong_tables = tuple(
            table_node
            for table_node in measurable_tables
            if table_coverages[table_node.source_node_id]
            >= _PDF_IMAGE_CELL_MIN_COVERAGE
        )
        if not strong_tables:
            continue
        competing_tables = tuple(
            table_node
            for table_node in measurable_tables
            if table_coverages[table_node.source_node_id]
            > _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
        )
        target_table = strong_tables[0] if len(strong_tables) == 1 else None
        target_cell: TableCellIR | None = None
        target_cell_match: _PdfImageCellPlacementMatch | None = None
        target_multi_cell_match: _PdfImageMultiCellPlacementMatch | None = None
        if (
            target_table is not None
            and len(competing_tables) == 1
            and len(measurable_tables) == len(page_tables)
        ):
            table = target_table.table_payload
            if table is not None and all(
                cell.native_locator.bbox is not None for cell in table.cells
            ):
                target_cell_match = _unique_pdf_image_table_cell(
                    image_bbox=locator.bbox,
                    cells=table.cells,
                )
                if target_cell_match is not None:
                    target_cell = target_cell_match.cell
                else:
                    target_multi_cell_match = _pdf_image_table_cells(
                        image_node=image_node,
                        image_bbox=locator.bbox,
                        cells=table.cells,
                    )

        if (
            stable_asset_binding
            and target_table is not None
            and (target_cell is not None or target_multi_cell_match is not None)
        ):
            table_structure = structure_by_identity.get(
                f"source:{target_table.source_node_id}"
            )
            if table_structure is None:
                continue
            if target_cell is not None:
                placement_kind = StructureRelationKind.PLACED_IN_CELL
                cell_refs = (_cell_ref(target_cell.cell_id),)
            else:
                assert target_multi_cell_match is not None
                placement_kind = StructureRelationKind.PLACED_OVER_CELLS
                cell_refs = tuple(
                    _cell_ref(cell.cell_id) for cell in target_multi_cell_match.cells
                )
            specs.append(
                _CellPlacementSpec(
                    kind=placement_kind,
                    source_structure_node_id=image_structure.structure_node_id,
                    target_structure_node_id=table_structure.structure_node_id,
                    source_refs=(
                        _node_ref(image_node.source_node_id),
                        *cell_refs,
                    ),
                )
            )
            continue

        missing_alpha_footprint = bool(
            image_node.asset_derivation is not None
            and image_node.asset_derivation.kind
            is SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE
            and image_node.asset_derivation.alpha_footprint is None
        )
        finding_code = (
            "pdf_image_table_cell_alpha_footprint_unavailable"
            if missing_alpha_footprint
            else "pdf_image_table_cell_placement_unresolved"
        )
        finding_identity = (
            "finding:"
            + hashlib.sha256(
                f"{image_node.source_node_id}:{finding_code}".encode("utf-8")
            ).hexdigest()
        )
        findings.append(
            StructureFinding(
                finding_id=stable_structure_finding_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    code=finding_code,
                    identity_key=finding_identity,
                ),
                identity_key=finding_identity,
                code=finding_code,
                severity=StructureFindingSeverity.ERROR,
                blocking=True,
                message_key=f"structure.{finding_code}",
                structure_node_id=image_structure.structure_node_id,
                source_refs=(_node_ref(image_node.source_node_id),),
            )
        )

    return (
        tuple(
            sorted(
                specs,
                key=lambda spec: (
                    spec.kind.value,
                    spec.source_structure_node_id,
                    spec.target_structure_node_id,
                    tuple(ref.source_id for ref in spec.source_refs),
                ),
            )
        ),
        tuple(
            sorted(
                findings,
                key=lambda finding: (finding.identity_key, finding.finding_id),
            )
        ),
    )


def _has_stable_pdf_image_asset_binding(
    image_node: SourceNode,
    *,
    assets_by_id: dict[str, SourceAssetRef],
) -> bool:
    asset_ref = image_node.asset_ref
    asset = assets_by_id.get(asset_ref or "")
    if asset_ref is None or asset is None:
        return False
    expected_kind = (
        SourceAssetKind.FIGURE
        if image_node.kind is SourceNodeKind.FIGURE
        else SourceAssetKind.IMAGE
    )
    if (
        asset.kind is not expected_kind
        or not asset.media_type.startswith("image/")
        or re.fullmatch(r"[0-9a-f]{64}", asset.content_sha256) is None
    ):
        return False
    if (
        image_node.visual_locator is not None
        and image_node.visual_locator != image_node.native_locator
    ) or (
        asset.native_locator is not None
        and asset.native_locator != image_node.native_locator
    ):
        return False
    return not any(
        diagnostic.severity is SourceDiagnosticSeverity.ERROR
        for diagnostic in image_node.extraction_diagnostics
    )


def _pdf_image_table_cells(
    *,
    image_node: SourceNode,
    image_bbox: tuple[float, float, float, float],
    cells: tuple[TableCellIR, ...],
) -> _PdfImageMultiCellPlacementMatch | None:
    derivation = image_node.asset_derivation
    if derivation is not None:
        if derivation.kind is not SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE:
            return None
        footprint = derivation.alpha_footprint
        if footprint is None:
            return None
        matched_cells = _pdf_alpha_footprint_cells(
            image_bbox=image_bbox,
            cells=cells,
            width_px=footprint.width_px,
            height_px=footprint.height_px,
            runs=tuple((run.y, run.x_start, run.x_end) for run in footprint.runs),
            expected_nonzero_sample_count=footprint.nonzero_sample_count,
        )
    else:
        matched_cells = _pdf_opaque_image_cells(
            image_bbox=image_bbox,
            cells=cells,
        )
    if (
        matched_cells is None
        or len(matched_cells) < 2
        or not _pdf_cells_form_bounded_connected_native_region(matched_cells)
    ):
        return None
    return _PdfImageMultiCellPlacementMatch(cells=matched_cells)


def _pdf_opaque_image_cells(
    *,
    image_bbox: tuple[float, float, float, float],
    cells: tuple[TableCellIR, ...],
) -> tuple[TableCellIR, ...] | None:
    """Resolve only balanced, complete area ownership for an opaque image."""

    coverages = {
        cell.cell_id: _bbox_coverage(image_bbox, cell.native_locator.bbox)
        for cell in cells
    }
    materially_overlapped = tuple(
        cell
        for cell in cells
        if coverages[cell.cell_id] > _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
    )
    if (
        len(materially_overlapped) < 2
        or len(materially_overlapped) > _PDF_IMAGE_CELL_MAX_MULTI_CELL_COUNT
        or any(
            coverages[cell.cell_id] < _PDF_IMAGE_CELL_MIN_OPAQUE_MULTI_CELL_COVERAGE
            for cell in materially_overlapped
        )
    ):
        return None
    covered_area_ratio = sum(coverages[cell.cell_id] for cell in materially_overlapped)
    if not (
        _PDF_IMAGE_CELL_MIN_COVERAGE
        <= covered_area_ratio
        <= 1.0 + _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
    ):
        return None
    return tuple(
        sorted(
            materially_overlapped,
            key=lambda cell: (cell.row, cell.column, cell.cell_id),
        )
    )


def _pdf_alpha_footprint_cells(
    *,
    image_bbox: tuple[float, float, float, float],
    cells: tuple[TableCellIR, ...],
    width_px: int,
    height_px: int,
    runs: tuple[tuple[int, int, int], ...],
    expected_nonzero_sample_count: int,
) -> tuple[TableCellIR, ...] | None:
    """Map every non-zero-alpha pixel centre to exactly one native table cell."""

    x0, y0, x1, y1 = image_bbox
    if width_px < 1 or height_px < 1 or x1 <= x0 or y1 <= y0 or not runs:
        return None
    pixel_rectangles: list[tuple[TableCellIR, int, int, int, int]] = []
    for cell in cells:
        cell_bbox = cell.native_locator.bbox
        if cell_bbox is None:
            return None
        cell_x0, cell_y0, cell_x1, cell_y1 = cell_bbox
        pixel_x0, pixel_x1 = _pdf_pixel_center_interval(
            image_start=x0,
            image_end=x1,
            pixel_count=width_px,
            container_start=cell_x0,
            container_end=cell_x1,
        )
        pixel_y0, pixel_y1 = _pdf_pixel_center_interval(
            image_start=y0,
            image_end=y1,
            pixel_count=height_px,
            container_start=cell_y0,
            container_end=cell_y1,
        )
        if pixel_x1 > pixel_x0 and pixel_y1 > pixel_y0:
            pixel_rectangles.append((cell, pixel_x0, pixel_x1, pixel_y0, pixel_y1))

    sample_counts: dict[str, int] = {}
    observed_sample_count = 0
    for run_y, run_x0, run_x1 in runs:
        intervals = [
            (
                max(run_x0, cell_x0),
                min(run_x1, cell_x1),
                cell,
            )
            for cell, cell_x0, cell_x1, cell_y0, cell_y1 in pixel_rectangles
            if cell_y0 <= run_y < cell_y1
            and min(run_x1, cell_x1) > max(run_x0, cell_x0)
        ]
        intervals.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2].row,
                item[2].column,
                item[2].cell_id,
            )
        )
        cursor = run_x0
        for interval_x0, interval_x1, cell in intervals:
            if interval_x0 != cursor:
                return None
            sample_count = interval_x1 - interval_x0
            sample_counts[cell.cell_id] = (
                sample_counts.get(cell.cell_id, 0) + sample_count
            )
            observed_sample_count += sample_count
            cursor = interval_x1
        if cursor != run_x1:
            return None
    if observed_sample_count != expected_nonzero_sample_count:
        return None
    cells_by_id = {cell.cell_id: cell for cell in cells}
    return tuple(
        sorted(
            (cells_by_id[cell_id] for cell_id in sample_counts),
            key=lambda cell: (cell.row, cell.column, cell.cell_id),
        )
    )


def _pdf_pixel_center_interval(
    *,
    image_start: float,
    image_end: float,
    pixel_count: int,
    container_start: float,
    container_end: float,
) -> tuple[int, int]:
    if pixel_count < 1 or image_end <= image_start or container_end <= container_start:
        return (0, 0)
    scale = (image_end - image_start) / pixel_count
    epsilon = 1e-9
    first = math.ceil((container_start - image_start) / scale - 0.5 - epsilon)
    end = math.ceil((container_end - image_start) / scale - 0.5 - epsilon)
    return (
        max(0, min(pixel_count, first)),
        max(0, min(pixel_count, end)),
    )


def _pdf_cells_form_bounded_connected_native_region(
    cells: tuple[TableCellIR, ...],
) -> bool:
    if (
        len(cells) < 2
        or len(cells) > _PDF_IMAGE_CELL_MAX_MULTI_CELL_COUNT
        or any(cell.native_locator.bbox is None for cell in cells)
    ):
        return False
    covered_slots = {
        (row, column)
        for cell in cells
        for row in range(cell.row, cell.row + cell.row_span)
        for column in range(cell.column, cell.column + cell.column_span)
    }
    if len(covered_slots) > _PDF_IMAGE_CELL_MAX_MULTI_CELL_GRID_SLOTS:
        return False
    reachable = {cells[0].cell_id}
    changed = True
    while changed:
        changed = False
        for left in cells:
            if left.cell_id not in reachable:
                continue
            for right in cells:
                if right.cell_id in reachable:
                    continue
                if _pdf_cells_share_native_boundary(left, right):
                    reachable.add(right.cell_id)
                    changed = True
    return len(reachable) == len(cells)


def _pdf_cells_share_native_boundary(
    left: TableCellIR,
    right: TableCellIR,
) -> bool:
    left_slots = {
        (row, column)
        for row in range(left.row, left.row + left.row_span)
        for column in range(left.column, left.column + left.column_span)
    }
    right_slots = {
        (row, column)
        for row in range(right.row, right.row + right.row_span)
        for column in range(right.column, right.column + right.column_span)
    }
    grid_adjacent = any(
        abs(left_row - right_row) + abs(left_column - right_column) == 1
        for left_row, left_column in left_slots
        for right_row, right_column in right_slots
    )
    if not grid_adjacent:
        return False
    left_bbox = left.native_locator.bbox
    right_bbox = right.native_locator.bbox
    if left_bbox is None or right_bbox is None:
        return False
    lx0, ly0, lx1, ly1 = left_bbox
    rx0, ry0, rx1, ry1 = right_bbox
    horizontal_shared = (
        min(abs(lx1 - rx0), abs(rx1 - lx0))
        <= _PDF_IMAGE_CELL_NATIVE_BOUNDARY_TOLERANCE_POINTS
        and min(ly1, ry1) - max(ly0, ry0) >= _PDF_IMAGE_CELL_MIN_SHARED_BOUNDARY_POINTS
    )
    vertical_shared = (
        min(abs(ly1 - ry0), abs(ry1 - ly0))
        <= _PDF_IMAGE_CELL_NATIVE_BOUNDARY_TOLERANCE_POINTS
        and min(lx1, rx1) - max(lx0, rx0) >= _PDF_IMAGE_CELL_MIN_SHARED_BOUNDARY_POINTS
    )
    return horizontal_shared or vertical_shared


def _unique_pdf_image_table_cell(
    *,
    image_bbox: tuple[float, float, float, float],
    cells: tuple[TableCellIR, ...],
) -> _PdfImageCellPlacementMatch | None:
    cell_coverages = {
        cell.cell_id: _bbox_coverage(
            image_bbox,
            cell.native_locator.bbox,
        )
        for cell in cells
    }
    strict_cells = tuple(
        cell
        for cell in cells
        if cell_coverages[cell.cell_id] >= _PDF_IMAGE_CELL_MIN_COVERAGE
    )
    strict_competing_cells = tuple(
        cell
        for cell in cells
        if cell_coverages[cell.cell_id] > _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
    )
    if len(strict_cells) == 1 and strict_competing_cells == strict_cells:
        return _PdfImageCellPlacementMatch(
            cell=strict_cells[0],
            boundary_tolerant=False,
        )

    boundary_tolerant_cells = tuple(
        cell
        for cell in cells
        if cell_coverages[cell.cell_id] >= _PDF_IMAGE_CELL_MIN_DOMINANT_COVERAGE
        and _bbox_coverage(
            image_bbox,
            _expanded_bbox(
                cell.native_locator.bbox,
                margin=_PDF_IMAGE_CELL_BOUNDARY_TOLERANCE_POINTS,
            ),
        )
        >= _PDF_IMAGE_CELL_MIN_COVERAGE
    )
    if len(boundary_tolerant_cells) != 1:
        return None
    target_cell = boundary_tolerant_cells[0]
    if any(
        cell.cell_id != target_cell.cell_id
        and cell_coverages[cell.cell_id]
        > _PDF_IMAGE_CELL_MAX_BOUNDARY_COMPETING_COVERAGE
        for cell in cells
    ):
        return None
    return _PdfImageCellPlacementMatch(
        cell=target_cell,
        boundary_tolerant=True,
    )


def _figure_reference_specs(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
) -> tuple[_ReferenceSpec, ...]:
    """Link an in-place image only when number, order, page, and scope agree."""

    nodes_by_id = {node.structure_node_id: node for node in structure_nodes}
    source_nodes_by_id = {node.source_node_id: node for node in snapshot.nodes}
    page_index = _structure_node_pages(snapshot, structure_nodes)
    text_by_ref = _source_text_by_ref(snapshot)
    image_records: list[tuple[StructureNode, int, str]] = []
    for structure_node in structure_nodes:
        image_sources = tuple(
            source_nodes_by_id[ref.source_id]
            for ref in structure_node.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
            and ref.source_id in source_nodes_by_id
            and source_nodes_by_id[ref.source_id].kind
            in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            and not _is_page_canvas_image(source_nodes_by_id[ref.source_id])
        )
        pages = page_index.get(structure_node.structure_node_id, ())
        section_id = _nearest_section_ancestor_id(structure_node, nodes_by_id)
        if len(image_sources) == 1 and len(pages) == 1 and section_id is not None:
            image_records.append((structure_node, pages[0], section_id))

    captions: list[tuple[str, StructureNode, int, str]] = []
    body_refs: list[tuple[str, StructureNode, int, str]] = []
    for structure_node in structure_nodes:
        if structure_node.kind is StructureNodeKind.DOCUMENT:
            continue
        pages = page_index.get(structure_node.structure_node_id, ())
        section_id = _nearest_section_ancestor_id(structure_node, nodes_by_id)
        if len(pages) != 1 or section_id is None:
            continue
        raw_text = _structure_node_text(structure_node, text_by_ref=text_by_ref)
        if not raw_text:
            continue
        caption_match = _FIGURE_CAPTION_PREFIX.search(raw_text)
        if caption_match is not None:
            captions.append(
                (
                    _canonical_caption_number_label(caption_match.group("label")),
                    structure_node,
                    pages[0],
                    section_id,
                )
            )
            continue
        for match in _FIGURE_BODY_REFERENCE.finditer(raw_text):
            body_refs.append(
                (
                    _canonical_caption_number_label(match.group("label")),
                    structure_node,
                    pages[0],
                    section_id,
                )
            )

    specs: list[_ReferenceSpec] = []
    observed_pairs: set[tuple[str, str]] = set()
    for label, caption, page_number, section_id in captions:
        candidate_images = tuple(
            image
            for image, image_page, image_section in image_records
            if image_page == page_number
            and image_section == section_id
            and image.source_order < caption.source_order
        )
        if len(candidate_images) != 1:
            continue
        image = candidate_images[0]
        matching_body_refs = tuple(
            body
            for body_label, body, body_page, body_section in body_refs
            if body_label == label
            and body_page == page_number
            and body_section == section_id
            and body.source_order < image.source_order
        )
        if not matching_body_refs:
            continue
        body = max(
            matching_body_refs,
            key=lambda node: (node.source_order, node.structure_node_id),
        )
        for source in (body, caption):
            pair = (source.structure_node_id, image.structure_node_id)
            if pair in observed_pairs:
                continue
            observed_pairs.add(pair)
            specs.append(
                _ReferenceSpec(
                    source_structure_node_id=source.structure_node_id,
                    target_structure_node_id=image.structure_node_id,
                    source_refs=_dedupe_source_refs(
                        source.source_refs,
                        image.source_refs,
                    ),
                )
            )
    return tuple(specs)


def _unstructured_visual_requirement_findings(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    reference_specs: tuple[_ReferenceSpec, ...],
    resolver_id: str,
    resolver_version: str,
) -> tuple[StructureFinding, ...]:
    """Flag a source-bound figure whose meaning remains inside the image.

    Two independent references to the same image are required: a numbered body
    reference and its numbered caption. This keeps decorative images and
    unproven page graphics out of the policy while preserving the original
    image as the only source of truth.
    """

    nodes_by_id = {node.structure_node_id: node for node in structure_nodes}
    specs_by_target: dict[str, list[_ReferenceSpec]] = {}
    for spec in reference_specs:
        specs_by_target.setdefault(spec.target_structure_node_id, []).append(spec)

    findings: list[StructureFinding] = []
    for target_id, specs in specs_by_target.items():
        if len({spec.source_structure_node_id for spec in specs}) < 2:
            continue
        target = nodes_by_id.get(target_id)
        if target is None:
            continue
        code = "unstructured_visual_requirement"
        identity_key = (
            "finding:"
            + hashlib.sha256(f"{target_id}:{code}".encode("utf-8")).hexdigest()
        )
        findings.append(
            StructureFinding(
                finding_id=stable_structure_finding_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    code=code,
                    identity_key=identity_key,
                ),
                identity_key=identity_key,
                code=code,
                severity=StructureFindingSeverity.WARNING,
                blocking=False,
                message_key="structure.unstructured_visual_requirement",
                structure_node_id=target_id,
                source_refs=_dedupe_source_refs(
                    target.source_refs,
                    *(spec.source_refs for spec in specs),
                ),
            )
        )
    return tuple(
        sorted(
            findings,
            key=lambda finding: (finding.identity_key, finding.finding_id),
        )
    )


def _external_reference_content_findings(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    resolver_id: str,
    resolver_version: str,
) -> tuple[StructureFinding, ...]:
    """Expose that a listed external standard was not recursively imported."""

    text_by_ref = _source_text_by_ref(snapshot)
    nodes_by_parent: dict[str, list[StructureNode]] = {}
    for node in structure_nodes:
        if node.parent_structure_node_id is not None:
            nodes_by_parent.setdefault(node.parent_structure_node_id, []).append(node)

    findings: list[StructureFinding] = []
    for section in structure_nodes:
        if section.kind is not StructureNodeKind.SECTION:
            continue
        heading_text = _structure_node_text(section, text_by_ref=text_by_ref)
        if _EXTERNAL_REFERENCE_SECTION_SIGNAL.search(heading_text) is None:
            continue
        descendants: list[StructureNode] = []
        pending = list(nodes_by_parent.get(section.structure_node_id, ()))
        while pending:
            child = pending.pop()
            descendants.append(child)
            pending.extend(nodes_by_parent.get(child.structure_node_id, ()))
        reference_rows = tuple(
            node
            for node in descendants
            if node.kind is StructureNodeKind.TABLE_ROW
            and _structure_node_text(node, text_by_ref=text_by_ref).strip()
        )
        if not reference_rows:
            continue
        code = "external_reference_content_not_loaded"
        identity_key = (
            "finding:"
            + hashlib.sha256(
                f"{section.structure_node_id}:{code}".encode("utf-8")
            ).hexdigest()
        )
        findings.append(
            StructureFinding(
                finding_id=stable_structure_finding_id(
                    source_ir_id=snapshot.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    code=code,
                    identity_key=identity_key,
                ),
                identity_key=identity_key,
                code=code,
                severity=StructureFindingSeverity.WARNING,
                blocking=False,
                message_key="structure.external_reference_content_not_loaded",
                structure_node_id=section.structure_node_id,
                source_refs=_dedupe_source_refs(
                    section.source_refs,
                    *(row.source_refs for row in reference_rows),
                ),
            )
        )
    return tuple(
        sorted(
            findings,
            key=lambda finding: (finding.identity_key, finding.finding_id),
        )
    )


def _source_text_by_ref(snapshot: SourceIRSnapshot) -> dict[StructureSourceRef, str]:
    result = {_node_ref(node.source_node_id): node.raw_text for node in snapshot.nodes}
    result.update(
        {
            _cell_ref(cell.cell_id): cell.raw_text
            for node in snapshot.nodes
            if node.table_payload is not None
            for cell in node.table_payload.cells
        }
    )
    result.update(
        {
            _visual_ref(observation.observation_id): observation.raw_text
            for visual_snapshot in snapshot.visual_text_snapshots
            for observation in visual_snapshot.observations
        }
    )
    return result


def _structure_node_text(
    node: StructureNode,
    *,
    text_by_ref: dict[StructureSourceRef, str],
) -> str:
    return "\n".join(
        text for ref in node.source_refs if (text := text_by_ref.get(ref, "")).strip()
    )


def _dedupe_source_refs(
    *groups: tuple[StructureSourceRef, ...],
) -> tuple[StructureSourceRef, ...]:
    result: list[StructureSourceRef] = []
    seen: set[StructureSourceRef] = set()
    for ref in (ref for group in groups for ref in group):
        if ref in seen:
            continue
        seen.add(ref)
        result.append(ref)
    return tuple(result)


def _option_state_specs(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    resolver_id: str,
    resolver_version: str,
) -> tuple[tuple[_OptionStateSpec, ...], tuple[StructureFinding, ...]]:
    """Preserve explicit native option marks without guessing missing states.

    A single bracketed fragment can be a citation or editorial token.  An
    option list therefore requires at least two sibling candidates with the
    same structural parent.  The relation is grounded by the complete option
    node, while an unfamiliar visible mark remains ``uncertain`` and blocks a
    later formal projection until it is reviewed.
    """

    text_by_ref = _source_text_by_ref(snapshot)
    candidates_by_parent: dict[
        str,
        list[tuple[StructureNode, StructureOptionState]],
    ] = {}
    for node in structure_nodes:
        if (
            node.kind not in {StructureNodeKind.CONTENT, StructureNodeKind.CLAUSE}
            or node.parent_structure_node_id is None
        ):
            continue
        raw_text = _structure_node_text(node, text_by_ref=text_by_ref)
        state = _explicit_option_state(raw_text)
        if state is None:
            continue
        candidates_by_parent.setdefault(
            node.parent_structure_node_id,
            [],
        ).append((node, state))

    specs: list[_OptionStateSpec] = []
    findings: list[StructureFinding] = []
    for parent_id, candidates in sorted(candidates_by_parent.items()):
        if len(candidates) < 2:
            continue
        for node, state in sorted(
            candidates,
            key=lambda item: (item[0].source_order, item[0].structure_node_id),
        ):
            specs.append(
                _OptionStateSpec(
                    source_structure_node_id=parent_id,
                    target_structure_node_id=node.structure_node_id,
                    source_refs=node.source_refs,
                    option_state=state,
                )
            )
            if state is not StructureOptionState.UNCERTAIN:
                continue
            code = "uncertain_option_state"
            identity_key = (
                "finding:"
                + hashlib.sha256(
                    f"{node.structure_node_id}:{code}".encode("utf-8")
                ).hexdigest()
            )
            findings.append(
                StructureFinding(
                    finding_id=stable_structure_finding_id(
                        source_ir_id=snapshot.source_ir_id,
                        resolver_id=resolver_id,
                        resolver_version=resolver_version,
                        code=code,
                        identity_key=identity_key,
                    ),
                    identity_key=identity_key,
                    code=code,
                    severity=StructureFindingSeverity.ERROR,
                    blocking=True,
                    message_key="structure.uncertain_option_state",
                    structure_node_id=node.structure_node_id,
                    source_refs=node.source_refs,
                )
            )
    return tuple(specs), tuple(findings)


def _explicit_option_state(raw_text: str) -> StructureOptionState | None:
    match = _SQUARE_OPTION_MARKER.search(raw_text)
    if match is None:
        match = _PAREN_OPTION_MARKER.search(raw_text)
    if match is None:
        match = _BOX_OPTION_MARKER.search(raw_text)
    if match is None:
        return None
    mark = re.sub(r"\s+", "", match.group("mark"))
    if mark in _CHECKED_OPTION_MARKS:
        return StructureOptionState.CHECKED
    if mark in _UNCHECKED_OPTION_MARKS:
        return StructureOptionState.UNCHECKED
    if any(character.isalnum() for character in mark):
        return None
    return StructureOptionState.UNCERTAIN


def _canonical_caption_number_label(raw_label: str) -> str:
    normalized = unicodedata.normalize("NFKC", raw_label)
    return re.sub(r"[ \t]", "", normalized).replace("-", ".")


def _duplicate_table_caption_findings(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    resolver_id: str,
    resolver_version: str,
    existing_findings: tuple[StructureFinding, ...],
) -> tuple[StructureFinding, ...]:
    """Warn on repeated same-page table captions without changing labels."""

    text_by_ref = _source_text_by_ref(snapshot)
    page_index = _structure_node_pages(snapshot, structure_nodes)
    captions: dict[tuple[int, str], list[StructureNode]] = {}
    for node in structure_nodes:
        pages = page_index.get(node.structure_node_id, ())
        if len(pages) != 1:
            continue
        raw_text = _structure_node_text(node, text_by_ref=text_by_ref)
        match = _TABLE_CAPTION_PREFIX.search(raw_text)
        if match is None:
            continue
        label = _canonical_caption_number_label(match.group("label"))
        captions.setdefault((pages[0], label), []).append(node)

    existing = {
        (finding.code, finding.structure_node_id) for finding in existing_findings
    }
    findings: list[StructureFinding] = []
    for (_, _), nodes in sorted(captions.items()):
        ordered = sorted(
            nodes, key=lambda item: (item.source_order, item.structure_node_id)
        )
        for previous, current in zip(ordered, ordered[1:], strict=False):
            code = "duplicate_table_caption_label"
            if (code, current.structure_node_id) in existing:
                continue
            identity_key = (
                "finding:"
                + hashlib.sha256(
                    (
                        f"{previous.structure_node_id}:{current.structure_node_id}:"
                        f"{code}"
                    ).encode("utf-8")
                ).hexdigest()
            )
            findings.append(
                StructureFinding(
                    finding_id=stable_structure_finding_id(
                        source_ir_id=snapshot.source_ir_id,
                        resolver_id=resolver_id,
                        resolver_version=resolver_version,
                        code=code,
                        identity_key=identity_key,
                    ),
                    identity_key=identity_key,
                    code=code,
                    severity=StructureFindingSeverity.WARNING,
                    blocking=False,
                    message_key="structure.duplicate_table_caption_label",
                    structure_node_id=current.structure_node_id,
                    source_refs=_dedupe_source_refs(
                        previous.source_refs,
                        current.source_refs,
                    ),
                )
            )
    return tuple(findings)


def _sequence_findings(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
    resolver_id: str,
    resolver_version: str,
    existing_findings: tuple[StructureFinding, ...],
) -> tuple[StructureFinding, ...]:
    """Report same-page sibling gaps and duplicate list ordinals as warnings."""

    page_index = _structure_node_pages(snapshot, structure_nodes)
    children: dict[str, list[StructureNode]] = {}
    for node in structure_nodes:
        if node.parent_structure_node_id is not None:
            children.setdefault(node.parent_structure_node_id, []).append(node)
    existing = {
        (finding.code, finding.structure_node_id) for finding in existing_findings
    }
    findings: list[StructureFinding] = []
    for parent_id, child_nodes in children.items():
        ordered = sorted(
            child_nodes,
            key=lambda item: (item.source_order, item.structure_node_id),
        )
        sections = [
            node
            for node in ordered
            if node.kind is StructureNodeKind.SECTION
            and node.explicit_label is not None
        ]
        for previous, current in zip(sections, sections[1:], strict=False):
            previous_parts = tuple(
                int(part) for part in previous.explicit_label.split(".")
            )
            current_parts = tuple(
                int(part) for part in current.explicit_label.split(".")
            )
            same_page = len(
                page_index.get(previous.structure_node_id, ())
            ) == 1 and page_index.get(previous.structure_node_id) == page_index.get(
                current.structure_node_id
            )
            if (
                len(previous_parts) < 2
                or len(previous_parts) != len(current_parts)
                or previous_parts[:-1] != current_parts[:-1]
                or current_parts[-1] <= previous_parts[-1] + 1
                or not same_page
                or ("section_numbering_gap", current.structure_node_id) in existing
            ):
                continue
            # A repeated label pair (for example contents and body) is not one
            # finding: bind its actual sibling scope and both stable endpoints.
            identity_key = "finding:" + hashlib.sha256(
                (
                    f"{parent_id}:{previous.structure_node_id}:{current.structure_node_id}:"
                    f"{previous.explicit_label}:{current.explicit_label}"
                ).encode("utf-8")
            ).hexdigest()
            findings.append(
                StructureFinding(
                    finding_id=stable_structure_finding_id(
                        source_ir_id=snapshot.source_ir_id,
                        resolver_id=resolver_id,
                        resolver_version=resolver_version,
                        code="section_numbering_gap",
                        identity_key=identity_key,
                    ),
                    identity_key=identity_key,
                    code="section_numbering_gap",
                    severity=StructureFindingSeverity.WARNING,
                    blocking=False,
                    message_key="structure.section_numbering_gap",
                    structure_node_id=current.structure_node_id,
                    source_refs=current.source_refs,
                )
            )

        first_clause_by_ordinal: dict[int, StructureNode] = {}
        for node in ordered:
            if node.kind is not StructureNodeKind.CLAUSE or node.list_ordinal is None:
                continue
            previous = first_clause_by_ordinal.get(node.list_ordinal)
            if previous is None:
                first_clause_by_ordinal[node.list_ordinal] = node
                continue
            same_page = len(
                page_index.get(previous.structure_node_id, ())
            ) == 1 and page_index.get(previous.structure_node_id) == page_index.get(
                node.structure_node_id
            )
            if (
                not same_page
                or ("duplicate_list_ordinal", node.structure_node_id) in existing
            ):
                continue
            identity_key = (
                "finding:"
                + hashlib.sha256(
                    f"{parent_id}:{node.structure_node_id}:duplicate_list_ordinal".encode(
                        "utf-8"
                    )
                ).hexdigest()
            )
            findings.append(
                StructureFinding(
                    finding_id=stable_structure_finding_id(
                        source_ir_id=snapshot.source_ir_id,
                        resolver_id=resolver_id,
                        resolver_version=resolver_version,
                        code="duplicate_list_ordinal",
                        identity_key=identity_key,
                    ),
                    identity_key=identity_key,
                    code="duplicate_list_ordinal",
                    severity=StructureFindingSeverity.WARNING,
                    blocking=False,
                    message_key="structure.duplicate_list_ordinal",
                    structure_node_id=node.structure_node_id,
                    source_refs=node.source_refs,
                )
            )
    return tuple(findings)


def _structure_node_pages(
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
) -> dict[str, tuple[int, ...]]:
    source_pages = {
        node.source_node_id: node.native_locator.page_number for node in snapshot.nodes
    }
    cell_pages = {
        cell.cell_id: cell.native_locator.page_number
        for node in snapshot.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
    }
    visual_pages = {
        observation.observation_id: observation.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
        for observation in visual_snapshot.observations
    }
    result: dict[str, tuple[int, ...]] = {}
    for node in structure_nodes:
        pages: set[int] = set()
        for ref in node.source_refs:
            if ref.kind is StructureSourceRefKind.SOURCE_NODE:
                page = source_pages.get(ref.source_id)
            elif ref.kind is StructureSourceRefKind.TABLE_CELL:
                page = cell_pages.get(ref.source_id)
            else:
                page = visual_pages.get(ref.source_id)
            if page is not None:
                pages.add(page)
        result[node.structure_node_id] = tuple(sorted(pages))
    return result


def _continued_table_specs(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
) -> tuple[_ContinuationSpec, ...]:
    """Find one-to-one cross-page table continuations from strong evidence."""

    source_nodes = {node.source_node_id: node for node in snapshot.nodes}
    structure_by_id = {node.structure_node_id: node for node in structure_nodes}
    page_max_y = _page_max_y(snapshot)
    tables: list[tuple[StructureNode, SourceNode]] = []
    for structure_node in structure_nodes:
        if structure_node.kind is not StructureNodeKind.TABLE:
            continue
        source_table = next(
            (
                source_nodes[source_ref.source_id]
                for source_ref in structure_node.source_refs
                if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
                and source_ref.source_id in source_nodes
                and source_nodes[source_ref.source_id].kind is SourceNodeKind.TABLE
            ),
            None,
        )
        if source_table is not None:
            tables.append((structure_node, source_table))

    tables_by_page: dict[int, list[tuple[StructureNode, SourceNode]]] = {}
    for item in tables:
        page_number = item[1].native_locator.page_number
        if page_number is not None:
            tables_by_page.setdefault(page_number, []).append(item)
    explicit_caption_pairs = _explicit_caption_table_continuation_pairs(
        snapshot=snapshot,
        tables=tuple(tables),
    )

    candidates: list[tuple[StructureNode, StructureNode]] = []
    for source_structure, source_table in tables:
        source_page = source_table.native_locator.page_number
        if source_page is None:
            continue
        for target_structure, target_table in tables_by_page.get(
            source_page + 1,
            (),
        ):
            if _tables_are_strong_continuation_candidates(
                source_structure=source_structure,
                source_table=source_table,
                target_structure=target_structure,
                target_table=target_table,
                structure_by_id=structure_by_id,
                page_max_y=page_max_y,
                explicit_caption_match=(
                    source_structure.structure_node_id,
                    target_structure.structure_node_id,
                )
                in explicit_caption_pairs,
            ):
                candidates.append((source_structure, target_structure))

    candidates = list(
        _resolve_table_continuation_candidate_competition(
            snapshot=snapshot,
            tables=tuple(tables),
            candidates=tuple(candidates),
        )
    )

    targets_by_source: dict[str, set[str]] = {}
    sources_by_target: dict[str, set[str]] = {}
    for source, target in candidates:
        targets_by_source.setdefault(source.structure_node_id, set()).add(
            target.structure_node_id
        )
        sources_by_target.setdefault(target.structure_node_id, set()).add(
            source.structure_node_id
        )

    accepted = [
        (source, target)
        for source, target in candidates
        if len(targets_by_source[source.structure_node_id]) == 1
        and len(sources_by_target[target.structure_node_id]) == 1
    ]
    accepted.sort(key=lambda pair: (pair[1].source_order, pair[1].structure_node_id))
    return tuple(
        _ContinuationSpec(
            source_structure_node_id=source.structure_node_id,
            target_structure_node_id=target.structure_node_id,
            source_refs=target.source_refs,
        )
        for source, target in accepted
    )


def _explicit_caption_table_continuation_pairs(
    *,
    snapshot: SourceIRSnapshot,
    tables: tuple[tuple[StructureNode, SourceNode], ...],
) -> frozenset[tuple[str, str]]:
    """Bind exact ``Table N`` → ``Table N (continued)`` page boundaries.

    The caption route is deliberately one-to-one before ownership is checked:
    only the final grid on one page and the first grid on the adjacent page are
    eligible, and each grid must have one immediately governing caption.  This
    lets explicit immutable labels survive merged-cell grid refinements without
    turning repeated table-number mentions into continuation edges.
    """

    tables_by_page: dict[int, list[tuple[StructureNode, SourceNode]]] = {}
    for item in tables:
        page_number = item[1].native_locator.page_number
        if page_number is not None:
            tables_by_page.setdefault(page_number, []).append(item)
    for page_tables in tables_by_page.values():
        page_tables.sort(key=lambda item: _structure_reading_key(item[1]))

    flow_by_page: dict[int, tuple[SourceNode, ...]] = {}
    staged_flow: dict[int, list[SourceNode]] = {}
    for node in snapshot.nodes:
        page_number = node.native_locator.page_number
        if page_number is not None:
            staged_flow.setdefault(page_number, []).append(node)
    flow_by_page = {
        page_number: tuple(sorted(nodes, key=_structure_reading_key))
        for page_number, nodes in staged_flow.items()
    }

    result: set[tuple[str, str]] = set()
    for page_number, source_page_tables in tables_by_page.items():
        target_page_tables = tables_by_page.get(page_number + 1)
        if not source_page_tables or not target_page_tables:
            continue
        source_structure, source_table = source_page_tables[-1]
        target_structure, target_table = target_page_tables[0]
        source_caption = _unique_adjacent_table_caption(
            table_source=source_table,
            flow_by_page=flow_by_page,
        )
        target_caption = _unique_adjacent_table_caption(
            table_source=target_table,
            flow_by_page=flow_by_page,
        )
        if (
            source_caption is None
            or target_caption is None
            or not _is_explicit_table_continuation_caption(
                target_caption.raw_text
            )
        ):
            continue
        source_label = _table_caption_number_label(source_caption.raw_text)
        target_label = _table_caption_number_label(target_caption.raw_text)
        if source_label is None or source_label != target_label:
            continue
        result.add(
            (
                source_structure.structure_node_id,
                target_structure.structure_node_id,
            )
        )
    return frozenset(result)


def _resolve_table_continuation_candidate_competition(
    *,
    snapshot: SourceIRSnapshot,
    tables: tuple[tuple[StructureNode, SourceNode], ...],
    candidates: tuple[tuple[StructureNode, StructureNode], ...],
) -> tuple[tuple[StructureNode, StructureNode], ...]:
    """Prefer only a caption-bounded adjacent table over older competitors.

    Page-bottom geometry can make both a completed table and a later header-only
    table look compatible with the first grid on the next page.  A later table
    wins only when the immutable page reading flow proves all three boundaries:
    it is the final table on the source page, the target is the first table on
    the next page, and a high-confidence table caption separates it from every
    older competing source.  Without that proof the existing one-to-one gate
    remains fail-closed.
    """

    if not candidates:
        return ()
    table_source_by_structure_id = {
        structure.structure_node_id: source for structure, source in tables
    }
    tables_by_page: dict[int, list[SourceNode]] = {}
    for _, source in tables:
        page_number = source.native_locator.page_number
        if page_number is not None:
            tables_by_page.setdefault(page_number, []).append(source)
    for page_tables in tables_by_page.values():
        page_tables.sort(key=_structure_reading_key)

    flow_by_page: dict[int, tuple[SourceNode, ...]] = {}
    for node in snapshot.nodes:
        page_number = node.native_locator.page_number
        if page_number is None:
            continue
        flow_by_page.setdefault(page_number, ())
        flow_by_page[page_number] = (*flow_by_page[page_number], node)
    flow_by_page = {
        page_number: tuple(sorted(nodes, key=_structure_reading_key))
        for page_number, nodes in flow_by_page.items()
    }

    # A new, unmarked table caption immediately before the target is a hard
    # semantic boundary even when there is only one geometrically plausible
    # predecessor.  Run this per-edge before the single-candidate fast path.
    candidates = tuple(
        pair
        for pair in candidates
        if not _target_page_starts_a_new_captioned_table(
            source_source=table_source_by_structure_id.get(
                pair[0].structure_node_id
            ),
            target_source=table_source_by_structure_id.get(
                pair[1].structure_node_id
            ),
            flow_by_page=flow_by_page,
        )
    )
    if not candidates:
        return ()

    by_target: dict[str, list[tuple[StructureNode, StructureNode]]] = {}
    for pair in candidates:
        by_target.setdefault(pair[1].structure_node_id, []).append(pair)

    resolved: list[tuple[StructureNode, StructureNode]] = []
    for target_id, target_candidates in by_target.items():
        if len(target_candidates) == 1:
            resolved.extend(target_candidates)
            continue
        target_source = table_source_by_structure_id.get(target_id)
        source_items = tuple(
            (
                pair,
                table_source_by_structure_id.get(pair[0].structure_node_id),
            )
            for pair in target_candidates
        )
        if target_source is None or any(source is None for _, source in source_items):
            resolved.extend(target_candidates)
            continue
        typed_source_items = tuple(
            (pair, source)
            for pair, source in source_items
            if source is not None
        )
        source_pages = {
            source.native_locator.page_number for _, source in typed_source_items
        }
        target_page = target_source.native_locator.page_number
        if len(source_pages) != 1 or target_page is None:
            resolved.extend(target_candidates)
            continue
        source_page = next(iter(source_pages))
        if source_page is None or target_page != source_page + 1:
            resolved.extend(target_candidates)
            continue
        ordered_sources = tuple(
            sorted(typed_source_items, key=lambda item: _structure_reading_key(item[1]))
        )
        winner_pair, winner_source = ordered_sources[-1]
        previous_source = ordered_sources[-2][1]
        page_tables = tuple(tables_by_page.get(source_page, ()))
        target_page_tables = tuple(tables_by_page.get(target_page, ()))
        if (
            not page_tables
            or not target_page_tables
            or page_tables[-1].source_node_id != winner_source.source_node_id
            or target_page_tables[0].source_node_id
            != target_source.source_node_id
        ):
            resolved.extend(target_candidates)
            continue
        previous_key = _structure_reading_key(previous_source)
        winner_key = _structure_reading_key(winner_source)
        separating_captions = tuple(
            node
            for node in flow_by_page.get(source_page, ())
            if previous_key < _structure_reading_key(node) < winner_key
            and _is_high_confidence_table_caption_boundary(node.raw_text)
        )
        if not separating_captions:
            resolved.extend(target_candidates)
            continue
        resolved.append(winner_pair)
    return tuple(
        sorted(
            resolved,
            key=lambda pair: (
                pair[1].source_order,
                pair[1].structure_node_id,
                pair[0].source_order,
                pair[0].structure_node_id,
            ),
        )
    )


def _native_wrapped_content_specs(
    *,
    snapshot: SourceIRSnapshot,
    structure_nodes: tuple[StructureNode, ...],
) -> tuple[_ContinuationSpec, ...]:
    """Connect same-page native blocks only when layout proves a wrapped line."""

    source_nodes = {node.source_node_id: node for node in snapshot.nodes}
    page_body_right_edges: dict[int, list[tuple[str, float]]] = {}
    for source_node in snapshot.nodes:
        page_number = source_node.native_locator.page_number
        bbox = source_node.native_locator.bbox
        if (
            source_node.native_locator.format is not NativeDocumentFormat.PDF
            or page_number is None
            or bbox is None
            or source_node.kind
            not in {
                SourceNodeKind.PARAGRAPH,
                SourceNodeKind.LIST,
                SourceNodeKind.LIST_ITEM,
                SourceNodeKind.NOTE,
            }
        ):
            continue
        page_body_right_edges.setdefault(page_number, []).append(
            (source_node.source_node_id, bbox[2])
        )
    children_by_parent: dict[str, list[StructureNode]] = {}
    for node in structure_nodes:
        if node.parent_structure_node_id is not None:
            children_by_parent.setdefault(
                node.parent_structure_node_id,
                [],
            ).append(node)
    specs: list[_ContinuationSpec] = []
    for owner in structure_nodes:
        if (
            owner.kind is not StructureNodeKind.CLAUSE
            or owner.boundary_basis is not StructureBoundaryBasis.EXPLICIT_NUMBER
            or owner.semantic_source_spans
        ):
            continue
        direct_children = sorted(
            children_by_parent.get(owner.structure_node_id, ()),
            key=lambda node: (
                node.source_order,
                node.structure_node_id,
            ),
        )
        if not direct_children:
            continue
        first_child = direct_children[0]
        if (
            first_child.kind is not StructureNodeKind.CONTENT
            or first_child.boundary_basis is not StructureBoundaryBasis.NATIVE_BLOCK
            or first_child.semantic_source_spans
        ):
            continue
        owner_source = _single_native_text_source_node(
            owner,
            source_nodes=source_nodes,
        )
        child_source = _single_native_text_source_node(
            first_child,
            source_nodes=source_nodes,
        )
        if (
            owner_source is None
            or child_source is None
            or not _explicit_owner_and_content_share_wrapped_line(
                owner_source,
                child_source,
                peer_right_edges=tuple(
                    edge
                    for source_id, edge in page_body_right_edges.get(
                        owner_source.native_locator.page_number or 0,
                        (),
                    )
                    if source_id
                    not in {
                        owner_source.source_node_id,
                        child_source.source_node_id,
                    }
                ),
            )
        ):
            continue
        specs.append(
            _ContinuationSpec(
                source_structure_node_id=owner.structure_node_id,
                target_structure_node_id=first_child.structure_node_id,
                source_refs=first_child.source_refs,
            )
        )
    for siblings in children_by_parent.values():
        ordered = sorted(
            siblings,
            key=lambda node: (
                node.source_order,
                node.structure_node_id,
            ),
        )
        for previous, current in zip(ordered, ordered[1:]):
            if (
                previous.kind is not StructureNodeKind.CONTENT
                or current.kind is not StructureNodeKind.CONTENT
                or previous.boundary_basis is not StructureBoundaryBasis.NATIVE_BLOCK
                or current.boundary_basis is not StructureBoundaryBasis.NATIVE_BLOCK
                or previous.semantic_source_spans
                or current.semantic_source_spans
            ):
                continue
            previous_source = _single_native_text_source_node(
                previous,
                source_nodes=source_nodes,
            )
            current_source = _single_native_text_source_node(
                current,
                source_nodes=source_nodes,
            )
            if (
                previous_source is None
                or current_source is None
                or not _native_text_blocks_share_wrapped_line(
                    previous_source,
                    current_source,
                )
            ):
                continue
            specs.append(
                _ContinuationSpec(
                    source_structure_node_id=previous.structure_node_id,
                    target_structure_node_id=current.structure_node_id,
                    source_refs=current.source_refs,
                )
            )
    return tuple(specs)


def _explicit_owner_and_content_share_wrapped_line(
    owner: SourceNode,
    content: SourceNode,
    *,
    peer_right_edges: tuple[float, ...],
) -> bool:
    """Require conservative PDF topology before linking title-shaped text.

    A numbered owner and a separately grounded paragraph are different source
    facts by default.  A physical wrap is admitted only when the common native
    line checks pass and the next block returns toward the line start without
    extending farther right.  Horizontal exhaustion must be proven either by
    a stable right margin from independent body blocks or by a very short tail
    touching the owner's left edge.  This distinguishes a short numbered title
    followed by an introduction without inspecting customer wording.
    """

    owner_text = owner.raw_text.strip()
    content_text = content.raw_text.strip()
    if (
        not owner_text
        or not content_text
        or owner.native_locator.format is not NativeDocumentFormat.PDF
        or content.native_locator.format is not NativeDocumentFormat.PDF
        or owner.native_locator.page_number is None
        or owner.native_locator.page_number
        != content.native_locator.page_number
        or _SENTENCE_TERMINATOR.search(owner_text)
        or _is_high_confidence_caption_boundary(owner_text)
        or _NATIVE_BLOCK_ITEM_PREFIX.match(content_text)
        or _EXPLICIT_SECTION_PREFIX.match(content_text)
        or _EXPLICIT_LIST_PREFIX.match(content_text)
    ):
        return False
    owner_bbox = owner.native_locator.bbox
    content_bbox = content.native_locator.bbox
    if owner_bbox is None or content_bbox is None:
        return False
    owner_height = owner_bbox[3] - owner_bbox[1]
    content_height = content_bbox[3] - content_bbox[1]
    gap = content_bbox[1] - owner_bbox[3]
    if owner_height <= 0.0 or content_height <= 0.0 or gap < -1.0:
        return False
    max_gap = max(
        3.0,
        min(owner_height, content_height) * _NATIVE_CONTINUATION_MAX_GAP_RATIO,
    )
    right_edge_tolerance = max(6.0, owner_height * 1.5)
    peer_margin = max(peer_right_edges, default=None)
    peer_margin_is_stable = bool(
        peer_margin is not None
        and sum(
            abs(edge - peer_margin) <= right_edge_tolerance
            for edge in peer_right_edges
        )
        >= 2
        and abs(owner_bbox[2] - peer_margin) <= right_edge_tolerance
    )
    owner_width = owner_bbox[2] - owner_bbox[0]
    content_width = content_bbox[2] - content_bbox[0]
    strong_short_tail = bool(
        owner_width >= content_width * 4.0
        and content_bbox[0] < owner_bbox[0]
        and abs(content_bbox[2] - owner_bbox[0]) <= 2.0
    )
    return (
        gap <= max_gap
        and content_bbox[0] <= owner_bbox[0] + 1.0
        and owner_bbox[2] + 1.0 >= content_bbox[2]
        and content_bbox[2] + 2.0 >= owner_bbox[0]
        and (peer_margin_is_stable or strong_short_tail)
    )


def _single_native_text_source_node(
    node: StructureNode,
    *,
    source_nodes: dict[str, SourceNode],
) -> SourceNode | None:
    refs = node.effective_semantic_source_refs
    if len(refs) != 1 or refs[0].kind is not StructureSourceRefKind.SOURCE_NODE:
        return None
    source = source_nodes.get(refs[0].source_id)
    if source is None or source.kind not in {
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST,
        SourceNodeKind.LIST_ITEM,
        SourceNodeKind.NOTE,
    }:
        return None
    return source


def _native_text_blocks_share_wrapped_line(
    previous: SourceNode,
    current: SourceNode,
) -> bool:
    previous_text = previous.raw_text.strip()
    current_text = current.raw_text.strip()
    if (
        not previous_text
        or not current_text
        or previous.native_locator.format is not NativeDocumentFormat.PDF
        or current.native_locator.format is not NativeDocumentFormat.PDF
        or previous.native_locator.page_number is None
        or previous.native_locator.page_number != current.native_locator.page_number
        or _SENTENCE_TERMINATOR.search(previous_text)
        or _is_high_confidence_caption_boundary(previous_text)
        or _NATIVE_BLOCK_ITEM_PREFIX.match(current_text)
        or _EXPLICIT_SECTION_PREFIX.match(current_text)
        or _EXPLICIT_LIST_PREFIX.match(current_text)
    ):
        return False
    previous_bbox = previous.native_locator.bbox
    current_bbox = current.native_locator.bbox
    if previous_bbox is None or current_bbox is None:
        return False
    previous_height = previous_bbox[3] - previous_bbox[1]
    current_height = current_bbox[3] - current_bbox[1]
    gap = current_bbox[1] - previous_bbox[3]
    if previous_height <= 0.0 or current_height <= 0.0 or gap < -1.0:
        return False
    max_gap = max(
        3.0,
        min(previous_height, current_height) * _NATIVE_CONTINUATION_MAX_GAP_RATIO,
    )
    horizontal_overlap = max(
        0.0,
        min(previous_bbox[2], current_bbox[2]) - max(previous_bbox[0], current_bbox[0]),
    )
    minimum_width = min(
        previous_bbox[2] - previous_bbox[0],
        current_bbox[2] - current_bbox[0],
    )
    return (
        gap <= max_gap
        and abs(previous_bbox[0] - current_bbox[0]) <= _NATIVE_CONTINUATION_MAX_X_SHIFT
        and minimum_width > 0.0
        and horizontal_overlap / minimum_width >= 0.35
    )


def _is_high_confidence_caption_boundary(raw_text: str) -> bool:
    """Veto physical-line joining only for a label-like table/figure caption.

    The shared caption-prefix grammar intentionally also recognizes body
    references such as ``表1所要求…`` and ``Table 1 shall ...`` so that
    other structure rules can recover their numeric labels.  A prefix match
    alone is therefore not enough evidence to split two adjacent PDF text
    blocks.  A grammatical body-reference tail or normative predicate keeps
    the blocks eligible for the normal geometry-based continuation test.
    Compact CJK forms additionally treat common predicates as body prose only
    when the number has no whitespace or punctuation title separator, so a
    short caption such as ``表1 要求`` remains a valid hard boundary.
    """

    match = _TABLE_CAPTION_PREFIX.match(raw_text)
    if match is None:
        match = _FIGURE_CAPTION_PREFIX.match(raw_text)
    if match is None:
        return False
    raw_tail = raw_text[match.end() :]
    has_title_separator = bool(
        raw_tail and _CAPTION_BOUNDARY_SEPARATOR.match(raw_tail).end() > 0
    )
    tail = _CAPTION_BOUNDARY_SEPARATOR.sub("", raw_tail, count=1)
    if not tail:
        return True
    return not (
        _CAPTION_BODY_REFERENCE_PREFIX.match(tail)
        or (
            not has_title_separator
            and _CAPTION_UNSEPARATED_BODY_PREDICATE_PREFIX.match(tail)
        )
        or _CAPTION_NORMATIVE_PREDICATE.search(tail)
    )


def _is_high_confidence_table_caption_boundary(raw_text: str) -> bool:
    return (
        _TABLE_CAPTION_PREFIX.match(raw_text) is not None
        and _is_high_confidence_caption_boundary(raw_text)
    )


_TABLE_CONTINUATION_CAPTION_PREFIX = re.compile(
    r"^\s*(?:续表(?=\s*\d)|continued\s+table(?=\s*\d))",
    re.IGNORECASE,
)
_TABLE_CONTINUATION_CAPTION_SUFFIX = re.compile(
    r"[（(]\s*(?:续|continued)\s*[）)](?=\s|$)",
    re.IGNORECASE,
)


def _is_explicit_table_continuation_caption(raw_text: str) -> bool:
    """Recognize only structural continuation markers, never word substrings."""

    if _TABLE_CONTINUATION_CAPTION_PREFIX.match(raw_text) is not None:
        return True
    caption = _TABLE_CAPTION_PREFIX.match(raw_text)
    return bool(
        caption is not None
        and _TABLE_CONTINUATION_CAPTION_SUFFIX.search(raw_text[caption.end() :])
        is not None
    )


def _target_page_starts_a_new_captioned_table(
    *,
    source_source: SourceNode | None,
    target_source: SourceNode | None,
    flow_by_page: dict[int, tuple[SourceNode, ...]],
) -> bool:
    """Veto an edge when target-page body flow declares a new table.

    TOC/furniture/table payload text is deliberately excluded.  A caption that
    explicitly says the table is continued is compatible with an edge; every
    other high-confidence table caption before the grid is a fail-closed new
    boundary because printed and physical table numbers need not align.
    """

    if target_source is None or target_source.native_locator.page_number is None:
        return False
    target_key = _structure_reading_key(target_source)
    eligible_kinds = {
        SourceNodeKind.HEADING,
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST,
        SourceNodeKind.LIST_ITEM,
        SourceNodeKind.NOTE,
    }
    captions = tuple(
        node
        for node in flow_by_page.get(target_source.native_locator.page_number, ())
        if node.kind in eligible_kinds
        and _structure_reading_key(node) < target_key
        and _is_high_confidence_table_caption_boundary(node.raw_text)
    )
    if any(
        not _is_explicit_table_continuation_caption(caption.raw_text)
        for caption in captions
    ):
        return True

    # An explicit continuation marker is not sufficient when both physical
    # table segments have a unique adjacent numbered caption.  In that case
    # the immutable labels are stronger evidence: ``Table N`` may continue as
    # ``Table N (continued)``, but it must not silently absorb ``Table M
    # (continued)``.  Missing or ambiguous source captions keep the existing
    # geometry/grid evidence path; this check never invents a label.
    target_caption = _unique_adjacent_table_caption(
        table_source=target_source,
        flow_by_page=flow_by_page,
    )
    if (
        target_caption is None
        or not _is_explicit_table_continuation_caption(target_caption.raw_text)
    ):
        return False
    source_caption = _unique_adjacent_table_caption(
        table_source=source_source,
        flow_by_page=flow_by_page,
    )
    if source_caption is None:
        return False
    source_label = _table_caption_number_label(source_caption.raw_text)
    target_label = _table_caption_number_label(target_caption.raw_text)
    return bool(
        source_label is not None
        and target_label is not None
        and source_label != target_label
    )


def _unique_adjacent_table_caption(
    *,
    table_source: SourceNode | None,
    flow_by_page: dict[int, tuple[SourceNode, ...]],
) -> SourceNode | None:
    """Return one trailing body caption immediately governing a table grid."""

    if table_source is None or table_source.native_locator.page_number is None:
        return None
    table_key = _structure_reading_key(table_source)
    table_bbox = table_source.native_locator.bbox
    eligible_kinds = {
        SourceNodeKind.HEADING,
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST,
        SourceNodeKind.LIST_ITEM,
        SourceNodeKind.NOTE,
    }
    preceding_body = tuple(
        node
        for node in flow_by_page.get(table_source.native_locator.page_number, ())
        if node.kind in eligible_kinds
        and _structure_reading_key(node) < table_key
        and not (
            table_bbox is not None
            and node.native_locator.bbox is not None
            and _bbox_coverage(node.native_locator.bbox, table_bbox) >= 0.98
        )
    )
    adjacent_captions: list[SourceNode] = []
    for node in reversed(preceding_body):
        if not (
            _is_high_confidence_table_caption_boundary(node.raw_text)
            or _TABLE_CONTINUATION_CAPTION_PREFIX.match(node.raw_text) is not None
        ):
            break
        adjacent_captions.append(node)
    if len(adjacent_captions) != 1:
        return None
    return adjacent_captions[0]


def _table_caption_number_label(raw_text: str) -> str | None:
    """Extract a canonical number from regular or leading continuation captions."""

    match = _TABLE_CAPTION_PREFIX.match(raw_text)
    if match is None:
        continuation = _TABLE_CONTINUATION_CAPTION_PREFIX.match(raw_text)
        if continuation is None:
            return None
        match = _TABLE_CAPTION_PREFIX.match(
            f"Table {raw_text[continuation.end():]}"
        )
    if match is None:
        return None
    return _canonical_caption_number_label(match.group("label"))


def _tables_are_strong_continuation_candidates(
    *,
    source_structure: StructureNode,
    source_table: SourceNode,
    target_structure: StructureNode,
    target_table: SourceNode,
    structure_by_id: dict[str, StructureNode],
    page_max_y: dict[int, float],
    explicit_caption_match: bool = False,
) -> bool:
    source_locator = source_table.native_locator
    target_locator = target_table.native_locator
    source_payload = source_table.table_payload
    target_payload = target_table.table_payload
    source_section_id = _nearest_section_ancestor_id(
        source_structure,
        structure_by_id,
    )
    target_section_id = _nearest_section_ancestor_id(
        target_structure,
        structure_by_id,
    )
    if (
        source_locator.format is not NativeDocumentFormat.PDF
        or target_locator.format is not NativeDocumentFormat.PDF
        or source_locator.page_number is None
        or target_locator.page_number != source_locator.page_number + 1
        or source_locator.bbox is None
        or target_locator.bbox is None
        or source_payload is None
        or target_payload is None
        or source_payload.column_count < 2
        or target_payload.column_count < 2
        or source_structure.parent_structure_node_id
        != target_structure.parent_structure_node_id
        or source_section_id is None
        or source_section_id != target_section_id
    ):
        return False
    if not _table_bboxes_horizontally_align(
        source_locator.bbox,
        target_locator.bbox,
    ):
        return False
    if explicit_caption_match:
        return True
    source_page_height = page_max_y.get(source_locator.page_number, 0.0)
    target_page_height = page_max_y.get(target_locator.page_number, 0.0)
    if (
        source_page_height <= 0.0
        or source_locator.bbox[3] / source_page_height
        < _TABLE_PREVIOUS_PAGE_BOTTOM_RATIO
        or not _target_table_starts_in_page_top_band(
            source_bbox=source_locator.bbox,
            target_bbox=target_locator.bbox,
            target_page_content_max_y=target_page_height,
        )
    ):
        return False
    # Repeated headers strengthen semantic confidence but never override a
    # shifted/crossing physical grid.  Every candidate must satisfy the same
    # column-partition compatibility gate.
    return _table_column_partitions_equivalent(
        source_cells=source_payload.cells,
        target_cells=target_payload.cells,
        source_column_count=source_payload.column_count,
        target_column_count=target_payload.column_count,
    )


def _target_table_starts_in_page_top_band(
    *,
    source_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
    target_page_content_max_y: float,
) -> bool:
    """Keep the page-top gate when SourceIR lacks physical page dimensions.

    ``SourceIR`` currently carries object coordinates, not the immutable PDF
    media-box height.  The ordinary ratio remains authoritative when the
    captured page content provides a useful vertical extent.  On a sparse page
    the target table itself may define ``content_max_y``; treating its bottom as
    the physical page bottom makes a real page-top table appear too low.  That
    fallback is accepted only when both physical tables start in a strict
    absolute top band and their top edges align.  A page-middle table therefore
    cannot pass merely because it is the last captured object on the page.
    """

    if (
        target_page_content_max_y > 0.0
        and target_bbox[1] / target_page_content_max_y
        <= _TABLE_NEXT_PAGE_TOP_RATIO
    ):
        return True
    if (
        target_page_content_max_y <= 0.0
        or abs(target_page_content_max_y - target_bbox[3])
        > _TABLE_SPARSE_PAGE_CONTENT_EXTENT_TOLERANCE_POINTS
        or source_bbox[1] > _TABLE_SPARSE_PAGE_TOP_MAX_POINTS
        or target_bbox[1] > _TABLE_SPARSE_PAGE_TOP_MAX_POINTS
        or abs(source_bbox[1] - target_bbox[1])
        > _TABLE_SPARSE_PAGE_TOP_ALIGNMENT_TOLERANCE_POINTS
    ):
        return False
    return True


def _nearest_section_ancestor_id(
    node: StructureNode,
    nodes_by_id: dict[str, StructureNode],
) -> str | None:
    parent_id = node.parent_structure_node_id
    while parent_id is not None:
        parent = nodes_by_id[parent_id]
        if parent.kind is StructureNodeKind.SECTION:
            return parent_id
        parent_id = parent.parent_structure_node_id
    return None


def _table_bboxes_horizontally_align(
    source_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
) -> bool:
    source_width = source_bbox[2] - source_bbox[0]
    target_width = target_bbox[2] - target_bbox[0]
    if source_width <= 0.0 or target_width <= 0.0:
        return False
    tolerance = max(
        _TABLE_HORIZONTAL_ALIGNMENT_MIN_POINTS,
        max(source_width, target_width) * _TABLE_HORIZONTAL_ALIGNMENT_RATIO,
    )
    return (
        abs(source_bbox[0] - target_bbox[0]) <= tolerance
        and abs(source_bbox[2] - target_bbox[2]) <= tolerance
    )


def _table_column_partitions_equivalent(
    *,
    source_cells: tuple[TableCellIR, ...],
    target_cells: tuple[TableCellIR, ...],
    source_column_count: int,
    target_column_count: int,
) -> bool:
    """Return whether one physical column grid is a refinement of the other.

    A continuation page may expose an extra column boundary that was hidden by
    a merged/wide cell on the previous page.  Column counts therefore need not
    be equal, but every boundary of the coarser partition must still occur in
    the finer partition.  Crossing or shifted partitions remain rejected.
    """

    source_boundaries = _table_column_boundaries(
        source_cells,
        source_column_count,
    )
    target_boundaries = _table_column_boundaries(
        target_cells,
        target_column_count,
    )
    if source_boundaries is None or target_boundaries is None:
        return False
    source_width = source_boundaries[-1] - source_boundaries[0]
    target_width = target_boundaries[-1] - target_boundaries[0]
    if source_width <= 0.0 or target_width <= 0.0:
        return False
    tolerance = max(
        _TABLE_COLUMN_GRID_ALIGNMENT_MIN_POINTS,
        max(source_width, target_width) * _TABLE_COLUMN_GRID_ALIGNMENT_RATIO,
    )
    if (
        abs(source_boundaries[0] - target_boundaries[0]) > tolerance
        or abs(source_boundaries[-1] - target_boundaries[-1]) > tolerance
    ):
        return False
    return _table_partition_refines(
        coarse_boundaries=source_boundaries,
        fine_boundaries=target_boundaries,
        tolerance=tolerance,
    ) or _table_partition_refines(
        coarse_boundaries=target_boundaries,
        fine_boundaries=source_boundaries,
        tolerance=tolerance,
    )


def _table_partition_refines(
    *,
    coarse_boundaries: tuple[float, ...],
    fine_boundaries: tuple[float, ...],
    tolerance: float,
) -> bool:
    return all(
        any(abs(coarse_x - fine_x) <= tolerance for fine_x in fine_boundaries)
        for coarse_x in coarse_boundaries
    )


def _table_column_boundaries(
    cells: tuple[TableCellIR, ...],
    column_count: int,
) -> tuple[float, ...] | None:
    candidates: dict[int, list[float]] = {
        boundary: [] for boundary in range(column_count + 1)
    }
    for cell in cells:
        bbox = cell.native_locator.bbox
        if bbox is None:
            continue
        candidates[cell.column].append(bbox[0])
        candidates[cell.column + cell.column_span].append(bbox[2])
    boundaries: list[float] = []
    for boundary in range(column_count + 1):
        values = sorted(candidates[boundary])
        if not values:
            return None
        middle = len(values) // 2
        if len(values) % 2:
            boundaries.append(values[middle])
        else:
            boundaries.append((values[middle - 1] + values[middle]) / 2.0)
    if any(right <= left for left, right in zip(boundaries, boundaries[1:])):
        return None
    return tuple(boundaries)


def _repeated_leading_row_count(
    source_cells: tuple[TableCellIR, ...],
    target_cells: tuple[TableCellIR, ...],
) -> int:
    source_rows = _leading_row_fingerprints(source_cells)
    target_rows = _leading_row_fingerprints(target_cells)
    repeated = 0
    for source_row, target_row in zip(source_rows, target_rows, strict=False):
        if source_row != target_row:
            break
        repeated += 1
    return repeated


def _leading_row_fingerprints(
    cells: tuple[TableCellIR, ...],
) -> tuple[tuple[str, ...], ...]:
    row_indices = sorted({cell.row for cell in cells})[:_MAX_REPEATED_LEADING_ROWS]
    rows: list[tuple[str, ...]] = []
    for row_index in row_indices:
        row = tuple(
            " ".join(cell.raw_text.split())
            for cell in sorted(
                (cell for cell in cells if cell.row == row_index),
                key=lambda item: (item.column, item.cell_id),
            )
        )
        if sum(bool(value) for value in row) < 2:
            break
        rows.append(row)
    return tuple(rows)


def _page_max_y(snapshot: SourceIRSnapshot) -> dict[int, float]:
    maximums: dict[int, float] = {}
    for node in snapshot.nodes:
        locator = node.native_locator
        if (
            locator.format is NativeDocumentFormat.PDF
            and locator.page_number is not None
            and locator.bbox is not None
        ):
            maximums[locator.page_number] = max(
                maximums.get(locator.page_number, 0.0),
                locator.bbox[3],
            )
    return maximums


def _nearest_retained_parent_source_id(
    source_node: SourceNode,
    *,
    source_nodes_by_id: dict[str, SourceNode],
    folded_source_node_ids: set[str],
) -> str | None:
    parent_id = source_node.parent_source_node_id
    while parent_id in folded_source_node_ids:
        parent_id = source_nodes_by_id[parent_id].parent_source_node_id
    return parent_id


def _node_ref(source_node_id: str) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_node_id,
    )


def _exact_structure_source_span(
    *,
    source_ref: StructureSourceRef,
    start_offset: int,
    end_offset: int,
) -> StructureSourceSpan:
    return StructureSourceSpan(
        structure_source_span_id=stable_structure_source_span_id(
            source_ref=source_ref,
            start_offset=start_offset,
            end_offset=end_offset,
            boundary_basis=StructureSourceSpanBoundaryBasis.EXPLICIT_NUMBER,
        ),
        source_ref=source_ref,
        start_offset=start_offset,
        end_offset=end_offset,
        boundary_basis=StructureSourceSpanBoundaryBasis.EXPLICIT_NUMBER,
    )


def _cell_ref(cell_id: str) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.TABLE_CELL,
        source_id=cell_id,
    )


def _visual_ref(observation_id: str) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
        source_id=observation_id,
    )


def _relation(
    *,
    source_ir_id: str,
    resolver_id: str,
    resolver_version: str,
    kind: StructureRelationKind,
    source_structure_node_id: str,
    target_structure_node_id: str,
    source_refs: tuple[StructureSourceRef, ...],
    option_state: StructureOptionState | None = None,
) -> StructureRelation:
    return StructureRelation(
        relation_id=stable_structure_relation_id(
            source_ir_id=source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            kind=kind,
            source_structure_node_id=source_structure_node_id,
            target_structure_node_id=target_structure_node_id,
            source_refs=source_refs,
            option_state=option_state,
        ),
        kind=kind,
        source_structure_node_id=source_structure_node_id,
        target_structure_node_id=target_structure_node_id,
        source_refs=source_refs,
        option_state=option_state,
    )


__all__ = [
    "DEFAULT_STRUCTURE_RESOLVER_ID",
    "DEFAULT_STRUCTURE_RESOLVER_VERSION",
    "StructureResolutionRejected",
    "StructureResolutionRejectionCode",
    "build_structure_tree",
    "superseded_native_text_source_ids",
    "validate_structure_tree_against_source_ir",
    "visual_text_authority_pages",
]
