"""Local OCR sidecars. Native text is never replaced or relabelled as OCR."""
from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from reqpilotx.domain.extraction.source_ir import (
    SourceIRSnapshot, SourceIRStatus, SourceDiagnostic, SourceDiagnosticSeverity,
    stable_document_id, stable_source_ir_id,
)
from reqpilotx.domain.source_visual_text import (
    VisualTextSnapshot, VisualTextObservation,
    stable_visual_text_snapshot_id, stable_visual_text_observation_id,
)


class TesseractDependencyMissing(RuntimeError):
    """The requested local executable or trained language data is unavailable."""


def extract_tesseract(source: Path, *, preflight, created_at: datetime, language: str, observer) -> SourceIRSnapshot:
    import pymupdf
    binary = shutil.which("tesseract")
    if not binary:
        raise TesseractDependencyMissing("TESSERACT_UNAVAILABLE")
    if language not in {"eng", "chi_sim", "eng+chi_sim"}:
        raise ValueError("TESSERACT_LANGUAGE_UNSUPPORTED")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if not preflight.processable or digest != preflight.identity.source_sha256:
        raise ValueError("Tesseract source changed or preflight rejected")
    languages = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=15, check=True).stdout
    available = set(languages.splitlines()[1:])
    if not set(language.split("+")).issubset(available):
        raise TesseractDependencyMissing("TESSERACT_LANGUAGE_DATA_MISSING: " + language)
    match = re.search(r'"([^"]+)"', languages.splitlines()[0])
    traineddata = {}
    for name in language.split("+"):
        data = Path(match.group(1)) / (name + ".traineddata") if match else None
        traineddata[name] = hashlib.sha256(data.read_bytes()).hexdigest() if data and data.is_file() else None
    config = {"language": language, "psm": 3, "oem": 1, "dpi": 144,
        "binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(), "traineddata_sha256": traineddata}
    adapter_id = "local-tesseract-lab"
    adapter_version = "lab1-" + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:24]
    identity = dict(source_sha256=digest, adapter_id=adapter_id, adapter_version=adapter_version)
    document_id = stable_document_id(digest)
    snapshots = []
    with pymupdf.open(source) as document, tempfile.TemporaryDirectory(prefix="parser-lab-ocr-") as temporary:
        for number, page in enumerate(document, 1):
            if page.rect.width * page.rect.height * 4 > 20_000_000:
                raise ValueError("Tesseract render pixel budget exceeded")
            image = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False, annots=False)
            png = image.tobytes("png")
            image_hash = hashlib.sha256(png).hexdigest()
            path = (Path(temporary) / "input.png").resolve()
            path.write_bytes(png)
            result = subprocess.run([binary, str(path), "stdout", "-l", language, "--oem", "1", "--psm", "3",
                "-c", "tessedit_create_tsv=1"], capture_output=True, timeout=90)
            observer(number, "tesseract-tsv", config, {"stdout": result.stdout, "stderr": result.stderr,
                "returncode": result.returncode, "page_image_png": png, "page_image_sha256": image_hash,
                "image_width": image.width, "image_height": image.height, "page_bbox": list(page.rect)})
            if result.returncode:
                raise ValueError("TESSERACT_FAILED: raw stderr retained")
            rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t")
            if not {"text", "conf", "left", "top", "width", "height"}.issubset(rows.fieldnames or ()):
                raise ValueError("TESSERACT_INVALID_TSV")
            common = dict(source_sha256=digest, page_image_sha256=image_hash, provider_id=adapter_id,
                provider_version=adapter_version, page_number=number)
            observations = []
            for row in rows:
                if not row["text"].strip() or float(row["conf"]) < 0:
                    continue
                x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
                if w <= 0 or h <= 0:
                    continue
                box = (x / image.width * page.rect.width, y / image.height * page.rect.height,
                    (x+w) / image.width * page.rect.width, (y+h) / image.height * page.rect.height)
                index = len(observations)
                observations.append(VisualTextObservation(**common, observation_index=index, bbox=box,
                    observation_id=stable_visual_text_observation_id(**common, observation_index=index, bbox=box),
                    raw_text=row["text"], confidence=min(1.0, float(row["conf"]) / 100), content_type="TEXT"))
            shape = dict(page_bbox=tuple(page.rect), page_image_width_px=image.width, page_image_height_px=image.height)
            snapshots.append(VisualTextSnapshot(**common, **shape, observations=tuple(observations), evaluated_content_types=("TEXT",),
                visual_text_snapshot_id=stable_visual_text_snapshot_id(**common, **shape)))
    return SourceIRSnapshot(**identity, document_id=document_id,
        source_ir_id=stable_source_ir_id(document_id=document_id, **identity), status=SourceIRStatus.PARTIAL,
        visual_text_snapshots=tuple(snapshots), created_at=created_at,
        diagnostics=(SourceDiagnostic(code="ocr_text_only", severity=SourceDiagnosticSeverity.WARNING,
            message="仅 TEXT 视觉观察；未评估表格拓扑、图片资产及 native text；不声明完整识别。"),))
