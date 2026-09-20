"""Bounded, local WorkBuddy PDF evaluation; never imports its editing/setup CLI.

The third-party skill stays outside this repository. Its original command
handlers run unchanged. Instrumentation records the actual OCR input pixels and
raw word boxes before WorkBuddy rounds/project them. No OCR text is promoted to
a business result by this probe.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def skill_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in sorted(root.rglob("*"))
            if p.is_file() and (p.suffix == ".py" or p.name == "SKILL.md")}


def join_ocr_words(words: list[str]) -> str:
    # A reading projection only; exact OCR tokens remain in ocr-data.json.
    import re
    cjk = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")
    out = ""
    for word in words:
        if out and not (cjk.match(out[-1]) or cjk.match(word[0])):
            out += " "
        out += word
    return out


def to_visual_snapshot(page_input, raw: dict, provider_version: str):
    """Preserve raw OCR text and adapt exact integer pixels to existing contracts."""
    from app.domain.source_visual_text import (
        VisualTextGeometryReceipt, VisualTextObservation, VisualTextSnapshot,
        stable_visual_text_observation_id, stable_visual_text_snapshot_id,
    )
    if sha(page_input.source_bytes) != page_input.source_sha256:
        raise ValueError("source_sha256_mismatch")
    if sha(page_input.page_image_bytes) != page_input.page_image_sha256:
        raise ValueError("page_image_sha256_mismatch")
    groups: OrderedDict[tuple, list[int]] = OrderedDict()
    for i, text in enumerate(raw["text"]):
        if text.strip() and float(raw["conf"][i]) >= 0:
            key = tuple(raw[k][i] for k in ("page_num", "block_num", "par_num", "line_num"))
            groups.setdefault(key, []).append(i)
    provider_id = "workbuddy-tesseract-probe"
    common = dict(source_sha256=page_input.source_sha256,
                  page_image_sha256=page_input.page_image_sha256,
                  provider_id=provider_id, provider_version=provider_version,
                  page_number=page_input.page_number)
    w, h = page_input.page_image_width_px, page_input.page_image_height_px
    px0, py0, px1, py1 = page_input.page_bbox
    observations = []
    for index, members in enumerate(groups.values()):
        x0 = min(raw["left"][i] for i in members)
        y0 = min(raw["top"][i] for i in members)
        x1 = max(raw["left"][i] + raw["width"][i] for i in members)
        y1 = max(raw["top"][i] + raw["height"][i] for i in members)
        quad = ((float(x0), float(y0)), (float(x1), float(y0)),
                (float(x1), float(y1)), (float(x0), float(y1)))
        bbox = (px0 + x0 / w * (px1-px0), py0 + y0 / h * (py1-py0),
                px0 + x1 / w * (px1-px0), py0 + y1 / h * (py1-py0))
        receipt = VisualTextGeometryReceipt(
            provider_quadrilateral_px=quad, original_page_quadrilateral_px=quad,
            provider_image_size_px=(w, h), original_page_image_size_px=(w, h),
            input_rotation_degrees=0, target_bbox=bbox)
        observations.append(VisualTextObservation(
            **common, observation_index=index, bbox=bbox,
            observation_id=stable_visual_text_observation_id(
                **common, observation_index=index, bbox=bbox),
            raw_text=join_ocr_words([raw["text"][i].strip() for i in members]),
            confidence=None, geometry_receipt=receipt))
    frame = dict(page_bbox=page_input.page_bbox, page_image_width_px=w, page_image_height_px=h)
    return VisualTextSnapshot(**common, **frame, observations=tuple(observations),
        visual_text_snapshot_id=stable_visual_text_snapshot_id(**common, **frame))


def run(args) -> None:
    import fitz
    import pytesseract
    from PIL import Image
    from app.domain.source_visual_text import VisualTextPageInput
    from app.adapters.documents.pdf_source_ir_adapter import render_pdf_visual_page_input

    source = args.source.resolve(strict=True)
    original = source.read_bytes()
    if sha(original) != args.expected_sha256:
        raise ValueError("source_sha256_mismatch")
    root = args.workbuddy_skill.resolve(strict=True)
    before = skill_hashes(root)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "source.pdf").write_bytes(original)
    sys.path.insert(0, str(root / "scripts"))
    modules = {name: importlib.import_module("pdfkit.commands." + name) for name in
               ("detect_type", "extract_text", "ocr_locate", "extract_table", "reading_order", "layout_analyze")}
    with fitz.open(stream=original, filetype="pdf") as doc:
        if any(type(p) is not int or p < 1 or p > len(doc) for p in args.pages):
            raise ValueError("invalid_page_scope")
        if len(set(args.pages)) != len(args.pages):
            raise ValueError("duplicate_page_scope")
        annotation_counts = {p: sum(1 for _ in doc[p-1].annots()) for p in args.pages}
    indexes = [p - 1 for p in args.pages]
    version = "wb1-" + sha(json.dumps(before, sort_keys=True).encode())[:16]
    summary = {"schema": "reqpilotx-workbuddy-pdf-probe/v1", "source_sha256": sha(original),
               "source_page_numbers": args.pages, "third_party_skill": str(root),
               "skill_hashes": before, "pymupdf": fitz.VersionBind,
               "pytesseract": pytesseract.__version__,
               "tesseract": str(pytesseract.get_tesseract_version()),
               "annotation_counts": annotation_counts, "baseline": {}, "pages": [],
               "external_model_calls": 0, "business_results_modified": False}
    write_json(output / "environment.json", summary)
    for name in ("detect_type", "extract_text", "extract_table", "reading_order", "layout_analyze"):
        params = {"input": str(source), "pages": indexes}
        if name == "extract_text":
            params.update(ocr_fallback=True, format="text", lang="eng+chi_sim")
        if name == "layout_analyze":
            params["detail"] = "full"
        start = time.perf_counter()
        try:
            value = modules[name].handler(params)
            write_json(output / ("baseline-" + name + ".json"), value)
            result = {"status": "completed", "elapsed_s": round(time.perf_counter()-start, 3)}
        except Exception as exc:
            result = {"status": "failed", "error": type(exc).__name__, "message": str(exc),
                      "elapsed_s": round(time.perf_counter()-start, 3)}
        summary["baseline"][name] = result
        print(json.dumps({"baseline": name, **result}), flush=True)
        write_json(output / "summary.json", summary)

    for number in args.pages:
        page_dir = output / f"page-{number:04d}"
        page_dir.mkdir()
        captured = {}
        actual_image_to_data = pytesseract.image_to_data

        def record_ocr(image, *a, **kw):
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            captured["image"] = buf.getvalue()
            captured["size"] = image.size
            raw = actual_image_to_data(image, *a, **kw)
            captured["raw"] = raw
            return raw

        start = time.perf_counter()
        with patch.object(pytesseract, "image_to_data", record_ocr):
            located = modules["ocr_locate"].handler(
                {"input": str(source), "page": number-1, "lang": "eng+chi_sim"})
        elapsed = time.perf_counter()-start
        write_json(page_dir / "workbuddy-ocr-locate.json", located)
        write_json(page_dir / "ocr-data.json", captured["raw"])
        (page_dir / "ocr-input.png").write_bytes(captured["image"])
        # Canonical rendering remains annotation-free; verify pixels, not PNG encoding.
        canonical = render_pdf_visual_page_input(source, page_number=number, scale=300/72)
        (page_dir / "source.png").write_bytes(canonical.page_image_bytes)
        canonical_image = Image.open(io.BytesIO(canonical.page_image_bytes)).convert("RGB")
        workbuddy_image = Image.open(io.BytesIO(captured["image"])).convert("RGB")
        pixels_match = canonical_image.size == workbuddy_image.size and canonical_image.tobytes() == workbuddy_image.tobytes()
        # Always bind to the actual frame consumed by OCR, never relabel a changed image.
        page_input = VisualTextPageInput(
            source_bytes=original, source_sha256=sha(original), page_number=number,
            page_bbox=canonical.page_bbox, page_image_bytes=captured["image"],
            page_image_sha256=sha(captured["image"]),
            page_image_width_px=captured["size"][0], page_image_height_px=captured["size"][1])
        snapshot = to_visual_snapshot(page_input, captured["raw"], version)
        write_json(page_dir / "visual-text-candidate.json", snapshot.model_dump(mode="json"))
        with fitz.open(stream=original, filetype="pdf") as doc:
            native = doc[number-1].get_text("text")
            words = doc[number-1].get_text("words")
        (page_dir / "native.md").write_text(native, encoding="utf-8")
        write_json(page_dir / "native-words.json", words)
        text = "\n".join(o.raw_text for o in snapshot.observations)
        (page_dir / "ocr-candidate.md").write_text(text + "\n", encoding="utf-8")
        result = {"page": number, "ocr_elapsed_s": round(elapsed, 3),
                  "native_chars": len(native.strip()), "ocr_chars": len(text),
                  "ocr_words": located["total_blocks"], "line_observations": len(snapshot.observations),
                  "canonical_pixels_match": pixels_match, "source_image_sha256": canonical.page_image_sha256,
                  "ocr_image_sha256": page_input.page_image_sha256,
                  "contract": "VALIDATED", "semantic_review": "PENDING", "adopted": False}
        summary["pages"].append(result)
        write_json(page_dir / "receipt.json", result)
        write_json(output / "summary.json", summary)
        print(json.dumps(result), flush=True)
    summary["source_unchanged"] = sha(source.read_bytes()) == sha(original)
    summary["third_party_code_unchanged"] = skill_hashes(root) == before
    write_json(output / "summary.json", summary)
    if not summary["source_unchanged"] or not summary["third_party_code_unchanged"]:
        raise ValueError("input_changed_during_probe")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--workbuddy-skill", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages", type=int, nargs="+", required=True)
    run(parser.parse_args())
