from __future__ import annotations

import json
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from app.application.build_source_ir import build_source_ir
from app.application.extraction.ingest_document import inspect_document_for_ingestion
from app.domain.extraction.preflight import DocumentFormat
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNodeKind,
    TableIR,
)
from app.domain.extraction.text import decode_plain_text_strict


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentContent:
    file_type: str
    extracted_text: str


def extract_knowledge_document_content(
    source_path: str | PathLike[str],
    *,
    expected_sha256: str,
) -> KnowledgeDocumentContent:
    """Build the retrieval text projection from one verified durable source."""

    path = Path(source_path)
    report = inspect_document_for_ingestion(path)
    normalized_expected = str(expected_sha256 or "").strip().lower()
    if not normalized_expected or report.source_sha256 != normalized_expected:
        raise RuntimeError("Knowledge source identity changed before parsing.")

    detected_format = report.identity.detected_format
    if detected_format is DocumentFormat.TXT:
        extracted_text = decode_plain_text_strict(path.read_bytes())
    else:
        source_ir = build_source_ir(path, preflight_report=report)
        if source_ir.status is SourceIRStatus.FAILED:
            raise RuntimeError("Formal knowledge SourceIR extraction failed.")
        extracted_text = source_ir_text_projection(source_ir)

    return KnowledgeDocumentContent(
        file_type=detected_format.value.upper(),
        extracted_text=extracted_text,
    )


def source_ir_text_projection(snapshot: SourceIRSnapshot) -> str:
    """Create a deterministic, non-semantic text view for retrieval chunking."""

    parts: list[str] = []
    for node in snapshot.nodes:
        if node.parent_source_node_id is not None:
            continue
        if node.kind is SourceNodeKind.TABLE:
            if node.table_payload is not None:
                parts.extend(
                    _table_markdown_lines(
                        node.table_payload,
                        native_locator=node.native_locator,
                    )
                )
            continue
        text = node.raw_text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _table_markdown_lines(
    table: TableIR,
    *,
    native_locator: NativeLocator,
) -> list[str]:
    """Project one native table as a deterministic Markdown table.

    The first row is generated locator metadata, not source cell content. It
    keeps the native locator inside the table chunk recognized by the current
    RAG chunker. The blank continuation columns and separator row preserve the
    source table's physical column count without inventing column headings.
    """

    rows = [[""] * table.column_count for _ in range(table.row_count)]
    for cell in sorted(
        table.cells,
        key=lambda item: (item.row, item.column, item.cell_id),
    ):
        if _is_covered_merged_cell(table, row=cell.row, column=cell.column):
            continue
        candidates = (cell.display_value, cell.raw_text, cell.formula)
        value = next(
            (
                _escape_markdown_cell(str(candidate))
                for candidate in candidates
                if candidate is not None and str(candidate).strip()
            ),
            "",
        )
        # A merged cell is represented once at its anchor. Covered positions
        # stay empty so retrieval text cannot duplicate the source fact.
        rows[cell.row][cell.column] = value

    locator_header = [
        _escape_markdown_cell(
            _native_table_locator_label(
                native_locator,
                table_id=table.table_id,
            )
        )
    ]
    locator_header.extend("" for _ in range(table.column_count - 1))
    return [
        _markdown_row(locator_header),
        _markdown_row(["---"] * table.column_count),
        *(_markdown_row(row) for row in rows),
    ]


def _is_covered_merged_cell(table: TableIR, *, row: int, column: int) -> bool:
    for region in table.merged_regions:
        in_region = (
            region.row <= row < region.row + region.row_span
            and region.column <= column < region.column + region.column_span
        )
        if in_region and (row, column) != (region.row, region.column):
            return True
    return False


def _native_table_locator_label(
    locator: NativeLocator,
    *,
    table_id: str,
) -> str:
    parts = [
        f"table_id={table_id}",
        f"format={locator.format.value.upper()}",
    ]
    if locator.format is NativeDocumentFormat.PDF:
        if locator.page_number is not None:
            parts.append(f"page={locator.page_number}")
        if locator.table_index is not None:
            parts.append(f"table_index={locator.table_index}")
    elif locator.format is NativeDocumentFormat.DOCX:
        if locator.body_element_index is not None:
            parts.append(f"body_element_index={locator.body_element_index}")
        if locator.table_index is not None:
            parts.append(f"table_index={locator.table_index}")
    elif locator.format is NativeDocumentFormat.XLSX:
        if locator.workbook is not None:
            parts.append(
                "workbook="
                + json.dumps(locator.workbook, ensure_ascii=False, separators=(",", ":"))
            )
        if locator.sheet is not None:
            parts.append(
                "sheet="
                + json.dumps(locator.sheet, ensure_ascii=False, separators=(",", ":"))
            )
        if locator.cell_range is not None:
            parts.append(f"range={locator.cell_range}")
    return "[source_locator " + " ".join(parts) + "]"


def _escape_markdown_cell(value: str) -> str:
    normalized = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\\", "\\\\").replace("|", "\\|")
    return normalized.replace("\n", "<br>")


def _markdown_row(values: list[str]) -> str:
    return "| " + " | ".join(values) + " |"


__all__ = [
    "KnowledgeDocumentContent",
    "extract_knowledge_document_content",
    "source_ir_text_projection",
]
