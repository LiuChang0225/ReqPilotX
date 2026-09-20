"""Read the original PDF annotation layer without promoting it to requirements."""
from __future__ import annotations

import hashlib
from pathlib import Path


def capture_pdf_annotations(source_path: str | Path, *, expected_sha256: str) -> dict:
    import pymupdf

    raw = Path(source_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("PDF annotation source identity changed")
    items = []
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        for page in document:
            for annotation in page.annots() or ():
                info = dict(annotation.info)
                items.append({
                    "annotation_id": f"pdf-annotation-{expected_sha256[:24]}-{annotation.xref}",
                    "source_sha256": expected_sha256, "page": page.number + 1,
                    "xref": annotation.xref, "annotation_type": annotation.type[1],
                    "rect": list(annotation.rect), "page_bounds": list(page.rect),
                    "text": info.get("content", ""), "metadata": info,
                    "source_role": "PDF_ANNOTATION", "normative_status": "NOT_DETERMINED",
                })
        page_count = document.page_count
    return {"schema_version": "pdf-annotation-source/v1", "source_sha256": expected_sha256,
            "page_count": page_count, "annotations": items,
            "interpretation": "Original comments are retained separately. They do not override body text or establish accepted requirements."}
