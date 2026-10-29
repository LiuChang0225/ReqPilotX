"""Bounded native pdfplumber facts for local comparison, without AI fallback."""
from __future__ import annotations

from datetime import datetime
import hashlib
from importlib.metadata import version
from pathlib import Path

from reqpilotx.domain.extraction.source_ir import (
    NativeDocumentFormat, NativeLocator, SourceDiagnostic, SourceDiagnosticSeverity,
    SourceIRSnapshot, SourceIRStatus, SourceNode, SourceNodeKind, TableIR, TableCellIR,
    stable_document_id, stable_source_ir_id, stable_node_id, stable_table_id, stable_cell_id,
)


def extract_pdfplumber(source: Path, *, preflight, created_at: datetime, observer) -> SourceIRSnapshot:
    import pdfplumber
    import pymupdf
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if not preflight.processable or digest != preflight.identity.source_sha256:
        raise ValueError("pdfplumber source changed or preflight rejected")
    adapter_id, adapter_version = "pdfplumber-native", "lab1-" + version("pdfplumber")
    document_id = stable_document_id(digest)
    identity = dict(source_sha256=digest, adapter_id=adapter_id, adapter_version=adapter_version)
    nodes, diagnostics = [], []

    def diagnostic(code, message, page):
        diagnostics.append(SourceDiagnostic(code=code, severity=SourceDiagnosticSeverity.WARNING,
            message=message, native_locator=NativeLocator(format=NativeDocumentFormat.PDF,
                page_number=page, bbox=tuple(geometry[page-1].rect))))

    with pdfplumber.open(source) as document, pymupdf.open(source) as geometry:
        for number, page in enumerate(document.pages, 1):
            lines = page.extract_text_lines()
            tables = page.find_tables()
            matrices = [table.extract() for table in tables]
            observer(number, "pdfplumber", {}, {"chars": page.chars, "images": page.images,
                "lines": lines, "tables": [{"bbox": table.bbox, "matrix": matrix,
                    "rows": [row.cells for row in table.rows]} for table, matrix in zip(tables, matrices)]})
            native = geometry[number-1]
            # pdfplumber uses the media-box origin; avoid inventing a transform
            # for rotated/cropped pages until that mapping has dedicated evidence.
            if native.rotation or native.cropbox != native.mediabox:
                diagnostic("pdfplumber_geometry_unsupported", "旋转或裁剪页尚未映射；原始输出已保留。", number)
                continue
            left, top, _, _ = page.bbox
            def locator(box, index, **path):
                return NativeLocator(format=NativeDocumentFormat.PDF, page_number=number,
                    block_index=index if "table_index" not in path else None,
                    bbox=(float(box[0]-left), float(box[1]-top), float(box[2]-left), float(box[3]-top)), **path)
            pending = []
            accepted_boxes = []
            for index, (table, matrix) in enumerate(zip(tables, matrices)):
                if not matrix or not matrix[0] or len(matrix) * len(matrix[0]) > 100_000:
                    diagnostic("pdfplumber_table_invalid", "表格网格为空或超出预算。", number)
                    continue
                if any(box is None for row in table.rows for box in row.cells):
                    diagnostic("pdfplumber_merged_table_unsupported", "合并单元格拓扑未实现；保留原始网格与文字，不冒称完整表格。", number)
                    continue
                where = locator(table.bbox, index, table_index=index)
                table_id = stable_table_id(**identity, native_locator=where)
                cells = tuple(TableCellIR(cell_id=stable_cell_id(table_id, row, column), row=row, column=column,
                    raw_text=text or "", native_locator=locator(table.rows[row].cells[column], index,
                        table_index=index, row_index=row, column_index=column))
                    for row, values in enumerate(matrix) for column, text in enumerate(values))
                payload = TableIR(table_id=table_id, row_count=len(matrix), column_count=len(matrix[0]), cells=cells)
                pending.append((where.bbox[1], where.bbox[0], SourceNode(
                    source_node_id=stable_node_id(**identity, kind=SourceNodeKind.TABLE, native_locator=where),
                    kind=SourceNodeKind.TABLE, source_order=0, native_locator=where, table_payload=payload)))
                accepted_boxes.append(table.bbox)
            for index, line in enumerate(lines, len(tables)):
                box = (line["x0"], line["top"], line["x1"], line["bottom"])
                if any(b[0] <= box[0] and b[1] <= box[1] and b[2] >= box[2] and b[3] >= box[3] for b in accepted_boxes):
                    continue
                where = locator(box, index)
                pending.append((where.bbox[1], where.bbox[0], SourceNode(
                    source_node_id=stable_node_id(**identity, kind=SourceNodeKind.PARAGRAPH, native_locator=where),
                    kind=SourceNodeKind.PARAGRAPH, source_order=0, native_locator=where, raw_text=line["text"])))
            for _, _, node in sorted(pending, key=lambda n: (n[0], n[1])):
                nodes.append(node.model_copy(update={"source_order": len(nodes)}))
            if page.images:
                diagnostic("pdfplumber_image_assets_missing", "检测到图片区域，但尚未提取真实资产；未创建 IMAGE 节点。", number)
            if page.curves or page.edges:
                diagnostic("pdfplumber_vector_semantics_unsupported", "矢量图形未识别为图片资产。", number)
            if not lines and not tables:
                diagnostic("pdfplumber_no_native_text", "此页没有提取到原生文本或表格；未自动运行 OCR。", number)
    return SourceIRSnapshot(**identity, document_id=document_id,
        source_ir_id=stable_source_ir_id(document_id=document_id, **identity),
        status=SourceIRStatus.PARTIAL if diagnostics else SourceIRStatus.COMPLETE,
        nodes=tuple(nodes), diagnostics=tuple(diagnostics), created_at=created_at)
