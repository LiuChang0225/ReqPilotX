"""Synthetic input builders with no repository or runtime path mutation."""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_fixture(directory: Path, *, table_image=False, numbered_targets=False) -> tuple[Path, Path, dict]:
    """Raw invented obligations cover all five classes and a complete test table."""
    import pymupdf
    from reqpilotx.application.fast_source_capture import capture_fast_source

    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "synthetic.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    lines = [
        (50, "Valve module specification"),
        (90, "1 Technical targets"),
        (120, "The valve shall open within 30 ms at 12 V and 23 C.\nThe valve shall close within 40 ms at 12 V and 23 C."),
        (155, "2 Project delivery"),
        (185, "The supplier shall deliver drawings before sample approval."),
        (220, "3 Review process"),
        (250, "The reviewer shall record the review decision."),
        (285, "4 Construction"),
        (315, "The housing shall use aluminium."),
        (350, "5 Functional behavior"),
        (380, "The module shall report an overtemperature warning."),
        (415, "6 Technical test"),
        (650, "Note: Packaging shown in Appendix A is illustrative."),
    ]
    if numbered_targets:
        lines[2] = (120, "1.1 The valve shall open within 30 ms at 12 V and 23 C.\n1.2 The valve shall close within 40 ms at 12 V and 23 C.")
    for y, text in lines:
        page.insert_text((40, y), text, fontsize=11)
    for x in (40, 190, 420, 555):
        page.draw_line((x, 440), (x, 590))
    for y in (440, 475, 530, 590):
        page.draw_line((40, y), (555, y))
    for x, y, text in [
        (45, 460, "Test"), (195, 460, "Method and conditions"), (425, 460, "Criterion"),
        (45, 495, "Response"), (195, 495, "Apply 12 V at 23 C."), (425, 495, "Open <= 30 ms"),
        (45, 550, "Durability"), (195, 550, "Repeat 1000 cycles."), (425, 550, "No leakage"),
    ]:
        page.insert_text((x, y), text, fontsize=10)
    from PIL import Image, ImageDraw
    if table_image:
        figure = Image.new("RGB", (100, 21), (130, 90, 70))
        ImageDraw.Draw(figure).text((5, 5), "Original test diagram", fill="white")
        image_bytes = BytesIO(); figure.save(image_bytes, format="PNG")
        page.insert_image(pymupdf.Rect(310, 503, 410, 524), stream=image_bytes.getvalue())
    for x, caption in ((40, "WIDTH <= 18 mm"), (260, "HEIGHT <= 6 mm")):
        figure = Image.new("RGB", (160, 80), (100, 100, 100))
        ImageDraw.Draw(figure).text((12, 32), caption, fill=(255, 255, 255))
        image_bytes = BytesIO(); figure.save(image_bytes, format="PNG")
        page.insert_image(pymupdf.Rect(x, 700, x + 160, 780), stream=image_bytes.getvalue())
    pdf.save(source)
    pdf.close()
    capture = directory / "capture"
    capture_fast_source(source, output_dir=capture)
    ir = json.loads((capture / "source_ir.json").read_text())
    by_text = {node["raw_text"]: node for node in ir["nodes"]}
    nodes = [{
        "node_id": "main", "parent_id": None, "kind": "DOCUMENT", "title": lines[0][1], "label": "",
        "title_source_ids": [by_text[lines[0][1]]["source_node_id"]], "content_source_ids": [],
    }]
    for number in range(1, 7):
        heading = lines[number * 2 - 1][1]
        body = lines[number * 2][1] if number < 6 else lines[-1][1]
        nodes.append({
            "node_id": f"s{number}", "parent_id": "main", "kind": "SECTION",
            "title": heading.split(" ", 1)[1], "label": str(number),
            "title_source_ids": [by_text[heading]["source_node_id"]],
            "content_source_ids": [by_text[body]["source_node_id"]],
        })
    table = next(node for node in ir["nodes"] if node.get("table_payload"))
    nodes[-1]["content_source_ids"].extend(
        node["source_node_id"] for node in ir["nodes"] if node["kind"] in {"image", "figure"}
    )
    duplicate_ids = [
        node["source_node_id"] for node in ir["nodes"]
        if node["kind"] not in {"table", "image", "figure"} and 440 < node["native_locator"]["bbox"][1] < 590
    ]
    decisions = {
        "schema_version": "reqpilot-map-decisions/v1", "source_sha256": ir["source_sha256"], "nodes": nodes,
        "logical_tables": [{
            "scope_id": "technical-test", "owner_node_id": "s6", "title": "Technical test", "role": "TEST_MATRIX",
            "fragments": [{"source_node_id": table["source_node_id"], "row_range": [0, 3]}],
        }],
        "source_dispositions": [
            {"source_node_id": value, "disposition": "DUPLICATE_CAPTURE", "reason": "This native text block is visibly the same cells in the captured table region."}
            for value in duplicate_ids
        ],
    }
    return source, capture, decisions


def item_decisions(map_dir: Path) -> dict:
    artifact = json.loads((map_dir / "document-map.json").read_text())
    requests = json.loads((map_dir / "scope-requests.json").read_text())
    kinds = {"s1": "QUALITY", "s2": "PROJECT", "s3": "PROCESS", "s4": "CONSTRAINT", "s5": "FUNCTIONAL"}
    decisions = {
        "schema_version": "reqpilot-item-decisions/v1", "source_sha256": artifact["source_sha256"],
        "document_map_artifact_sha256": artifact["artifact_sha256"], "text_decisions": [], "table_decisions": [],
    }
    for request in requests["text_requests"]:
        items, structure = [], []
        for content in request["source_content"]:
            span = {"content_id": content["content_id"], "start_offset": 0, "end_offset": len(content["text"])}
            if content["is_title"]:
                structure.append({"text_span": span, "reason_codes": ["source_heading"]})
            else:
                kind = kinds.get(request["node_id"])
                items.append({
                    "item_key": "body", "label": content["text"], "source_form": "NUMBERED_CLAUSE",
                    "text_spans": [span], "requirementness": "REQUIREMENT" if kind else "NON_REQUIREMENT",
                    "requirement_kinds": [kind] if kind else [], "non_requirement_role": None if kind else "NOTE",
                    "confidence": 0.9,
                    "decision_summary": "Synthetic original provision preserves complete wording." if kind else "The note states illustrative context.",
                    "reason_codes": ["source_meaning"],
                })
        decisions["text_decisions"].append({"scope_id": request["scope_id"], "items": items, "structure_only_spans": structure})
    for request in requests["table_requests"]:
        fragment = request["fragments"][0]
        records = [{
            "record_key": label.lower(), "label": label, "source_form": "TABLE_RECORD",
            "row_groups": [{"fragment_id": fragment["fragment_id"], "row_start": row, "row_end": row + 1}],
            "requirementness": "REQUIREMENT", "requirement_kinds": ["QUALITY"], "confidence": 0.9,
            "decision_summary": "Method, conditions and criterion belong to the same complete test.",
            "reason_codes": ["complete_test"],
        } for row, label in ((1, "Response"), (2, "Durability"))]
        decisions["table_decisions"].append({
            "logical_table_scope_id": request["logical_table_scope_id"], "records": records,
            "context_row_groups": [{
                "role": "HEADER", "row_group": {"fragment_id": fragment["fragment_id"], "row_start": 0, "row_end": 1},
                "reason_codes": ["shared_header"],
            }],
        })
    return decisions
