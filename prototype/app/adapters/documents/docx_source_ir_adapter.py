from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from os import PathLike
from pathlib import Path

from lxml import etree

from app.domain.extraction.preflight import (
    DocumentFormat,
    DocumentPreflightReport,
)
from app.domain.extraction.source_ir import (
    DocxInlineBoundaryBasis,
    DocxInlineLanguageEvidence,
    DocxInlineSpan,
    DocxListMetadata,
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
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
    stable_table_id,
)
from app.ports.source_ir_adapter import (
    SourceIRAdapterRejected,
    SourceIRAdapterRejectionCode,
)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPE_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_V_NS = "urn:schemas-microsoft-com:vml"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_ASVG_NS = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"

# A Choice is selectable only when every namespace named by mc:Requires is
# explicitly understood by this adapter. Unknown/future extension namespaces
# must use the document's Fallback branch instead of being traversed blindly.
_SUPPORTED_MC_REQUIREMENT_NAMESPACES = frozenset(
    {_W_NS, _R_NS, _A_NS, _V_NS, _WPS_NS, _ASVG_NS}
)

_NS = {
    "w": _W_NS,
    "r": _R_NS,
    "a": _A_NS,
    "v": _V_NS,
}
_DOCUMENT_XML = "word/document.xml"
_DOCUMENT_RELS_XML = "word/_rels/document.xml.rels"
_CONTENT_TYPES_XML = "[Content_Types].xml"
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_DOCX_SOURCE_NODES = 500_000
_MAX_DOCX_PHYSICAL_TABLE_CELLS = 500_000
_MAX_DOCX_IMAGE_PLACEMENTS = 50_000
_MAX_DOCX_UNIQUE_MEDIA_BYTES = 512 * 1024 * 1024
_IMAGE_RELATIONSHIP_SUFFIX = "/image"
_CONTENT_RELATIONSHIP_SUFFIXES = frozenset(
    {
        "/aFChunk",
        "/chart",
        "/diagramData",
        "/diagramLayout",
        "/diagramQuickStyle",
        "/diagramColors",
        "/embeddedPackage",
        "/footer",
        "/footnotes",
        "/header",
        "/oleObject",
        "/endnotes",
    }
)
_IGNORED_NONVISUAL_BODY_TAGS = frozenset(
    {"bookmarkStart", "bookmarkEnd", "proofErr", "permStart", "permEnd"}
)


@dataclass(frozen=True, slots=True)
class _Relationship:
    relationship_id: str
    relationship_type: str
    target: str
    external: bool


@dataclass(slots=True)
class _CellBuild:
    row: int
    column: int
    column_span: int
    raw_text: str
    row_span: int = 1


@dataclass(frozen=True, slots=True)
class _CellParagraph:
    element: etree._Element
    row: int
    column: int
    paragraph_index: int


@dataclass(frozen=True, slots=True)
class _DocxInlineSpanSpec:
    start_offset: int
    end_offset: int
    language_evidence: DocxInlineLanguageEvidence


@dataclass(frozen=True, slots=True)
class _TableBuild:
    payload: TableIR | None
    cell_paragraphs: tuple[_CellParagraph, ...]
    diagnostics: tuple[SourceDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class _ImageRendition:
    relationship_id: str
    kind: str
    source_order: int


@dataclass(frozen=True, slots=True)
class _ImagePlacement:
    renditions: tuple[_ImageRendition, ...]


@dataclass(frozen=True, slots=True)
class _MediaPartMetadata:
    content_sha256: str
    asset_id: str
    media_type: str
    storage_key: str
    content_bytes: bytes = field(repr=False)


@dataclass(slots=True)
class _DocxResourceBudget:
    source_nodes: int = 0
    physical_table_cells: int = 0
    image_placements: int = 0
    unique_media_bytes: int = 0
    media_parts: dict[str, _MediaPartMetadata] = field(default_factory=dict)

    def reserve_source_nodes(self, count: int = 1) -> None:
        self.source_nodes += count
        if self.source_nodes > _MAX_DOCX_SOURCE_NODES:
            raise _DocxResourceBudgetExceeded("SourceIR node budget exceeded")

    def reserve_physical_table_cells(self, count: int = 1) -> None:
        self.physical_table_cells += count
        if self.physical_table_cells > _MAX_DOCX_PHYSICAL_TABLE_CELLS:
            raise _DocxResourceBudgetExceeded("physical table-cell budget exceeded")

    def reserve_image_placements(self, count: int) -> None:
        self.image_placements += count
        if self.image_placements > _MAX_DOCX_IMAGE_PLACEMENTS:
            raise _DocxResourceBudgetExceeded("image-placement budget exceeded")

    def remaining_image_placements(self) -> int:
        return max(0, _MAX_DOCX_IMAGE_PLACEMENTS - self.image_placements)

    def reserve_unique_media_bytes(self, count: int) -> None:
        self.unique_media_bytes += count
        if self.unique_media_bytes > _MAX_DOCX_UNIQUE_MEDIA_BYTES:
            raise _DocxResourceBudgetExceeded("unique media byte budget exceeded")


class _DocxResourceBudgetExceeded(RuntimeError):
    pass


class DocxSourceIRAdapter:
    """Read-only OOXML-to-SourceIR adapter used by the shadow pipeline.

    The adapter consumes only the original ZIP package and returns references
    to package parts; it never extracts media beside the source document.
    """

    adapter_id = "native-docx-ooxml"
    adapter_version = "1.2.1"
    document_format = DocumentFormat.DOCX

    def __init__(
        self,
        *,
        source_asset_publisher: Callable[[SourceAssetRef, bytes], None]
        | None = None,
    ) -> None:
        if source_asset_publisher is not None and not callable(
            source_asset_publisher
        ):
            raise TypeError("source_asset_publisher must be callable")
        self._source_asset_publisher = source_asset_publisher

    def extract(
        self,
        source_path: str | PathLike[str],
        *,
        preflight: DocumentPreflightReport,
        created_at: datetime,
    ) -> SourceIRSnapshot:
        path = Path(source_path)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_TIMESTAMP,
                "created_at must be timezone-aware before DOCX extraction.",
            )
        source_bytes, source_size, source_sha256 = _read_source_bytes(path)
        self._validate_preflight(
            path=path,
            source_size=source_size,
            source_sha256=source_sha256,
            preflight=preflight,
        )

        try:
            nodes, assets, diagnostics = self._extract_package(source_bytes, source_sha256)
        except _DocxResourceBudgetExceeded:
            return self._snapshot(
                source_sha256=source_sha256,
                created_at=created_at,
                status=SourceIRStatus.FAILED,
                diagnostics=(
                    SourceDiagnostic(
                        code="docx_resource_budget_exceeded",
                        severity=SourceDiagnosticSeverity.ERROR,
                        message=(
                            "The validated DOCX package exceeds the safe SourceIR node, "
                            "physical table-cell, image-placement, or unique media byte budget."
                        ),
                    ),
                ),
            )
        except Exception as exc:
            return self._snapshot(
                source_sha256=source_sha256,
                created_at=created_at,
                status=SourceIRStatus.FAILED,
                diagnostics=(
                    SourceDiagnostic(
                        code="docx_parse_failed",
                        severity=SourceDiagnosticSeverity.ERROR,
                        message=(
                            "The validated DOCX package could not be mapped to SourceIR "
                            f"({type(exc).__name__})."
                        ),
                    ),
                ),
            )

        status = (
            SourceIRStatus.PARTIAL
            if any(
                item.severity
                in {SourceDiagnosticSeverity.WARNING, SourceDiagnosticSeverity.ERROR}
                for item in diagnostics
            )
            else SourceIRStatus.COMPLETE
        )
        return self._snapshot(
            source_sha256=source_sha256,
            created_at=created_at,
            status=status,
            nodes=nodes,
            assets=assets,
            diagnostics=diagnostics,
        )

    def _validate_preflight(
        self,
        *,
        path: Path,
        source_size: int,
        source_sha256: str,
        preflight: DocumentPreflightReport,
    ) -> None:
        problems: list[str] = []
        identity = preflight.identity
        if not preflight.processable:
            problems.append("preflight did not mark the source processable")
        if identity.detected_format is not DocumentFormat.DOCX:
            problems.append("preflight did not identify a DOCX package")
        if identity.source_name != path.name:
            problems.append("preflight source name does not match the requested source")
        if identity.declared_extension != path.suffix.lower():
            problems.append("preflight extension does not match the requested source")
        if problems:
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_PREFLIGHT,
                "DOCX extraction was blocked: " + "; ".join(problems) + ".",
            )
        if identity.source_sha256 != source_sha256 or identity.source_size != source_size:
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.SOURCE_CHANGED,
                "DOCX source bytes changed after preflight.",
            )

    def _extract_package(
        self,
        source_bytes: bytes,
        source_sha256: str,
    ) -> tuple[tuple[SourceNode, ...], tuple[SourceAssetRef, ...], tuple[SourceDiagnostic, ...]]:
        nodes: list[SourceNode] = []
        assets_by_id: dict[str, SourceAssetRef] = {}
        diagnostics: list[SourceDiagnostic] = []
        resource_budget = _DocxResourceBudget()
        source_order = 0

        with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
            member_names = frozenset(archive.namelist())
            document_root = _parse_xml(archive.read(_DOCUMENT_XML))
            body = document_root.find("w:body", namespaces=_NS)
            if body is None:
                raise ValueError("word/document.xml has no w:body")
            relationships = _read_relationships(archive, member_names)
            content_types = _read_content_types(archive, member_names)
            numbered_styles, heading_styles = _read_style_kinds(archive, member_names)

            unrepresented_alternate_content = (
                _count_unrepresented_alternate_content(document_root)
            )
            if unrepresented_alternate_content:
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_alternate_content_not_represented",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message=(
                            "DOCX compatibility markup had "
                            f"{unrepresented_alternate_content} AlternateContent "
                            "branch(es) with neither a supported Choice nor a Fallback; "
                            "no replacement content was inferred."
                        ),
                    )
                )

            if (
                "word/comments.xml" in member_names
                or document_root.find(".//w:commentRangeStart", namespaces=_NS) is not None
                or document_root.find(".//w:commentReference", namespaces=_NS) is not None
            ):
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_comments_ignored_by_policy",
                        severity=SourceDiagnosticSeverity.INFO,
                        message="DOCX comments were intentionally ignored by the default extraction policy.",
                    )
                )

            paragraph_index = 0
            table_index = 0
            ignored_nonvisual_markers: set[str] = set()
            for body_element_index, element in _iter_body_content(body):
                if element.tag == _qn(_W_NS, "p"):
                    locator = NativeLocator(
                        format=NativeDocumentFormat.DOCX,
                        body_element_index=body_element_index,
                        paragraph_index=paragraph_index,
                    )
                    raw_text, inline_span_specs = (
                        _paragraph_raw_text_and_inline_span_specs(element)
                    )
                    kind = _paragraph_kind(element, numbered_styles, heading_styles)
                    paragraph_node: SourceNode | None = None
                    if raw_text:
                        list_metadata = _direct_docx_list_metadata(
                            element,
                            locator=locator,
                            diagnostics=diagnostics,
                        )
                        resource_budget.reserve_source_nodes()
                        paragraph_node = self._node(
                            source_sha256=source_sha256,
                            kind=kind,
                            source_order=source_order,
                            raw_text=raw_text,
                            locator=locator,
                            docx_list_metadata=list_metadata,
                            docx_inline_span_specs=inline_span_specs,
                        )
                        nodes.append(paragraph_node)
                        source_order += 1
                    image_nodes, source_order = self._map_images(
                        archive=archive,
                        member_names=member_names,
                        content_types=content_types,
                        relationships=relationships,
                        paragraph=element,
                        locator=locator,
                        source_sha256=source_sha256,
                        source_order=source_order,
                        parent_source_node_id=(
                            paragraph_node.source_node_id if paragraph_node is not None else None
                        ),
                        assets_by_id=assets_by_id,
                        diagnostics=diagnostics,
                        resource_budget=resource_budget,
                    )
                    nodes.extend(image_nodes)
                    paragraph_index += 1
                    continue

                if element.tag == _qn(_W_NS, "tbl"):
                    table_locator = NativeLocator(
                        format=NativeDocumentFormat.DOCX,
                        body_element_index=body_element_index,
                        table_index=table_index,
                    )
                    table_build = self._map_table(
                        element=element,
                        locator=table_locator,
                        source_sha256=source_sha256,
                        resource_budget=resource_budget,
                    )
                    diagnostics.extend(table_build.diagnostics)
                    table_node: SourceNode | None = None
                    if table_build.payload is not None:
                        resource_budget.reserve_source_nodes()
                        table_node = self._node(
                            source_sha256=source_sha256,
                            kind=SourceNodeKind.TABLE,
                            source_order=source_order,
                            raw_text="",
                            locator=table_locator,
                            table_payload=table_build.payload,
                        )
                        nodes.append(table_node)
                        source_order += 1

                    for cell_paragraph in table_build.cell_paragraphs:
                        cell_locator = NativeLocator(
                            format=NativeDocumentFormat.DOCX,
                            body_element_index=body_element_index,
                            table_index=table_index,
                            row_index=cell_paragraph.row,
                            column_index=cell_paragraph.column,
                            paragraph_index=cell_paragraph.paragraph_index,
                        )
                        # TableIR keeps the complete cell text.  These child nodes are a
                        # parallel fact-layer view of physical OOXML paragraphs; they do
                        # not imply Requirement entities or automatic requirement splits.
                        raw_text, inline_span_specs = (
                            _paragraph_raw_text_and_inline_span_specs(
                                cell_paragraph.element
                            )
                        )
                        paragraph_node: SourceNode | None = None
                        if raw_text:
                            kind = _paragraph_kind(
                                cell_paragraph.element,
                                numbered_styles,
                                heading_styles,
                            )
                            list_metadata = _direct_docx_list_metadata(
                                cell_paragraph.element,
                                locator=cell_locator,
                                diagnostics=diagnostics,
                            )
                            resource_budget.reserve_source_nodes()
                            paragraph_node = self._node(
                                source_sha256=source_sha256,
                                kind=kind,
                                source_order=source_order,
                                raw_text=raw_text,
                                locator=cell_locator,
                                parent_source_node_id=(
                                    table_node.source_node_id
                                    if table_node is not None
                                    else None
                                ),
                                docx_list_metadata=list_metadata,
                                docx_inline_span_specs=inline_span_specs,
                            )
                            nodes.append(paragraph_node)
                            source_order += 1
                        image_nodes, source_order = self._map_images(
                            archive=archive,
                            member_names=member_names,
                            content_types=content_types,
                            relationships=relationships,
                            paragraph=cell_paragraph.element,
                            locator=cell_locator,
                            source_sha256=source_sha256,
                            source_order=source_order,
                            parent_source_node_id=(
                                paragraph_node.source_node_id
                                if paragraph_node is not None
                                else (
                                    table_node.source_node_id
                                    if table_node is not None
                                    else None
                                )
                            ),
                            assets_by_id=assets_by_id,
                            diagnostics=diagnostics,
                            resource_budget=resource_budget,
                        )
                        nodes.extend(image_nodes)
                    table_index += 1
                    continue

                if element.tag == _qn(_W_NS, "sectPr"):
                    continue
                local_name = etree.QName(element).localname
                if local_name in _IGNORED_NONVISUAL_BODY_TAGS:
                    ignored_nonvisual_markers.add(local_name)
                    continue
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_body_element_not_represented",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message=(
                            "A DOCX body element could not be represented without inventing a "
                            f"native locator ({local_name})."
                        ),
                    )
                )

            if ignored_nonvisual_markers:
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_nonvisual_markers_ignored",
                        severity=SourceDiagnosticSeverity.INFO,
                        message=(
                            "Non-visual DOCX body markers were intentionally excluded from "
                            "SourceIR content nodes ("
                            + ", ".join(sorted(ignored_nonvisual_markers))
                            + ")."
                        ),
                    )
                )

            referenced_relationship_ids = _referenced_relationship_ids(document_root)
            for relationship in relationships.values():
                if relationship.relationship_id not in referenced_relationship_ids:
                    continue
                if any(
                    relationship.relationship_type.endswith(suffix)
                    for suffix in _CONTENT_RELATIONSHIP_SUFFIXES
                ):
                    diagnostics.append(
                        SourceDiagnostic(
                            code="docx_related_content_not_parsed",
                            severity=SourceDiagnosticSeverity.WARNING,
                            message=(
                                "A referenced DOCX content relationship is retained only as a "
                                f"diagnostic ({relationship.relationship_type.rsplit('/', 1)[-1]})."
                            ),
                        )
                    )

        return tuple(nodes), tuple(assets_by_id.values()), tuple(diagnostics)

    def _map_table(
        self,
        *,
        element: etree._Element,
        locator: NativeLocator,
        source_sha256: str,
        resource_budget: _DocxResourceBudget,
    ) -> _TableBuild:
        rows = _iter_table_rows(element)
        if not rows:
            return _TableBuild(
                payload=None,
                cell_paragraphs=(),
                diagnostics=(
                    SourceDiagnostic(
                        code="docx_empty_table_not_represented",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message="An empty DOCX table could not satisfy the SourceIR table contract.",
                        native_locator=locator,
                    ),
                ),
            )

        table_id = stable_table_id(
            source_sha256=source_sha256,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            native_locator=locator,
        )
        grid_columns = len(element.findall("./w:tblGrid/w:gridCol", namespaces=_NS))
        diagnostics: list[SourceDiagnostic] = []
        cells: list[_CellBuild] = []
        cell_paragraphs: list[_CellParagraph] = []
        active_vertical_merges: dict[int, _CellBuild] = {}
        observed_columns = 0

        for row_index, row in enumerate(rows):
            column = _integer_property(row, "./w:trPr/w:gridBefore", default=0)
            next_vertical_merges: dict[int, _CellBuild] = {}
            extended_anchors: set[int] = set()
            for cell in _iter_row_cells(row):
                resource_budget.reserve_physical_table_cells()
                for _ in cell.iterfind(".//w:tc", namespaces=_NS):
                    resource_budget.reserve_physical_table_cells()
                column_span = _integer_property(cell, "./w:tcPr/w:gridSpan", default=1)
                if column_span < 1:
                    column_span = 1
                    diagnostics.append(
                        SourceDiagnostic(
                            code="docx_invalid_grid_span",
                            severity=SourceDiagnosticSeverity.WARNING,
                            message="A non-positive DOCX gridSpan was treated as one column.",
                            native_locator=locator,
                        )
                    )
                physical_blocks = _iter_table_cell_blocks(cell)
                raw_text = _table_cell_raw_text(physical_blocks)
                vertical_merge = _vertical_merge_state(cell)
                physical_paragraphs = tuple(
                    block
                    for block in physical_blocks
                    if block.tag == _qn(_W_NS, "p")
                )
                cell_paragraphs.extend(
                    _CellParagraph(
                        element=paragraph,
                        row=row_index,
                        column=column,
                        paragraph_index=paragraph_index,
                    )
                    for paragraph_index, paragraph in enumerate(physical_paragraphs)
                )
                if any(block.tag == _qn(_W_NS, "tbl") for block in physical_blocks):
                    diagnostics.append(
                        SourceDiagnostic(
                            code="docx_nested_table_flattened",
                            severity=SourceDiagnosticSeverity.WARNING,
                            message=(
                                "A nested DOCX table was flattened into its containing cell text; "
                                "the nested grid has no lossless locator in SourceIR v1."
                            ),
                            native_locator=NativeLocator(
                                format=NativeDocumentFormat.DOCX,
                                body_element_index=locator.body_element_index,
                                table_index=locator.table_index,
                                row_index=row_index,
                                column_index=column,
                            ),
                        )
                    )

                if vertical_merge == "continue":
                    anchor = active_vertical_merges.get(column)
                    expected_anchor = all(
                        active_vertical_merges.get(covered_column) is anchor
                        for covered_column in range(column, column + column_span)
                    )
                    if anchor is None or not expected_anchor:
                        diagnostics.append(
                            SourceDiagnostic(
                                code="docx_orphan_vertical_merge",
                                severity=SourceDiagnosticSeverity.WARNING,
                                message=(
                                    "A vertical merge continuation had no matching anchor and was "
                                    "preserved as a standalone cell."
                                ),
                                native_locator=locator,
                            )
                        )
                        anchor = _CellBuild(
                            row=row_index,
                            column=column,
                            column_span=column_span,
                            raw_text=raw_text,
                        )
                        cells.append(anchor)
                    else:
                        anchor_key = id(anchor)
                        if anchor_key not in extended_anchors:
                            anchor.row_span += 1
                            extended_anchors.add(anchor_key)
                        if raw_text:
                            anchor.raw_text = _join_text_blocks(anchor.raw_text, raw_text)
                    for covered_column in range(column, column + column_span):
                        next_vertical_merges[covered_column] = anchor
                else:
                    anchor = _CellBuild(
                        row=row_index,
                        column=column,
                        column_span=column_span,
                        raw_text=raw_text,
                    )
                    cells.append(anchor)
                    if vertical_merge == "restart":
                        for covered_column in range(column, column + column_span):
                            next_vertical_merges[covered_column] = anchor
                column += column_span

            column += _integer_property(row, "./w:trPr/w:gridAfter", default=0)
            observed_columns = max(observed_columns, column)
            active_vertical_merges = next_vertical_merges

        column_count = max(1, grid_columns, observed_columns)
        merged_regions = tuple(
            MergedRegion(
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
            )
            for cell in cells
            if cell.row_span > 1 or cell.column_span > 1
        )
        table_cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, cell.row, cell.column),
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
                raw_text=cell.raw_text,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=locator.body_element_index,
                    table_index=locator.table_index,
                    row_index=cell.row,
                    column_index=cell.column,
                ),
            )
            for cell in cells
        )
        payload = TableIR(
            table_id=table_id,
            row_count=len(rows),
            column_count=column_count,
            cells=table_cells,
            merged_regions=merged_regions,
        )
        return _TableBuild(
            payload=payload,
            cell_paragraphs=tuple(cell_paragraphs),
            diagnostics=tuple(diagnostics),
        )

    def _map_images(
        self,
        *,
        archive: zipfile.ZipFile,
        member_names: frozenset[str],
        content_types: dict[str, str],
        relationships: dict[str, _Relationship],
        paragraph: etree._Element,
        locator: NativeLocator,
        source_sha256: str,
        source_order: int,
        parent_source_node_id: str | None,
        assets_by_id: dict[str, SourceAssetRef],
        diagnostics: list[SourceDiagnostic],
        resource_budget: _DocxResourceBudget,
    ) -> tuple[list[SourceNode], int]:
        image_placements = _image_placements(
            paragraph,
            maximum_count=resource_budget.remaining_image_placements(),
        )
        resource_budget.reserve_image_placements(len(image_placements))
        nodes: list[SourceNode] = []
        for object_index, placement in enumerate(image_placements):
            image_locator = locator.model_copy(update={"object_index": object_index})
            if not placement.renditions:
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_drawing_not_represented",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message=(
                            "A DOCX drawing placement has no image relationship representable "
                            "by this adapter."
                        ),
                        native_locator=image_locator,
                    )
                )
                continue

            rendition = _select_image_rendition(
                placement,
                relationships=relationships,
                member_names=member_names,
            )
            relationship_id = rendition.relationship_id
            if len(placement.renditions) > 1:
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_image_alternate_renditions_collapsed",
                        severity=SourceDiagnosticSeverity.INFO,
                        message=(
                            "A single DOCX drawing placement contained multiple image "
                            "renditions; one deterministic available rendition was retained "
                            "and the alternates were not emitted as independent placements."
                        ),
                        native_locator=image_locator,
                    )
                )
            relationship = relationships.get(relationship_id)
            if relationship is None:
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_image_relationship_missing",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message="An embedded image relationship is missing from document.xml.rels.",
                        native_locator=image_locator,
                    )
                )
                continue
            if relationship.external:
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_external_image_not_captured",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message="An externally linked DOCX image was not copied into SourceIR.",
                        native_locator=image_locator,
                    )
                )
                continue
            if not relationship.relationship_type.endswith(_IMAGE_RELATIONSHIP_SUFFIX):
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_non_image_drawing_relationship",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message="A drawing relationship did not identify an OOXML image part.",
                        native_locator=image_locator,
                    )
                )
                continue
            target = _relationship_member_name(relationship.target)
            if target is None or target not in member_names:
                diagnostics.append(
                    SourceDiagnostic(
                        code="docx_image_part_missing",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message="An embedded DOCX image part is missing or has an unsafe target path.",
                        native_locator=image_locator,
                    )
                )
                continue

            media_part = resource_budget.media_parts.get(target)
            if media_part is None:
                declared_size = archive.getinfo(target).file_size
                resource_budget.reserve_unique_media_bytes(declared_size)
                payload = archive.read(target)
                if len(payload) > declared_size:
                    resource_budget.reserve_unique_media_bytes(len(payload) - declared_size)
                content_sha256 = hashlib.sha256(payload).hexdigest()
                media_type = _media_type(target, content_types)
                media_part = _MediaPartMetadata(
                    content_sha256=content_sha256,
                    asset_id=_stable_reference_id(
                        "asset",
                        source_sha256,
                        target,
                        content_sha256,
                    ),
                    media_type=media_type,
                    storage_key=_logical_media_storage_key(
                        content_sha256=content_sha256,
                        member_name=target,
                        media_type=media_type,
                    ),
                    content_bytes=payload,
                )
                resource_budget.media_parts[target] = media_part
            asset_id = media_part.asset_id
            if asset_id not in assets_by_id:
                asset = SourceAssetRef(
                    asset_id=asset_id,
                    kind=SourceAssetKind.IMAGE,
                    content_sha256=media_part.content_sha256,
                    media_type=media_part.media_type,
                    storage_key=media_part.storage_key,
                    native_locator=image_locator,
                )
                assets_by_id[asset_id] = asset
                self._publish_source_asset(asset, media_part.content_bytes)
            resource_budget.reserve_source_nodes()
            nodes.append(
                self._node(
                    source_sha256=source_sha256,
                    kind=SourceNodeKind.IMAGE,
                    source_order=source_order,
                    raw_text="",
                    locator=image_locator,
                    parent_source_node_id=parent_source_node_id,
                    asset_ref=asset_id,
                )
            )
            source_order += 1
        return nodes, source_order

    def _publish_source_asset(
        self,
        asset: SourceAssetRef,
        content_bytes: bytes,
    ) -> None:
        if self._source_asset_publisher is None:
            return
        if (
            not content_bytes
            or hashlib.sha256(content_bytes).hexdigest() != asset.content_sha256
        ):
            raise RuntimeError("source asset bytes do not match SourceIR identity")
        self._source_asset_publisher(asset, content_bytes)

    def _node(
        self,
        *,
        source_sha256: str,
        kind: SourceNodeKind,
        source_order: int,
        raw_text: str,
        locator: NativeLocator,
        parent_source_node_id: str | None = None,
        table_payload: TableIR | None = None,
        asset_ref: str | None = None,
        docx_list_metadata: DocxListMetadata | None = None,
        docx_inline_span_specs: tuple[_DocxInlineSpanSpec, ...] = (),
    ) -> SourceNode:
        source_node_id = stable_node_id(
            source_sha256=source_sha256,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            kind=kind,
            native_locator=locator,
        )
        return SourceNode(
            source_node_id=source_node_id,
            kind=kind,
            source_order=source_order,
            raw_text=raw_text,
            native_locator=locator,
            parent_source_node_id=parent_source_node_id,
            table_payload=table_payload,
            asset_ref=asset_ref,
            docx_list_metadata=docx_list_metadata,
            docx_inline_spans=tuple(
                DocxInlineSpan(
                    source_node_id=source_node_id,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                    language_evidence=span.language_evidence,
                    boundary_basis=DocxInlineBoundaryBasis.DOCX_RUN_BOUNDARY,
                )
                for span in docx_inline_span_specs
            ),
        )

    def _snapshot(
        self,
        *,
        source_sha256: str,
        created_at: datetime,
        status: SourceIRStatus,
        nodes: tuple[SourceNode, ...] = (),
        assets: tuple[SourceAssetRef, ...] = (),
        diagnostics: tuple[SourceDiagnostic, ...] = (),
    ) -> SourceIRSnapshot:
        document_id = stable_document_id(source_sha256)
        return SourceIRSnapshot(
            source_ir_id=stable_source_ir_id(
                document_id=document_id,
                source_sha256=source_sha256,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
            ),
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            status=status,
            nodes=nodes,
            assets=assets,
            diagnostics=diagnostics,
            created_at=created_at,
        )


def _read_source_bytes(path: Path) -> tuple[bytes, int, str]:
    digest = hashlib.sha256()
    payload = bytearray()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            payload.extend(chunk)
    source_bytes = bytes(payload)
    return source_bytes, len(source_bytes), digest.hexdigest()


def _parse_xml(payload: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
    )
    return etree.fromstring(payload, parser=parser)


def _read_relationships(
    archive: zipfile.ZipFile,
    member_names: frozenset[str],
) -> dict[str, _Relationship]:
    if _DOCUMENT_RELS_XML not in member_names:
        return {}
    root = _parse_xml(archive.read(_DOCUMENT_RELS_XML))
    relationships: dict[str, _Relationship] = {}
    for element in root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
        relationship_id = element.get("Id", "")
        if not relationship_id:
            continue
        relationships[relationship_id] = _Relationship(
            relationship_id=relationship_id,
            relationship_type=element.get("Type", ""),
            target=element.get("Target", ""),
            external=element.get("TargetMode", "").lower() == "external",
        )
    return relationships


def _read_content_types(
    archive: zipfile.ZipFile,
    member_names: frozenset[str],
) -> dict[str, str]:
    if _CONTENT_TYPES_XML not in member_names:
        return {}
    root = _parse_xml(archive.read(_CONTENT_TYPES_XML))
    values: dict[str, str] = {}
    for element in root.findall(f"{{{_CONTENT_TYPE_NS}}}Default"):
        extension = element.get("Extension", "").lower()
        content_type = element.get("ContentType", "")
        if extension and content_type:
            values[f"ext:{extension}"] = content_type
    for element in root.findall(f"{{{_CONTENT_TYPE_NS}}}Override"):
        part_name = element.get("PartName", "").lstrip("/")
        content_type = element.get("ContentType", "")
        if part_name and content_type:
            values[f"part:{part_name}"] = content_type
    return values


def _read_style_kinds(
    archive: zipfile.ZipFile,
    member_names: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    if "word/styles.xml" not in member_names:
        return frozenset(), frozenset()
    root = _parse_xml(archive.read("word/styles.xml"))
    numbered: set[str] = set()
    headings: set[str] = set()
    for style in root.findall("./w:style", namespaces=_NS):
        style_id = style.get(_qn(_W_NS, "styleId"), "")
        if not style_id:
            continue
        if style.find("./w:pPr/w:numPr", namespaces=_NS) is not None:
            numbered.add(style_id)
        style_name = style.find("./w:name", namespaces=_NS)
        name = style_name.get(_qn(_W_NS, "val"), "") if style_name is not None else ""
        if _looks_like_heading(style_id) or _looks_like_heading(name):
            headings.add(style_id)
    return frozenset(numbered), frozenset(headings)


def _iter_body_content(body: etree._Element) -> tuple[tuple[int, etree._Element], ...]:
    """Flatten content controls while retaining the top-level body identity."""

    values: list[tuple[int, etree._Element]] = []

    def append_visible(index: int, element: etree._Element) -> None:
        if element.tag == _qn(_MC_NS, "AlternateContent"):
            for child in _selected_alternate_content_children(element):
                append_visible(index, child)
            return
        if element.tag != _qn(_W_NS, "sdt"):
            values.append((index, element))
            return
        content = element.find("./w:sdtContent", namespaces=_NS)
        if content is None:
            values.append((index, element))
            return
        for child in content:
            append_visible(index, child)

    for body_element_index, element in enumerate(body):
        append_visible(body_element_index, element)
    return tuple(values)


def _paragraph_kind(
    paragraph: etree._Element,
    numbered_styles: frozenset[str],
    heading_styles: frozenset[str],
) -> SourceNodeKind:
    style_element = paragraph.find("./w:pPr/w:pStyle", namespaces=_NS)
    style_id = style_element.get(_qn(_W_NS, "val"), "") if style_element is not None else ""
    if paragraph.find("./w:pPr/w:numPr", namespaces=_NS) is not None or style_id in numbered_styles:
        return SourceNodeKind.LIST_ITEM
    if style_id in heading_styles or _looks_like_heading(style_id):
        return SourceNodeKind.HEADING
    return SourceNodeKind.PARAGRAPH


def _direct_docx_list_metadata(
    paragraph: etree._Element,
    *,
    locator: NativeLocator,
    diagnostics: list[SourceDiagnostic],
) -> DocxListMetadata | None:
    """Map only values written directly on this paragraph's ``w:numPr``."""

    numbering = paragraph.find("./w:pPr/w:numPr", namespaces=_NS)
    if numbering is None:
        return None

    values: dict[str, int] = {}
    invalid_fields: list[str] = []
    for xml_name, field_name in (("numId", "num_id"), ("ilvl", "ilvl")):
        element = numbering.find(f"./w:{xml_name}", namespaces=_NS)
        if element is None:
            continue
        raw_value = element.get(_qn(_W_NS, "val"))
        try:
            value = int(raw_value) if raw_value is not None else -1
        except ValueError:
            value = -1
        if value < 0:
            invalid_fields.append(xml_name)
            continue
        values[field_name] = value

    if invalid_fields:
        diagnostics.append(
            SourceDiagnostic(
                code="docx_direct_list_metadata_invalid",
                severity=SourceDiagnosticSeverity.WARNING,
                message=(
                    "A direct DOCX list property had no valid non-negative integer value ("
                    + ", ".join(invalid_fields)
                    + "); no replacement value was inferred."
                ),
                native_locator=locator,
            )
        )
    return DocxListMetadata(**values) if values else None


def _looks_like_heading(value: str) -> bool:
    folded = value.casefold().replace(" ", "")
    return folded.startswith("heading") or folded.startswith("标题")


def _paragraph_raw_text(paragraph: etree._Element) -> str:
    return _paragraph_raw_text_and_inline_span_specs(paragraph)[0]


def _paragraph_raw_text_and_inline_span_specs(
    paragraph: etree._Element,
) -> tuple[str, tuple[_DocxInlineSpanSpec, ...]]:
    run_segments: list[tuple[etree._Element | None, str]] = []
    for element in _iter_visible_subtree(paragraph):
        piece = ""
        if element.tag == _qn(_W_NS, "t"):
            piece = element.text or ""
        elif element.tag == _qn(_W_NS, "tab"):
            piece = "\t"
        elif element.tag in {_qn(_W_NS, "br"), _qn(_W_NS, "cr")}:
            piece = "\n"
        elif element.tag == _qn(_W_NS, "noBreakHyphen"):
            piece = "-"
        if not piece:
            continue
        run = _nearest_docx_run(element, paragraph=paragraph)
        if run_segments and run_segments[-1][0] is run:
            previous_run, previous_text = run_segments[-1]
            run_segments[-1] = (previous_run, previous_text + piece)
        else:
            run_segments.append((run, piece))

    raw_text = "".join(text for _, text in run_segments)
    if not raw_text:
        return "", ()
    first_zh_run_index = next(
        (
            index
            for index, (_, text) in enumerate(run_segments)
            if _contains_han(text)
        ),
        None,
    )
    if first_zh_run_index is None or first_zh_run_index == 0:
        return raw_text, ()

    prefix = "".join(text for _, text in run_segments[:first_zh_run_index])
    suffix = "".join(text for _, text in run_segments[first_zh_run_index:])
    first_zh_run = run_segments[first_zh_run_index][1]
    first_han_offset = next(
        index for index, character in enumerate(first_zh_run) if _is_han(character)
    )
    if (
        any(character.isalpha() for character in first_zh_run[:first_han_offset])
        or not _strong_english_language_evidence(prefix)
        or _han_character_count(suffix) < 2
    ):
        return raw_text, ()

    split_offset = len(prefix)
    return (
        raw_text,
        (
            _DocxInlineSpanSpec(
                start_offset=0,
                end_offset=split_offset,
                language_evidence=DocxInlineLanguageEvidence.EN,
            ),
            _DocxInlineSpanSpec(
                start_offset=split_offset,
                end_offset=len(raw_text),
                language_evidence=DocxInlineLanguageEvidence.ZH_CN,
            ),
        ),
    )


def _nearest_docx_run(
    element: etree._Element,
    *,
    paragraph: etree._Element,
) -> etree._Element | None:
    current = element.getparent()
    run_tag = _qn(_W_NS, "r")
    while current is not None and current is not paragraph:
        if current.tag == run_tag:
            return current
        current = current.getparent()
    return None


def _strong_english_language_evidence(value: str) -> bool:
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
    return latin_letters >= 12 and len(latin_words) >= 2 and not _contains_han(value)


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


def _iter_table_cell_blocks(cell: etree._Element) -> tuple[etree._Element, ...]:
    """Return cell-level paragraphs/tables in OOXML order through wrappers.

    Word may place visible block content inside ``w:sdtContent``, revision, or
    ``w:customXml`` containers.  Those wrappers do not create a new SourceIR
    boundary, so descend through them.  A nested table remains one block and is
    flattened separately; its paragraphs must not also become direct cell
    paragraph nodes.
    """

    blocks: list[etree._Element] = []
    paragraph_tag = _qn(_W_NS, "p")
    table_tag = _qn(_W_NS, "tbl")
    properties_tag = _qn(_W_NS, "tcPr")

    def visit(parent: etree._Element) -> None:
        for child in _iter_visible_children(parent):
            if child.tag == properties_tag:
                continue
            if child.tag in {paragraph_tag, table_tag}:
                blocks.append(child)
                continue
            visit(child)

    visit(cell)
    return tuple(blocks)


def _iter_table_rows(table: etree._Element) -> tuple[etree._Element, ...]:
    """Return table-owned rows through block wrappers, excluding nested tables."""

    return _iter_wrapped_structural_children(
        table,
        target_tag=_qn(_W_NS, "tr"),
        stop_tags=frozenset({_qn(_W_NS, "tbl")}),
    )


def _iter_row_cells(row: etree._Element) -> tuple[etree._Element, ...]:
    """Return row-owned cells through block wrappers, excluding nested grids."""

    return _iter_wrapped_structural_children(
        row,
        target_tag=_qn(_W_NS, "tc"),
        stop_tags=frozenset({_qn(_W_NS, "tbl"), _qn(_W_NS, "tr")}),
    )


def _iter_wrapped_structural_children(
    parent: etree._Element,
    *,
    target_tag: str,
    stop_tags: frozenset[str],
) -> tuple[etree._Element, ...]:
    values: list[etree._Element] = []

    def visit(container: etree._Element) -> None:
        for child in _iter_visible_children(container):
            if child.tag == target_tag:
                values.append(child)
                continue
            if child.tag in stop_tags:
                continue
            visit(child)

    visit(parent)
    return tuple(values)


def _table_cell_raw_text(physical_blocks: tuple[etree._Element, ...]) -> str:
    text_blocks: list[str] = []
    for block in physical_blocks:
        if block.tag == _qn(_W_NS, "p"):
            text_blocks.append(_paragraph_raw_text(block))
        elif block.tag == _qn(_W_NS, "tbl"):
            nested = [
                _paragraph_raw_text(paragraph)
                for paragraph in _iter_visible_subtree(block)
                if paragraph.tag == _qn(_W_NS, "p")
            ]
            text_blocks.append("\n".join(nested))
    return "\n".join(text_blocks)


def _vertical_merge_state(cell: etree._Element) -> str | None:
    merge = cell.find("./w:tcPr/w:vMerge", namespaces=_NS)
    if merge is None:
        return None
    value = merge.get(_qn(_W_NS, "val"), "continue").casefold()
    return "restart" if value == "restart" else "continue"


def _integer_property(element: etree._Element, path: str, *, default: int) -> int:
    value_element = element.find(path, namespaces=_NS)
    if value_element is None:
        return default
    try:
        return int(value_element.get(_qn(_W_NS, "val"), str(default)))
    except (TypeError, ValueError):
        return default


def _image_placements(
    paragraph: etree._Element,
    *,
    maximum_count: int,
) -> tuple[_ImagePlacement, ...]:
    """Return one record per actual ``w:drawing``/``w:pict`` placement.

    Modern Word can store an SVG and a raster fallback in the same drawing.
    Those are renditions of one placement, not two independently positioned
    images, so they deliberately remain grouped here.
    """

    placement_tags = {_qn(_W_NS, "drawing"), _qn(_W_NS, "pict")}
    values: list[_ImagePlacement] = []
    for placement in _iter_visible_subtree(paragraph):
        if placement.tag not in placement_tags:
            continue
        if len(values) >= maximum_count:
            raise _DocxResourceBudgetExceeded("image-placement budget exceeded")
        renditions: list[_ImageRendition] = []
        for source_order, element in enumerate(_iter_visible_subtree(placement)):
            local_name = etree.QName(element).localname
            if local_name not in {"blip", "imagedata", "svgBlip"}:
                continue
            for attribute_name in (
                _qn(_R_NS, "embed"),
                _qn(_R_NS, "id"),
                _qn(_R_NS, "link"),
            ):
                relationship_id = element.get(attribute_name)
                if relationship_id:
                    renditions.append(
                        _ImageRendition(
                            relationship_id=relationship_id,
                            kind=local_name,
                            source_order=source_order,
                        )
                    )
                    break

        # A package can repeat the same relationship in compatibility markup.
        # Keep only its highest-fidelity occurrence before choosing a rendition.
        unique: dict[str, _ImageRendition] = {}
        for rendition in sorted(renditions, key=_rendition_preference):
            unique.setdefault(rendition.relationship_id, rendition)
        values.append(_ImagePlacement(renditions=tuple(unique.values())))
    return tuple(values)


def _select_image_rendition(
    placement: _ImagePlacement,
    *,
    relationships: dict[str, _Relationship],
    member_names: frozenset[str],
) -> _ImageRendition:
    def availability(rendition: _ImageRendition) -> int:
        relationship = relationships.get(rendition.relationship_id)
        if relationship is None:
            return 1
        target = _relationship_member_name(relationship.target)
        if (
            not relationship.external
            and relationship.relationship_type.endswith(_IMAGE_RELATIONSHIP_SUFFIX)
            and target is not None
            and target in member_names
        ):
            return 0
        return 1

    return min(
        placement.renditions,
        key=lambda rendition: (availability(rendition), *_rendition_preference(rendition)),
    )


def _rendition_preference(rendition: _ImageRendition) -> tuple[int, int]:
    # Prefer the vector rendition when Word provides SVG plus a raster fallback.
    kind_priority = {"svgBlip": 0, "blip": 1, "imagedata": 2}
    return kind_priority.get(rendition.kind, 3), rendition.source_order


def _referenced_relationship_ids(root: etree._Element) -> frozenset[str]:
    relationship_attributes = {
        _qn(_R_NS, "embed"),
        _qn(_R_NS, "link"),
        _qn(_R_NS, "id"),
    }
    return frozenset(
        value
        for element in _iter_visible_subtree(root)
        for attribute, value in element.attrib.items()
        if attribute in relationship_attributes and value
    )


def _iter_visible_subtree(root: etree._Element) -> Iterator[etree._Element]:
    """Depth-first OOXML traversal selecting one Markup Compatibility branch."""

    yield root
    for child in _iter_visible_children(root):
        yield from _iter_visible_subtree(child)


def _iter_visible_children(parent: etree._Element) -> Iterator[etree._Element]:
    alternate_content_tag = _qn(_MC_NS, "AlternateContent")
    for child in parent:
        if child.tag == alternate_content_tag:
            yield from _selected_alternate_content_children(child)
        else:
            yield child


def _selected_alternate_content_children(
    alternate_content: etree._Element,
) -> tuple[etree._Element, ...]:
    """Return children from the first supported Choice or its Fallback."""

    selected = _select_alternate_content_branch(alternate_content)
    if selected is None:
        return ()
    return tuple(_iter_visible_children(selected))


def _select_alternate_content_branch(
    alternate_content: etree._Element,
) -> etree._Element | None:
    """Apply the OOXML Markup Compatibility Choice/Fallback rule.

    ``mc:Requires`` contains namespace prefixes. A Choice is understood only
    when every prefix resolves and its namespace is in the adapter's explicit
    capability set. Choice order is significant; Fallback is used only when no
    Choice can be processed.
    """

    choice_tag = _qn(_MC_NS, "Choice")
    fallback_tag = _qn(_MC_NS, "Fallback")
    for child in alternate_content:
        if child.tag != choice_tag:
            continue
        required_prefixes = tuple(child.get("Requires", "").split())
        if required_prefixes and all(
            child.nsmap.get(prefix) in _SUPPORTED_MC_REQUIREMENT_NAMESPACES
            for prefix in required_prefixes
        ):
            return child
    return next(
        (child for child in alternate_content if child.tag == fallback_tag),
        None,
    )


def _count_unrepresented_alternate_content(root: etree._Element) -> int:
    """Count only unresolved AlternateContent on branches visible to us."""

    alternate_content_tag = _qn(_MC_NS, "AlternateContent")

    def count(element: etree._Element) -> int:
        if element.tag == alternate_content_tag:
            selected = _select_alternate_content_branch(element)
            if selected is None:
                return 1
            return sum(count(child) for child in selected)
        return sum(count(child) for child in element)

    return count(root)


def _relationship_member_name(target: str) -> str | None:
    normalized_target = target.replace("\\", "/")
    if not normalized_target or normalized_target.startswith("/"):
        return None
    member = posixpath.normpath(posixpath.join("word", normalized_target))
    if member == ".." or member.startswith("../"):
        return None
    return member


def _media_type(member: str, content_types: dict[str, str]) -> str:
    override = content_types.get(f"part:{member}")
    if override:
        return override
    extension = member.rsplit(".", 1)[-1].lower() if "." in member else ""
    declared = content_types.get(f"ext:{extension}")
    if declared:
        return declared
    guessed, _ = mimetypes.guess_type(member)
    return guessed or "application/octet-stream"


def _logical_media_storage_key(
    *,
    content_sha256: str,
    member_name: str,
    media_type: str,
) -> str:
    extension = Path(member_name).suffix.lower().lstrip(".")
    if not extension or not extension.isalnum() or len(extension) > 10:
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/tiff": "tif",
            "image/svg+xml": "svg",
        }.get(media_type.lower(), "bin")
    return f"source-assets/{content_sha256[:2]}/{content_sha256}.{extension}"


def _stable_reference_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


def _join_text_blocks(first: str, second: str) -> str:
    if not first:
        return second
    if not second:
        return first
    return f"{first}\n{second}"


def _qn(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


__all__ = ["DocxSourceIRAdapter"]
