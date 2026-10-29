"""Local Parser Lab receipts. SourceIR remains the only normalized fact model.

Every invocation has a new directory. Raw observations and source bytes are write
once; review edits never go through this store. No model, DB, or network is used.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any
from uuid import uuid4

from reqpilotx.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from reqpilotx.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx.adapters.documents.tesseract_source_ir_adapter import TesseractDependencyMissing
from reqpilotx.application.build_source_ir import build_source_ir
from reqpilotx.domain.extraction.source_ir import SourceAssetRef

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_PAGES = 100
# PyMuPDF table detection has process-global mutable state. Serialize lab runs.
_PARSE_LOCK = threading.Lock()


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def parser_catalog() -> list[dict]:
    return [
        {"id": "pymupdf", "name": "PyMuPDF", "version": _version("pymupdf"),
         "availability": "READY" if _version("pymupdf") else "MISSING_DEPENDENCY"},
        {"id": "pdfplumber", "name": "pdfplumber", "version": _version("pdfplumber"),
         "availability": "READY" if _version("pdfplumber") else "MISSING_DEPENDENCY"},
        {"id": "tesseract", "name": "Tesseract OCR", "version": _tesseract_version(),
         "availability": "READY" if shutil.which("tesseract") else "MISSING_DEPENDENCY"},
        {"id": "docling", "name": "Docling", "version": _version("docling"),
         "availability": "UNSUPPORTED", "dependency_status": "READY" if _version("docling") else "MISSING_DEPENDENCY",
         "reason": "Docling→SourceIR adapter 尚未实现；不自动下载模型。"},
    ]


def _tesseract_version() -> str | None:
    binary = shutil.which("tesseract")
    if not binary:
        return None
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, timeout=10)
        return result.stdout.decode("utf-8", errors="replace").splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def _raw_json(value: Any) -> Any:
    """Lossless bytes encoding; geometry objects retain their ordered values."""
    if isinstance(value, bytes):
        return {"encoding": "base64", "bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): _raw_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_raw_json(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if type(value).__module__.startswith("pdfminer"):
        if hasattr(value, "get_rawdata"):
            return {"pdf_stream_attributes": _raw_json(value.attrs), "raw_stream": _raw_json(value.get_rawdata())}
        if hasattr(value, "objid"):
            return {"pdf_object_ref": value.objid}
        if hasattr(value, "name"):
            return {"pdf_literal": _raw_json(value.name)}
    if hasattr(value, "tables"):
        return {"tables": [{"bbox": _raw_json(t.bbox), "row_count": t.row_count,
            "col_count": t.col_count, "matrix": _raw_json(t.extract()),
            "rows": [{"bbox": _raw_json(row.bbox), "cells": _raw_json(row.cells)} for row in t.rows],
            "header": _raw_json({key: getattr(t.header, key, None)
                for key in ("bbox", "cells", "names", "external")}) if hasattr(t, "header") else None,
            } for t in value.tables]}
    try:
        return [_raw_json(v) for v in value]
    except TypeError as exc:
        raise TypeError(f"Unsupported raw evidence type: {type(value).__name__}") from exc


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_raw_json(value), ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


class ParserLabStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise FileNotFoundError("Unknown Parser Lab run")
        directory = self.root / run_id
        if not directory.is_dir():
            raise FileNotFoundError("Unknown Parser Lab run")
        return directory

    def list_runs(self) -> list[dict]:
        records = []
        for path in self.root.glob("*/run.json"):
            if re.fullmatch(r"[0-9a-f]{32}", path.parent.name):
                records.append(json.loads(path.read_bytes()))
        return sorted(records, key=lambda r: r["started_at"], reverse=True)

    def get(self, run_id: str) -> dict:
        directory = self._directory(run_id)
        record = json.loads((directory / "run.json").read_bytes())
        source_ir = directory / "source-ir.json"
        record["source_ir"] = json.loads(source_ir.read_bytes()) if source_ir.is_file() else None
        return record

    def artifact(self, run_id: str, artifact_id: str) -> tuple[Path, str]:
        record = self.get(run_id)
        artifact = next((a for a in record["artifacts"] if a["id"] == artifact_id), None)
        if artifact is None:
            raise FileNotFoundError("Unknown Parser Lab artifact")
        path = (self._directory(run_id) / artifact["path"]).resolve()
        if not path.is_relative_to(self._directory(run_id)) or not path.is_file():
            raise FileNotFoundError("Missing Parser Lab artifact")
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("Parser Lab artifact hash mismatch")
        return path, artifact["media_type"]

    def run(self, content: bytes, *, filename: str, parser: str, language: str = "eng") -> dict:
        catalog = {p["id"]: p for p in parser_catalog()}
        if parser not in catalog or language not in {"eng", "chi_sim", "eng+chi_sim"}:
            raise ValueError("Unsupported parser or OCR language")
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("PDF must contain 1 byte to 25 MiB")
        run_id = uuid4().hex
        directory = self.root / run_id
        directory.mkdir(mode=0o700)
        now = datetime.now(timezone.utc)
        record = {"schema_version": "parser-lab-run/v1", "run_id": run_id,
            "filename": Path(filename).name[:200], "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_size": len(content), "parser": parser, "parser_version": catalog[parser]["version"],
            "adapter_id": None, "adapter_version": None, "mapping_version": "parser-lab/v1",
            "config": {"ocr_language": language, "ocr_psm": 3, "render_scale": 1.25,
                "max_pages": MAX_PAGES, "remote_services": False},
            "status": "RUNNING", "started_at": now.isoformat(), "finished_at": None,
            "pages": [], "diagnostics": [], "artifacts": [], "counts": {}}
        assets: dict[str, str] = {}

        def write(relative: str, data: bytes, media: str, artifact_id: str | None = None):
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as target:
                target.write(data)
            identifier = artifact_id or uuid4().hex
            record["artifacts"].append({"id": identifier, "path": relative, "media_type": media,
                "byte_count": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            return identifier

        def receipt():
            temporary = directory / "run.pending"
            temporary.write_bytes(_json_bytes(record))
            temporary.replace(directory / "run.json")

        def asset_sink(asset: SourceAssetRef, data: bytes):
            if asset.asset_id not in assets:
                if hashlib.sha256(data).hexdigest() != asset.content_sha256:
                    raise ValueError("asset hash mismatch")
                assets[asset.asset_id] = write(asset.storage_key, data, asset.media_type, asset.asset_id)

        evidence_index = 0
        def observer(page: int, method: str, config: dict, result: Any):
            nonlocal evidence_index
            evidence_index += 1
            write(f"raw/{evidence_index:06d}-page-{page}-{method}.json",
                _json_bytes({"page_number": page, "method": method, "config": config, "output": result}),
                "application/json")

        started = time.perf_counter()
        write("source.pdf", content, "application/pdf", "source")
        receipt()
        try:
            if catalog[parser]["availability"] != "READY":
                record["status"] = catalog[parser]["availability"]
                record["diagnostics"].append({"code": record["status"], "message":
                    catalog[parser].get("reason", f"缺少本地依赖 {parser}")})
                return self._finish(record, receipt, started)
            source = directory / "source.pdf"
            preflight = LocalDocumentPreflight().inspect(source)
            write("preflight.json", _json_bytes(asdict(preflight)), "application/json", "preflight")
            if not preflight.processable:
                record["status"] = "FAILED"
                record["diagnostics"].extend({"code": d.code.value, "message": d.message} for d in preflight.diagnostics)
                return self._finish(record, receipt, started)
            import pymupdf
            with _PARSE_LOCK:
                with pymupdf.open(source) as document:
                    if document.page_count > MAX_PAGES:
                        raise ValueError(f"Page limit exceeded: {document.page_count} > {MAX_PAGES}")
                    for index, page in enumerate(document):
                        # Match the existing adapter: top-left, rotated page.rect points.
                        rect = list(page.rect)
                        pixels = (page.rect.width * 1.25) * (page.rect.height * 1.25)
                        if pixels > 20_000_000:
                            raise ValueError("Page render pixel budget exceeded")
                        render = page.get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), alpha=False, annots=False)
                        render_id = write(f"pages/{index+1}.png", render.tobytes("png"), "image/png")
                        record["pages"].append({"page_number": index+1, "bbox": rect,
                            "width": page.rect.width, "height": page.rect.height,
                            "rotation": page.rotation, "cropbox": list(page.cropbox), "mediabox": list(page.mediabox),
                            "rotation_matrix": list(page.rotation_matrix), "derotation_matrix": list(page.derotation_matrix),
                            "pdf_to_mupdf_matrix": list(page.transformation_matrix),
                            "coordinate_space": "rotated-page-top-left-points", "render_artifact": render_id,
                            "render_width": render.width, "render_height": render.height})
                if parser == "pymupdf":
                    adapter = PdfSourceIRAdapter(source_asset_publisher=asset_sink, native_evidence_observer=observer)
                    source_ir = build_source_ir(source, preflight_report=preflight, adapters=(adapter,), clock=lambda: now)
                elif parser == "pdfplumber":
                    from reqpilotx.adapters.documents.pdfplumber_source_ir_adapter import extract_pdfplumber
                    source_ir = extract_pdfplumber(source, preflight=preflight, created_at=now, observer=observer)
                else:
                    from reqpilotx.adapters.documents.tesseract_source_ir_adapter import extract_tesseract
                    source_ir = extract_tesseract(source, preflight=preflight, created_at=now,
                        language=language, observer=observer)
            record["adapter_id"] = source_ir.adapter_id
            record["adapter_version"] = source_ir.adapter_version
            record["status"] = source_ir.status.value
            record["counts"] = dict(Counter(n.kind.value for n in source_ir.nodes))
            record["diagnostics"].extend(d.model_dump(mode="json") for d in source_ir.diagnostics)
            for asset in source_ir.assets:
                if asset.asset_id not in assets:
                    record["status"] = "PARTIAL" if record["status"] != "FAILED" else "FAILED"
                    record["diagnostics"].append({"code": "ASSET_MISSING", "message": asset.asset_id})
            write("source-ir.json", source_ir.model_dump_json(indent=2).encode(), "application/json", "source-ir")
            write("page-geometry.json", _json_bytes(record["pages"]), "application/json", "geometry")
        except Exception as exc:
            record["status"] = "MISSING_DEPENDENCY" if isinstance(exc, (ImportError, TesseractDependencyMissing)) else "FAILED"
            record["diagnostics"].append({"code": type(exc).__name__, "message": str(exc)[:1000]})
        return self._finish(record, receipt, started)

    def _finish(self, record, receipt, started):
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        receipt()
        return self.get(record["run_id"])
