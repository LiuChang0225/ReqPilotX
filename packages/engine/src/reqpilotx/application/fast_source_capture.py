"""Portable, local-only source capture before semantic document decisions.

This entry point deliberately does not import the database, model providers,
project runtime or a customer-specific extraction implementation. It returns
the same SourceIR used by the application, plus optional diagnostic structure
proposals. An accepted Document Map and SourceItem ledger remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from reqpilotx.adapters.documents.docx_source_ir_adapter import DocxSourceIRAdapter
from reqpilotx.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from reqpilotx.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx.adapters.documents.xlsb_source_ir_adapter import XlsbSourceIRAdapter
from reqpilotx.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
from reqpilotx.application.build_source_ir import build_source_ir
from reqpilotx.domain.extraction.source_ir import SourceAssetRef, SourceDiagnosticSeverity, SourceIRSnapshot, SourceIRStatus


FAST_SOURCE_CAPTURE_SCHEMA = "reqpilotx-fast-source-capture/v1"
_VISUAL_GAP_CODES = frozenset({
    "pdf_full_page_image_with_invisible_text_layer",
    "pdf_full_page_image_without_visible_text",
    "pdf_vector_text_with_low_native_unicode_coverage",
    "pdf_visual_text_provider_not_configured",
    "pdf_page_without_locatable_content",
})


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _capture_gaps(snapshot: SourceIRSnapshot) -> list[dict[str, Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for diagnostic in snapshot.diagnostics:
        if diagnostic.severity is SourceDiagnosticSeverity.ERROR or diagnostic.code in _VISUAL_GAP_CODES:
            groups[diagnostic.code].append(diagnostic)
    return [
        {
            "code": code,
            "pages": sorted({
                item.native_locator.page_number
                for item in items
                if item.native_locator is not None and item.native_locator.page_number is not None
            }),
            "occurrences": len(items),
            "message": items[0].message,
        }
        for code, items in sorted(groups.items())
    ]


def _page_facts(snapshot: SourceIRSnapshot, page_count: int | None) -> list[dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {
        page: {"page": page, "native_text_characters": 0, "nodes": 0, "tables": 0, "images": 0}
        for page in range(1, (page_count or 0) + 1)
    }
    for node in snapshot.nodes:
        page = node.native_locator.page_number
        if page is None:
            continue
        fact = pages.setdefault(page, {"page": page, "native_text_characters": 0, "nodes": 0, "tables": 0, "images": 0})
        fact["nodes"] += 1
        # Table text is a parallel structural channel, so do not count it
        # again as captured source prose or treat rows as requirements.
        if node.table_payload is None:
            fact["native_text_characters"] += len(node.raw_text)
        fact["tables"] += int(node.table_payload is not None)
        fact["images"] += int(node.asset_ref is not None)
    return [pages[key] for key in sorted(pages)]


def capture_fast_source(
    source_path: str | Path,
    *,
    output_dir: str | Path,
    expected_sha256: str | None = None,
    include_structure: bool = True,
    progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Capture a file into a new directory without network or semantic work.

    The caller owns ingress authority (customer requirements vs internal
    knowledge). This function never guesses that authority from a filename.
    Partial captures are usable evidence with explicit gaps, not a completed
    source ledger. The directory is never reused or silently overwritten.
    """

    started = time.perf_counter()
    source = Path(source_path).expanduser().resolve(strict=True)
    report = LocalDocumentPreflight().inspect(source)
    if expected_sha256 is not None and report.identity.source_sha256 != expected_sha256:
        raise ValueError("source_sha256_mismatch")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    timings: dict[str, float] = {"preflight": time.perf_counter() - started}
    assets: dict[str, dict[str, Any]] = {}

    def publish(asset: SourceAssetRef, content: bytes) -> None:
        if hashlib.sha256(content).hexdigest() != asset.content_sha256:
            raise ValueError("source_asset_sha256_mismatch")
        relative = Path("assets") / asset.storage_key
        path = destination / relative
        if not path.is_relative_to(destination):
            raise ValueError("source_asset_path_escape")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != content:
            raise ValueError("source_asset_path_collision")
        path.write_bytes(content)
        assets[asset.asset_id] = {"asset_id": asset.asset_id, "path": relative.as_posix(), "sha256": asset.content_sha256}

    capture_started = time.perf_counter()
    snapshot = build_source_ir(
        source,
        preflight_report=report,
        adapters=(
            PdfSourceIRAdapter(source_asset_publisher=publish, page_progress_callback=progress),
            DocxSourceIRAdapter(source_asset_publisher=publish),
            XlsxSourceIRAdapter(source_asset_publisher=publish),
            XlsbSourceIRAdapter(source_asset_publisher=publish),
        ),
    )
    timings["capture"] = time.perf_counter() - capture_started
    artifacts: dict[str, Any] = {"source_ir": "source_ir.json", "assets": list(assets.values())}
    # Keep the original file available even when native text is absent or a
    # vector/raster page cannot be promoted to readable content. Recheck bytes
    # instead of letting a changed source enter the completed evidence bundle.
    original_bytes = source.read_bytes()
    if hashlib.sha256(original_bytes).hexdigest() != snapshot.source_sha256:
        raise ValueError("source_changed_before_bundle_copy")
    source_copy = "source" + source.suffix.lower()
    (destination / source_copy).write_bytes(original_bytes)
    artifacts["source_document"] = source_copy
    if report.identity.detected_format.value == "pdf":
        import pymupdf

        outline_started = time.perf_counter()
        with pymupdf.open(stream=original_bytes, filetype="pdf") as pdf:
            bookmarks = [
                {"native_order": order, "level": level, "title": title, "page_number": page if page > 0 else None}
                for order, (level, title, page) in enumerate(pdf.get_toc())
            ]
        _write_json(destination / "native_outline.json", {
            "schema_version": "reqpilotx-native-pdf-outline/v1",
            "source_sha256": snapshot.source_sha256,
            "origin": "embedded_pdf_bookmarks",
            "status": "source_metadata_unconfirmed_as_document_map",
            "nodes": bookmarks,
        })
        timings["native_outline"] = time.perf_counter() - outline_started
        artifacts["native_outline"] = "native_outline.json"
        from reqpilotx.application.pdf_annotations import capture_pdf_annotations
        annotations = capture_pdf_annotations(source, expected_sha256=snapshot.source_sha256)
        _write_json(destination / "pdf-annotations.json", annotations)
        artifacts["pdf_annotations"] = "pdf-annotations.json"
    write_started = time.perf_counter()
    (destination / "source_ir.json").write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    timings["source_ir_write"] = time.perf_counter() - write_started
    structure_counts: dict[str, int] | None = None
    if include_structure and snapshot.status is not SourceIRStatus.FAILED:
        from reqpilotx.application.extraction.inspect_structure import inspect_structure
        from reqpilotx.application.extraction.resolve_structure import build_structure_tree

        structure_started = time.perf_counter()
        structure = build_structure_tree(snapshot)
        timings["structure_proposal"] = time.perf_counter() - structure_started
        (destination / "structure.json").write_text(structure.model_dump_json(indent=2) + "\n", encoding="utf-8")
        artifacts["structure_proposal"] = "structure.json"
        structure_counts = dict(Counter(node.kind.value for node in structure.nodes))
        inspection_started = time.perf_counter()
        inspection = inspect_structure(snapshot, structure)
        timings["structure_inspection"] = time.perf_counter() - inspection_started
        (destination / "inspection.json").write_text(inspection.model_dump_json(indent=2) + "\n", encoding="utf-8")
        artifacts["structure_inspection"] = "inspection.json"

    timings["total"] = time.perf_counter() - started
    manifest: dict[str, Any] = {
        "schema_version": FAST_SOURCE_CAPTURE_SCHEMA,
        "mode": "local_native_algorithms",
        "source": {"name": source.name, "sha256": snapshot.source_sha256, "size_bytes": report.identity.source_size, "format": report.identity.detected_format.value, "page_count": report.page_count},
        "source_ir_id": snapshot.source_ir_id,
        "source_ir_status": snapshot.status.value,
        "semantic_status": {"document_map": "not_decided", "source_items": "not_decided", "classification": "not_run", "decomposition": "not_run"},
        "artifacts": artifacts,
        "timings_seconds": timings,
        "capture_counts": dict(Counter(node.kind.value for node in snapshot.nodes)),
        "structure_proposal_counts": structure_counts,
        "page_facts": _page_facts(snapshot, report.page_count),
        "gaps": _capture_gaps(snapshot),
        "diagnostic_counts": dict(Counter(item.code for item in snapshot.diagnostics)),
        "interpretation": [
            "SourceIR COMPLETE describes native capture execution, not complete human-readable content or accepted semantics.",
            "PDF SourceIR order is native paint order; its table channel overlaps raw text. It is not the final ledger reading order.",
            "StructureTree is an unaccepted algorithmic proposal. Physical table rows and transport containers are not source chapters or requirements.",
            "Keep existing accepted Document Map and SourceItem identities. Only an explicit source-bound decision may publish a successor.",
            "No OCR or model provider was constructed. Raster, vector-glyph and untrusted hidden-text pages retain original evidence and explicit gaps.",
        ],
    }
    _write_json(destination / "manifest.json", manifest)
    return manifest


__all__ = ["FAST_SOURCE_CAPTURE_SCHEMA", "capture_fast_source"]
