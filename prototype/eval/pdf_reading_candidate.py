"""Experimental local reading packet, OCR supplement and evidence selection.

Reuses the existing renderer/VisualText/SourceIR contracts. It does not alter a
Map, create source items, publish to a service, or certify OCR as original text.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import time

from eval.workbuddy_pdf_probe import sha, to_visual_snapshot, write_json


def prepare(source: Path, expected_sha256: str, pages: list[int], output: Path) -> dict:
    import fitz
    from app.adapters.documents.pdf_source_ir_adapter import render_pdf_visual_page_input
    original = source.read_bytes()
    if sha(original) != expected_sha256:
        raise ValueError("source_sha256_mismatch")
    with fitz.open(stream=original, filetype="pdf") as doc:
        if not pages or len(set(pages)) != len(pages) or any(
            type(p) is not int or not 1 <= p <= len(doc) for p in pages
        ):
            raise ValueError("invalid_page_scope")
        native = {p: doc[p-1].get_text("text") for p in pages}
    output.mkdir(parents=True, exist_ok=False)
    (output / "source.pdf").write_bytes(original)
    packet = {"schema": "reqpilotx-reading-probe-packet/v1", "source_sha256": expected_sha256,
              "source": "source.pdf", "pages": [], "semantic_status": "UNREVIEWED"}
    for page in pages:
        frame = render_pdf_visual_page_input(source, page_number=page, scale=300/72)
        if frame.source_sha256 != expected_sha256:
            raise ValueError("source_changed_during_prepare")
        image_path, text_path = f"page-{page:04d}.png", f"page-{page:04d}.md"
        (output / image_path).write_bytes(frame.page_image_bytes)
        (output / text_path).write_text(native[page], encoding="utf-8")
        packet["pages"].append({**frame.model_dump(mode="json"), "image": image_path,
                                "native_text": text_path, "native_chars": len(native[page].strip()),
                                "native_empty": not native[page].strip(),
                                "coverage": "NOT_ESTABLISHED"})
        write_json(output / "packet.json", packet)
    if sha(source.read_bytes()) != expected_sha256:
        raise ValueError("source_changed_during_prepare")
    return {"packet": str(output / "packet.json"), "pages": pages, "source_sha256": expected_sha256}


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError("packet_path_escape")
    return path


def load_page(packet_path: Path, number: int):
    from app.domain.source_visual_text import VisualTextPageInput
    from app.adapters.documents.visual_text_provider_support import validate_visual_text_page_input
    from PIL import Image
    packet = json.loads(packet_path.read_text())
    frames = [p for p in packet["pages"] if p["page_number"] == number]
    if type(number) is not int or len(frames) != 1:
        raise ValueError("page_not_in_packet")
    record = frames[0]
    if record["source_sha256"] != packet["source_sha256"]:
        raise ValueError("page_source_mismatch")
    source_bytes = _inside(packet_path.parent, packet["source"]).read_bytes()
    image_bytes = _inside(packet_path.parent, record["image"]).read_bytes()
    fields = {k: record[k] for k in VisualTextPageInput.model_fields
              if k not in {"source_bytes", "page_image_bytes"}}
    frame = VisualTextPageInput(**fields, source_bytes=source_bytes, page_image_bytes=image_bytes)
    validate_visual_text_page_input(frame)
    with Image.open(io.BytesIO(image_bytes)) as image:
        if image.size != (frame.page_image_width_px, frame.page_image_height_px):
            raise ValueError("image_dimensions_mismatch")
    return frame


def supplement(packet_path: Path, pages: list[int], output: Path, *, lang: str = "eng+chi_sim", psm: int = 3) -> dict:
    import pytesseract
    from PIL import Image
    if not pages or len(set(pages)) != len(pages):
        raise ValueError("invalid_page_scope")
    if lang not in {"eng+chi_sim", "chi_sim+eng"} or psm not in {3, 6}:
        raise ValueError("unsupported_probe_ocr_config")
    frames = [load_page(packet_path, n) for n in pages]
    output.mkdir(parents=True, exist_ok=False)
    version = "canonical1-" + sha(Path(__file__).read_bytes())[:12] + "-" + sha(
        Path(to_visual_snapshot.__code__.co_filename).read_bytes())[:12] + f"-{lang}-psm{psm}"
    receipt = {"schema": "reqpilotx-reading-probe-supplement/v1", "pages": [],
               "packet_sha256": sha(packet_path.read_bytes()), "engine": str(pytesseract.get_tesseract_version()),
               "lang": lang, "psm": psm, "source_sha256": frames[0].source_sha256,
               "scope_origin": "OPERATOR_SELECTED_EXISTING_CASE_PAGES", "model_calls": 0,
               "semantic_status": "UNREVIEWED"}
    for frame in frames:
        start = time.perf_counter()
        with Image.open(io.BytesIO(frame.page_image_bytes)) as image:
            raw = pytesseract.image_to_data(image, lang=lang, config=f"--psm {psm}", timeout=60,
                                            output_type=pytesseract.Output.DICT)
        snapshot = to_visual_snapshot(frame, raw, version)
        name = f"page-{frame.page_number:04d}"
        write_json(output / (name + ".ocr-data.json"), raw)
        write_json(output / (name + ".visual-text.json"), snapshot.model_dump(mode="json"))
        text = "\n".join(o.raw_text for o in snapshot.observations)
        (output / (name + ".md")).write_text(text + "\n", encoding="utf-8")
        item = {"page": frame.page_number, "elapsed_s": round(time.perf_counter()-start, 3),
                "image_sha256": frame.page_image_sha256, "lines": len(snapshot.observations),
                "text_characters": len(text), "snapshot": name + ".visual-text.json",
                "snapshot_sha256": sha((output / (name + ".visual-text.json")).read_bytes())}
        receipt["pages"].append(item)
        write_json(output / "receipt.json", receipt)
        print(json.dumps(item), flush=True)
    return receipt


def select_evidence(packet_path: Path, candidate_dir: Path, base_ir_path: Path, output: Path) -> dict:
    from app.domain.source_visual_text import VisualTextSnapshot
    from app.domain.extraction.source_ir import SourceIRSnapshot
    base_bytes = base_ir_path.read_bytes()
    base = SourceIRSnapshot.model_validate_json(base_bytes)
    receipt = json.loads((candidate_dir / "receipt.json").read_text())
    if receipt["packet_sha256"] != sha(packet_path.read_bytes()):
        raise ValueError("packet_changed_after_supplement")
    if base.source_sha256 != receipt["source_sha256"]:
        raise ValueError("base_source_mismatch")
    if not receipt["pages"]:
        raise ValueError("empty_candidate_selection")
    candidates = []
    for item in receipt["pages"]:
        frame = load_page(packet_path, item["page"])
        raw = _inside(candidate_dir, item["snapshot"]).read_bytes()
        if sha(raw) != item["snapshot_sha256"]:
            raise ValueError("candidate_changed_after_supplement")
        candidate = VisualTextSnapshot.model_validate_json(raw)
        if (candidate.source_sha256 != frame.source_sha256 or candidate.page_number != frame.page_number
                or candidate.page_image_sha256 != frame.page_image_sha256
                or candidate.page_bbox != frame.page_bbox
                or candidate.page_image_width_px != frame.page_image_width_px
                or candidate.page_image_height_px != frame.page_image_height_px):
            raise ValueError("candidate_frame_mismatch")
        candidates.append(candidate)
    by_id = {s.visual_text_snapshot_id: s for s in (*base.visual_text_snapshots, *candidates)}
    payload = base.model_dump(mode="json")
    payload["visual_text_snapshots"] = [s.model_dump(mode="json") for s in sorted(
        by_id.values(), key=lambda s: (s.page_number, s.provider_id, s.provider_version, s.visual_text_snapshot_id))]
    # The native adapter identity also defines existing node/asset IDs. Preserve
    # it and version the appended evidence by the exact artifact digest, like
    # the existing drawing-text capture extension. This is a separate file.
    augmented = SourceIRSnapshot.model_validate(payload)
    assert augmented.nodes == base.nodes and augmented.assets == base.assets
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "source_ir.json", augmented.model_dump(mode="json"))
    selected = {"schema": "reqpilotx-reading-probe-selection/v1", "source_sha256": base.source_sha256,
                "base_source_ir_sha256": sha(base_bytes), "source_ir_sha256": sha((output / "source_ir.json").read_bytes()),
                "source_ir_id": augmented.source_ir_id, "selected_snapshots": len(candidates),
                "reading_evidence_revision": sha((output / "source_ir.json").read_bytes()),
                "selected_observations": sum(len(s.observations) for s in candidates),
                "native_nodes_unchanged": augmented.nodes == base.nodes,
                "assets_unchanged": augmented.assets == base.assets,
                "base_file_unchanged": base_ir_path.read_bytes() == base_bytes,
                "selection_purpose": "READING_EVIDENCE_ONLY", "semantic_status": "UNREVIEWED",
                "business_results_modified": False, "portable_map_consumption": "NOT_IMPLEMENTED"}
    write_json(output / "selection.json", selected)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--expected-sha256", required=True)
    p.add_argument("--pages", type=int, nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("supplement")
    p.add_argument("--packet", type=Path, required=True)
    p.add_argument("--pages", type=int, nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--lang", default="eng+chi_sim", choices=["eng+chi_sim", "chi_sim+eng"])
    p.add_argument("--psm", type=int, default=3, choices=[3, 6])
    p = commands.add_parser("select-evidence")
    p.add_argument("--packet", type=Path, required=True)
    p.add_argument("--candidates", type=Path, required=True)
    p.add_argument("--base-source-ir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.source, args.expected_sha256, args.pages, args.output)
    elif args.command == "supplement":
        result = supplement(args.packet, args.pages, args.output, lang=args.lang, psm=args.psm)
    else:
        result = select_evidence(args.packet, args.candidates, args.base_source_ir, args.output)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
