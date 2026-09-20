"""Two small, invented L2 originals; operator-only recipes and source oracles."""

from pathlib import Path

from .contracts import file_sha, save


GOAL = (
    "请复核并完善当前任务已保存的原文条目，重点核对各条目独立阅读时所需的原文定义、"
    "条件和适用范围是否准确关联。对有原文依据的问题直接修订，保留正确内容和原文次序，"
    "交付当前工作台成果及同版Excel，说明实际改动和仍未解决的范围。"
    "本次不建立派生子项，未提供内部知识依据，不进行产品符合性判断；无需等待人工逐项确认。"
)


def unit(
    key,
    number,
    heading,
    body,
    *,
    role=None,
    parent=None,
    page=1,
    source_form="NUMBERED_CLAUSE",
):
    return {
        "key": key,
        "number": number,
        "heading": heading,
        "body": body,
        "page": page,
        "parent": parent or "document",
        "requirementness": "NON_REQUIREMENT" if role else "REQUIREMENT",
        "requirement_kinds": [] if role else ["QUALITY"],
        "source_form": source_form,
        "non_requirement_role": role,
        "summary": "Source definition/condition."
        if role
        else "Original measurable target.",
    }


CASES = [
    {
        "key": "stage-context-definition-v2",
        "title": "A17 control specification",
        "filename": "A17-control-spec.pdf",
        "units": [
            unit(
                "definition",
                "1.1",
                "Settling interval",
                "Throughout this specification, settling interval is the time from a command edge until position remains within 2 percent of its target.",
                role="DEFINITION",
            ),
            unit(
                "opening",
                "3.1",
                "Opening motion",
                "The settling interval during opening shall not exceed 45 ms.",
                page=2,
            ),
            unit(
                "closing",
                "3.2",
                "Closing motion",
                "The settling interval during closing shall not exceed 70 ms.",
                page=2,
            ),
            unit(
                "insulation",
                "3.3",
                "Insulation",
                "Insulation resistance shall be at least 8 Mohm.",
                page=2,
            ),
        ],
        "initial_links": {},
        "must_link": [["opening", "definition"], ["closing", "definition"]],
        "must_not_link": [["insulation", "definition"]],
    },
    {
        "key": "stage-context-bounded-scope-v2",
        "title": "B29 module specification",
        "filename": "B29-module-spec.pdf",
        "units": [
            unit(
                "conditions",
                "4",
                "Bench targets",
                "The conditions 28 V and 35 C apply only to clauses 4.1 and 4.2.",
                role="CONTEXT",
                source_form="PARAGRAPH",
            ),
            unit(
                "current",
                "4.1",
                "Idle current",
                "Idle current shall not exceed 3 mA.",
                parent="conditions",
            ),
            unit(
                "travel",
                "4.2",
                "Travel duration",
                "Travel duration shall not exceed 90 ms.",
                parent="conditions",
            ),
            unit(
                "cold",
                "4.3",
                "Cold start",
                "At 20 V and minus 25 C, start-up shall complete within 800 ms.",
                parent="conditions",
            ),
        ],
        "initial_links": {"current": ["conditions"], "cold": ["conditions"]},
        "must_link": [["current", "conditions"], ["travel", "conditions"]],
        "must_not_link": [["cold", "conditions"]],
    },
]


def generate(out):
    import pymupdf

    out = Path(out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    bindings = {"sources": {}, "stage_fixtures": {}}
    catalogue = []
    for case in CASES:
        folder = out / case["key"]
        folder.mkdir()
        source = folder / case["filename"]
        doc = pymupdf.open()
        for page_number in range(1, max(u["page"] for u in case["units"]) + 1):
            page = doc.new_page()
            y = 46
            if page_number == 1:
                page.insert_text((40, y), case["title"], fontsize=15)
                y += 65
            for u in [u for u in case["units"] if u["page"] == page_number]:
                page.insert_text((40, y), u["number"] + " " + u["heading"], fontsize=12)
                y += 22
                spare = page.insert_textbox(
                    pymupdf.Rect(40, y, 555, y + 72), u["body"], fontsize=11
                )
                if spare < 0:
                    raise ValueError("Micro source text would be clipped")
                y += 103
        doc.save(source, no_new_id=True)
        doc.close()
        sha = file_sha(source)
        recipe = {k: case[k] for k in ("title", "units", "initial_links")}
        recipe["source_sha256"] = sha
        recipe_path = folder / "recipe.json"
        save(recipe_path, recipe)
        oracle = {
            "source_sha256": sha,
            "selectors": {u["key"]: u["body"] for u in case["units"]},
            "must_link": case["must_link"],
            "must_not_link": case["must_not_link"],
        }
        oracle_path = folder / "oracle.json"
        save(oracle_path, oracle)
        key = case["key"]
        bindings["sources"][key] = {"path": str(source), "sha256": sha}
        bindings["stage_fixtures"][key] = {
            "path": str(recipe_path),
            "sha256": file_sha(recipe_path),
        }
        catalogue.append(
            {
                "case_id": key,
                "title": case["title"] + "：现有条目关联复核",
                "source_key": key,
                "source_sha256": sha,
                "partition": "development",
                "status": "READY_WITH_PRIVATE_BINDING",
                "scope": "SCOPED_REVISION",
                "preparation": "STAGE_FIXTURE",
                "fixture_key": key,
                "fixture_sha256": file_sha(recipe_path),
                "oracle_sha256": file_sha(oracle_path),
                "task_goal": GOAL,
                "tags": ["synthetic", "stage-fixture", "context-links"],
                "rubric_ids": [
                    "source_items",
                    "delivery_consistency",
                    "evidence_discipline",
                ],
                "capability_focus": ["source_items", "delivery_consistency"],
                "agent_visible_fields": ["task_goal"],
            }
        )
    save(out / "private-bindings.json", bindings)
    save(out / "case-entries.json", catalogue)
    return {"status": "GENERATED", "models_called": 0, "out": str(out)}
