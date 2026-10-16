"""Invented source-unit lessons compiled through the public task facade.

These explicit worked answers teach tool syntax; they are not extraction rules
or evaluation gold. All source positions are resolved from the generated PDF.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil


def _save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _specification():
    return [
        {
            "id": "numbered",
            "title": "1 Valve limits",
            "texts": [
                (
                    "parent",
                    "The following limits apply at 12 V and 23 C.",
                    None,
                    "CONTEXT",
                ),
                ("opening", "1.1 The valve shall open within 30 ms.", "QUALITY", None),
                ("closing", "1.2 The valve shall close within 40 ms.", "QUALITY", None),
            ],
        },
        {
            "id": "milestones",
            "title": "2 Delivery milestones",
            "table": [
                ["No.", "Deliverable", "Due at"],
                ["M1", "Drawings", "Concept approval"],
                ["M2", "Samples", "Design approval"],
            ],
            "groups": [(1, 2, "drawings"), (2, 3, "samples")],
            "kind": "PROJECT",
        },
        {
            "id": "parameters",
            "title": "3 Electrical limits",
            "table": [
                ["Property", "Condition", "Limit"],
                ["Idle current", "12 V, 23 C", "max 0.8 mA"],
                ["Startup time", "12 V, 23 C", "max 10 ms"],
            ],
            "groups": [(1, 2, "idle"), (2, 3, "startup")],
            "kind": "QUALITY",
        },
        {
            "id": "bom",
            "title": "4 Product composition",
            "texts": [
                (
                    "lead",
                    "The valve module shall comprise the following parts.",
                    "CONSTRAINT",
                    None,
                )
            ],
            "table": [
                ["No.", "Part", "Quantity"],
                ["1", "Housing", "1"],
                ["2", "Seal", "1"],
            ],
            "groups": [(1, 3, "complete-bom")],
            "kind": "CONSTRAINT",
            "mixed": True,
        },
        {
            "id": "independent",
            "title": "4.1 Connector",
            "parent": "bom",
            "texts": [
                ("connector", "The connector shall have four pins.", "CONSTRAINT", None)
            ],
        },
        {
            "id": "tests",
            "title": "5 Environmental tests",
            "texts": [
                (
                    "vibration",
                    "5.1 Mount the module at 12 V. Apply vibration for 2 h.",
                    "QUALITY",
                    None,
                ),
                (
                    "vibration-end",
                    "After vibration, inspect for cracks. No cracks are permitted.",
                    "QUALITY",
                    None,
                ),
                (
                    "thermal",
                    "5.2 Cycle between -20 C and 80 C for 50 cycles. No leakage is permitted.",
                    "QUALITY",
                    None,
                ),
            ],
            "combine": [("vibration", "vibration-end")],
        },
        {
            "id": "context",
            "title": "6 Response definition and applicability",
            "texts": [
                (
                    "definition",
                    "Response duration means input edge to stable valve output.",
                    None,
                    "DEFINITION",
                ),
                (
                    "applicability",
                    "The next limit applies to variant A. Variant B is excluded.",
                    None,
                    "CONTEXT",
                ),
                (
                    "target",
                    "6.1 Variant A response duration shall be below 25 ms at 12 V.",
                    "QUALITY",
                    None,
                ),
                (
                    "verification",
                    "6.2 Measure response duration at 12 V against the limit in 6.1.",
                    "QUALITY",
                    None,
                ),
            ],
        },
        {
            "id": "figures",
            "title": "7 Supporting figure",
            "texts": [
                (
                    "drawing-target",
                    "7.1 The housing width shall be at most 18 mm, as shown in Figure 1.",
                    "QUALITY",
                    None,
                )
            ],
            "figure": True,
        },
    ]


def _make_pdf(path, sections):
    import pymupdf
    from PIL import Image, ImageDraw
    from io import BytesIO

    document = pymupdf.open()
    for section in sections:
        page = document.new_page(width=595, height=842)
        section["page"] = len(document)
        page.insert_text((40, 55), section["title"], fontsize=12)
        for index, (_, text, _, _) in enumerate(section.get("texts", [])):
            if section["id"] == "tests" and index == 1:
                page = document.new_page(width=595, height=842)
                section["continuation_page"] = len(document)
            y = 110 + (index if section["id"] != "tests" else min(index, 1)) * 75
            if section["id"] == "tests" and index == 2:
                y = 290
            page.insert_text((40, y), text, fontsize=9)
        if section.get("table"):
            rows = section["table"]
            xs = (40, 160, 370, 555)
            top = 240
            for x in xs:
                page.draw_line((x, top), (x, top + 45 * len(rows)))
            for row in range(len(rows) + 1):
                page.draw_line((xs[0], top + 45 * row), (xs[-1], top + 45 * row))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    page.insert_text(
                        (xs[column] + 5, top + row * 45 + 25), value, fontsize=10
                    )
        if section.get("figure"):
            image = Image.new("RGB", (350, 140), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((30, 30, 300, 90), outline="black", width=3)
            draw.text((85, 105), "Figure 1: WIDTH <= 18 mm", fill="black")
            data = BytesIO()
            image.save(data, format="PNG")
            page.insert_image(pymupdf.Rect(40, 220, 390, 360), stream=data.getvalue())
    document.save(path)
    document.close()


def generate(project_root, output, source_input=None):
    from reqpilot import load_runtime

    load_runtime(project_root)
    from task_service import TaskService

    output.mkdir(parents=True, exist_ok=False)
    sections = _specification()
    source = output / "source-units.pdf"
    _make_pdf(source, sections)
    if source_input is not None:
        # Comparison runs may supply these same invented pages. Exact text and
        # measured capture below still have to match every declared lesson.
        original = Path(source_input).expanduser()
        if original.is_symlink() or not original.is_file():
            raise ValueError("Example source must be a regular file")
        shutil.copyfile(original, source)
    (output / "workspaces").mkdir(mode=0o700)
    service = TaskService(
        output / "workspaces", Path(__file__).with_name("reqpilot.py"), project_root
    )
    opened = service.invoke(
        "open",
        {
            "mode": "create",
            "source": str(source),
            "goal": "Generate explicit synthetic source-unit teaching artifacts; no model or human acceptance.",
            "creation_key": "source-unit-methods",
        },
    )
    task_id = opened["task_id"]
    calls = []

    def write(action, **arguments):
        state = service.invoke("inspect", {"task_id": task_id})
        request = {
            "task_id": task_id,
            "expected_revision": state["revision"],
            **arguments,
        }
        result = service.invoke(action, request)
        if result.get("operation_state") not in {"COMMITTED", "NOT_NEEDED"}:
            raise ValueError(
                f"Worked example {action} did not commit: {result.get('result', result.get('status'))}"
            )
        result_path = output / f"{len(calls) + 1:02d}-{action}-result.json"
        _save(result_path, result)
        calls.append(
            {
                "action": action,
                "request": request,
                "result_path": result_path.name,
                "operation_state": result.get("operation_state"),
            }
        )
        _save(output / "public-calls.json", calls)
        return result

    prepared = write("prepare", mode="capture")
    capture = Path(prepared["selected"]["READING"]["path"])
    if capture.is_dir():
        capture = capture / "source_ir.json"
    ir = json.loads(capture.read_text())

    def select(text, page):
        matches = [
            n
            for n in ir["nodes"]
            if n["native_locator"].get("page_number") == page
            and text in n["raw_text"]
            and not n.get("table_payload")
        ]
        if len(matches) != 1:
            raise ValueError("Synthetic quotation must resolve exactly once: " + text)
        node = matches[0]
        start = node["raw_text"].index(text)
        return {
            "source_node_id": node["source_node_id"],
            "start_offset": start,
            "end_offset": start + len(text),
        }

    nodes = [
        {
            "node_id": "document",
            "kind": "DOCUMENT",
            "parent_id": None,
            "title": source.name,
        }
    ]
    tables, duplicates = [], []
    for section in sections:
        title = section["title"]
        page = section["page"]
        node = {
            "node_id": section["id"],
            "kind": "SECTION",
            "parent_id": section.get("parent", "document"),
            "title": title.split(" ", 1)[1],
            "label": title.split(" ", 1)[0],
            "title_source_ranges": [select(title, page)],
            "content_source_ranges": [],
        }
        for index, (_, text, _, _) in enumerate(section.get("texts", [])):
            actual_page = (
                section.get("continuation_page", page)
                if section["id"] == "tests" and index
                else page
            )
            node["content_source_ranges"].append(select(text, actual_page))
        if section.get("figure"):
            node["content_source_ids"] = [
                n["source_node_id"]
                for n in ir["nodes"]
                if n["native_locator"].get("page_number") == page
                and n["kind"] in {"image", "figure"}
            ]
        nodes.append(node)
        if section.get("table"):
            table_nodes = [
                n
                for n in ir["nodes"]
                if n["native_locator"].get("page_number") == page
                and n.get("table_payload")
            ]
            if len(table_nodes) != 1:
                raise ValueError("Synthetic lesson requires its one measured table")
            table = table_nodes[0]
            tables.append(
                {
                    "scope_id": section["id"] + "-table",
                    "owner_node_id": section["id"],
                    "title": node["title"],
                    "role": "PRODUCT_COMPOSITION"
                    if section.get("mixed")
                    else "SOURCE_RECORDS",
                    "fragments": [{"source_node_id": table["source_node_id"]}],
                }
            )
            box = table["native_locator"]["bbox"]
            duplicates.extend(
                n["source_node_id"]
                for n in ir["nodes"]
                if n["native_locator"].get("page_number") == page
                and n["kind"] not in {"table", "image", "figure"}
                and box[1] <= n["native_locator"]["bbox"][1] < box[3]
            )
    mapping = {
        "schema_version": "reqpilot-map-edit/v1",
        "nodes": nodes,
        "logical_tables": tables,
        "source_dispositions": [
            {
                "source_node_id": sid,
                "disposition": "DUPLICATE_CAPTURE",
                "reason": "The same original words are retained in the measured table cells.",
            }
            for sid in duplicates
        ],
    }
    _save(output / "map-decisions.json", mapping)
    mapped = write("map", decisions=mapping)
    map_dir = Path(mapped["selected"]["MAP"]["output_dir"])
    requests = json.loads((map_dir / "scope-requests.json").read_text())
    sections_by_id = {s["id"]: s for s in sections}
    decisions = {"text_decisions": [], "table_decisions": []}
    for request in requests["text_requests"]:
        section = sections_by_id.get(request["node_id"])
        entry = {
            "scope_id": request["scope_id"],
            "items": [],
            "structure_only_spans": [],
        }
        by_key = {}
        for content in request["source_content"]:
            span = {
                "content_id": content["content_id"],
                "start_offset": 0,
                "end_offset": len(content["text"]),
            }
            if content["is_title"]:
                entry["structure_only_spans"].append(
                    {"text_span": span, "reason_codes": ["source_heading"]}
                )
                continue
            choice = next(t for t in section["texts"] if t[1] == content["text"])
            key, text, kind, role = choice
            if section.get("mixed"):
                entry["table_owned_spans"] = [
                    {
                        "logical_table_scope_id": "bom-table",
                        "record_key": "complete-bom",
                        "text_span": span,
                    }
                ]
                continue
            numbered = key in {
                "opening",
                "closing",
                "vibration",
                "thermal",
                "target",
                "verification",
                "drawing-target",
            }
            by_key[key] = {
                "item_key": key,
                "label": text,
                "source_form": "NUMBERED_CLAUSE" if numbered else "PARAGRAPH",
                "text_spans": [span],
                "requirementness": "REQUIREMENT" if kind else "NON_REQUIREMENT",
                "requirement_kinds": [kind] if kind else [],
                "non_requirement_role": role,
                "confidence": 0.95,
                "decision_summary": "Explicit synthetic source-unit choice.",
                "reason_codes": ["source_unit_method"],
            }
        for first, continuation in section.get("combine", []) if section else []:
            by_key[first]["text_spans"].extend(by_key.pop(continuation)["text_spans"])
        entry["items"] = list(by_key.values())
        decisions["text_decisions"].append(entry)
    for request in requests["table_requests"]:
        section = sections_by_id[request["node_id"]]
        fid = request["fragments"][0]["fragment_id"]
        records = []
        for lo, hi, key in section["groups"]:
            record = {
                "record_key": key,
                "label": key,
                "source_form": "TABLE_BRANCH"
                if section.get("mixed")
                else "TABLE_RECORD",
                "row_groups": [{"fragment_id": fid, "row_start": lo, "row_end": hi}],
                "requirementness": "REQUIREMENT",
                "requirement_kinds": [
                    "PROJECT" if section.get("mixed") else section["kind"]
                ],
                "confidence": 0.95,
                "decision_summary": "Known teaching error: composition misclassified as delivery."
                if section.get("mixed")
                else "One complete independent obligation with its conditions.",
                "reason_codes": [
                    "teaching_seed_error"
                    if section.get("mixed")
                    else "complete_source_unit"
                ],
            }
            if section.get("mixed"):
                text_request = next(
                    r for r in requests["text_requests"] if r["node_id"] == "bom"
                )
                content = next(
                    c for c in text_request["source_content"] if not c["is_title"]
                )
                record["owning_text_spans"] = [
                    {
                        "content_id": content["content_id"],
                        "start_offset": 0,
                        "end_offset": len(content["text"]),
                    }
                ]
            records.append(record)
        decisions["table_decisions"].append(
            {
                "logical_table_scope_id": request["logical_table_scope_id"],
                "records": records,
                "context_row_groups": [
                    {
                        "role": "HEADER",
                        "row_group": {"fragment_id": fid, "row_start": 0, "row_end": 1},
                        "reason_codes": ["shared_header"],
                    }
                ],
            }
        )
    _save(output / "item-decisions.json", decisions)
    initial = write("items", decisions=decisions)
    initial_path = Path(initial["selected"]["ITEMS"]["path"])
    baseline = json.loads(initial_path.read_text())
    _save(output / "initial-baseline.json", baseline)
    write("review", analysis_policy="skip_no_internal_evidence")
    bom = next(r for r in baseline["records"] if r["node_id"] == "bom")
    _save(
        output / "readback-bom-before.json",
        service.invoke(
            "read",
            {
                "task_id": task_id,
                "view": "item",
                "source_item_id": bom["source_display_id"],
                "part": "scope",
            },
        ),
    )
    corrected = write(
        "revise",
        command_id="correct-bom-classification",
        self_review_finding={
            "text": "The composition clause defines the product, not a delivery milestone.",
            "source_item_ids": [bom["source_display_id"]],
        },
        changes={
            "model_id": "deterministic-worked-answer",
            "decisions": [
                {
                    "source_record_id": bom["source_display_id"],
                    "requirementness": "REQUIREMENT",
                    "requirement_kinds": ["CONSTRAINT"],
                    "confidence": 0.95,
                    "decision_summary": "The original lead and component rows form one imposed product composition.",
                    "reason_codes": ["product_composition"],
                }
            ],
        },
    )

    # Native item keys are retained by the shared compiler. Read actual records
    # instead of guessing a SRC number or treating display order as identity.
    def item(key):
        return next(
            r
            for r in baseline["records"]
            if r.get("item_key") == key
            or any(
                text["text"]
                == next(
                    t[1] for s in sections for t in s.get("texts", []) if t[0] == key
                )
                for text in r.get("text_bindings", [])
            )
        )

    links = [
        ("opening", ["parent"]),
        ("closing", ["parent"]),
        ("target", ["definition", "applicability"]),
        ("verification", ["target", "definition", "applicability"]),
    ]
    context_updates = {
        item(key)["source_display_id"]: {
            "source_item_id": item(key)["source_display_id"],
            "context_source_ids": [
                item(context)["source_display_id"] for context in contexts
            ],
            "reason": "Only these explicitly governing original definitions, conditions or verification targets apply.",
        }
        for key, contexts in links
    }
    for section in sections:
        for key, text, _, _ in section.get("texts", []):
            if key in {
                "opening",
                "closing",
                "vibration",
                "thermal",
                "target",
                "verification",
                "drawing-target",
            }:
                sid = item(key)["source_display_id"]
                update = context_updates.setdefault(
                    sid,
                    {
                        "source_item_id": sid,
                        "reason": "Preserve the exact original item prefix.",
                    },
                )
                update.update(
                    source_number=text.split(" ", 1)[0],
                    source_number_ref={"kind": "ITEM_SOURCE", "segment_index": 0},
                )
    write("annotations", changes={"annotation_updates": list(context_updates.values())})
    state = service.invoke("inspect", {"task_id": task_id})
    before_noop = state["revision"]
    preserved = write("review", analysis_policy="preserve")
    if service.invoke("inspect", {"task_id": task_id})["revision"] != before_noop:
        raise ValueError("Preserving a correct review must not manufacture a revision")
    delivered = write("deliver")
    final_state = service.invoke("inspect", {"task_id": task_id})
    final_path = Path(final_state["selected"]["ITEMS"]["path"])
    final = json.loads(final_path.read_text())
    _save(
        output / "readback-bom-after.json",
        service.invoke(
            "read",
            {
                "task_id": task_id,
                "view": "item",
                "source_item_id": bom["source_display_id"],
                "part": "scope",
            },
        ),
    )
    if [r["source_record_id"] for r in final["records"]] != [
        r["source_record_id"] for r in baseline["records"]
    ]:
        raise ValueError("A local classification correction changed source identities")
    receipt = {
        "status": "GENERATED",
        "example_only": True,
        "model_calls": 0,
        "families": [
            "numbering",
            "milestones-and-parameters",
            "mixed-bom",
            "complete-tests",
            "source-context",
            "supporting-figure",
        ],
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "task_id": task_id,
        "task_root": opened["task_root"],
        "initial_baseline": str(initial_path),
        "final_baseline": str(final_path),
        "source_items": len(final["records"]),
        "stable_source_ids": True,
        "classification_correction": {
            "source_item_id": bom["source_display_id"],
            "before": ["PROJECT"],
            "after": ["CONSTRAINT"],
            "operation_state": corrected["operation_state"],
        },
        "unchanged_review": {
            "operation_state": preserved["operation_state"],
            "revision": before_noop,
        },
        "delivery": {
            "local_delivery_ready": delivered.get("local_delivery_ready"),
            "result_path": calls[-1]["result_path"],
            "selected": delivered.get("selected", {}).get("DELIVERY"),
        },
        "semantic_acceptance": "TEACHING_ANSWER_NOT_MODEL_ACCEPTANCE",
    }
    _save(output / "example-receipt.json", receipt)
    return receipt
