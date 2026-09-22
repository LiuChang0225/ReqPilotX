"""Frozen stage inputs and source-bound relation checks; never an extraction agent.

Recipes live outside the model workspace. Seeding uses the tested bundle's real
task service, and its pre-model operations are explicitly excluded from traces.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .contracts import digest, file_sha, read, save


def inventory(root):
    return {
        str(p.relative_to(root)): file_sha(p)
        for p in sorted((root / "runtime-data").rglob("*"))
        if p.is_file()
    }


def seed(workspace, source, recipe_path, python, workbench_url, goal, operator):
    recipe = read(recipe_path)
    if recipe["source_sha256"] != file_sha(source):
        raise ValueError("Stage recipe is for a different original")
    script = Path(__file__).resolve().parents[2] / "prototype/scripts/agent_eval.py"
    request = {
        "workspace": str(workspace),
        "source": str(source),
        "recipe": recipe,
        "workbench_url": workbench_url,
        "goal": goal,
    }
    process = subprocess.run(
        [str(python), str(script), "seed-stage-internal"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(process.stdout)
    save(operator / "stage-seed.json", result)
    save(operator / "stage-recipe.json", recipe)
    files = inventory(workspace)
    save(operator / "stage-files.json", files)
    note = (
        "这是现有任务的阶段输入，由评测准备程序生成，未计为本轮 Agent 成果。\n"
        f"当前 task_id: {result['task_id']}\n"
        "原件、已保存的 Document Map 和原文条目均在本工作区。"
        "请按用户任务继续处理并实际保存成果。\n"
    )
    (workspace / "stage-input.md").write_text(note)
    return {
        "recipe_sha256": file_sha(operator / "stage-recipe.json"),
        "seed_sha256": file_sha(operator / "stage-seed.json"),
        "files_sha256": digest(files),
        "note_sha256": file_sha(workspace / "stage-input.md"),
        "task_id": result["task_id"],
        "task_root": result["task_root"],
    }


def verify_seed(root, run):
    evidence = run["stage_fixture"]
    for name, key in [
        ("stage-recipe.json", "recipe_sha256"),
        ("stage-seed.json", "seed_sha256"),
    ]:
        if file_sha(root / "operator" / name) != evidence[key]:
            raise ValueError("Frozen stage input changed")
    files = read(root / "operator/stage-files.json")
    if (
        digest(files) != evidence["files_sha256"]
        or inventory(Path(run["workspace"])) != files
    ):
        raise ValueError("Prepared stage workspace changed before model execution")
    if file_sha(Path(run["workspace"]) / "stage-input.md") != evidence["note_sha256"]:
        raise ValueError("Stage task note changed")


def seed_internal(request):
    """Subprocess-only: imports the selected portable runtime, not checkout code."""
    workspace = Path(request["workspace"])
    package = workspace / ".reqpilot"
    sys.path.insert(0, str(package / "scripts"))
    from reqpilot import load_runtime

    load_runtime(None)
    from task_service import TaskService

    service = TaskService(workspace, package / "scripts/reqpilot.py")
    recipe = request["recipe"]
    state = service.invoke(
        "open",
        {
            "mode": "create",
            "source": request["source"],
            "goal": request["goal"],
            "creation_key": "stage-fixture-v1",
            "workbench_url": request["workbench_url"],
        },
    )
    task_id = state["task_id"]

    def write(action, **fields):
        current = service.invoke("inspect", {"task_id": task_id})
        value = service.invoke(
            action,
            {"task_id": task_id, "expected_revision": current["revision"], **fields},
        )
        if value.get("operation_state") != "COMMITTED":
            raise ValueError(f"Stage preparation {action} did not commit: {value}")
        return value

    state = write("prepare", mode="capture")
    task_root = Path(state["task_root"])
    captures = list(task_root.rglob("source_ir.json"))
    if len(captures) != 1:
        raise ValueError("Stage input must have one unambiguous initial capture")
    ir = read(captures[0])

    def norm(text):
        return " ".join(text.split())

    def source_id(text):
        matches = [
            n["source_node_id"]
            for n in ir["nodes"]
            if norm(n.get("raw_text", "")) == norm(text)
        ]
        if len(matches) != 1:
            raise ValueError(
                "Fixture source text must match one actual captured block: " + text
            )
        return matches[0]

    nodes = [
        {
            "node_id": "document",
            "kind": "DOCUMENT",
            "parent_id": None,
            "title": recipe["title"],
            "title_source_ids": [source_id(recipe["title"])],
        }
    ]
    for unit in recipe["units"]:
        nodes.append(
            {
                "node_id": unit["key"],
                "kind": "SECTION",
                "parent_id": unit.get("parent", "document"),
                "label": unit["number"],
                "title": unit["heading"],
                "title_source_ids": [source_id(unit["number"] + " " + unit["heading"])],
                "content_source_ids": [source_id(unit["body"])],
            }
        )
    state = write(
        "map", decisions={"schema_version": "reqpilot-map-edit/v1", "nodes": nodes}
    )
    requests = read(
        Path(state["selected"]["MAP"]["output_dir"]) / "scope-requests.json"
    )
    units = {u["key"]: u for u in recipe["units"]}
    decisions = []
    for scope in requests["text_requests"]:
        if scope["node_id"] not in units:
            continue
        unit = units[scope["node_id"]]
        content = [c for c in scope["source_content"] if not c["is_title"]]
        if len(content) != 1 or norm(content[0]["text"]) != norm(unit["body"]):
            raise ValueError("Unexpected fixture body scope")
        record = {
            "item_key": unit["key"],
            "label": unit["body"],
            "source_form": unit.get("source_form", "NUMBERED_CLAUSE"),
            "text_spans": [
                {
                    "content_id": content[0]["content_id"],
                    "start_offset": 0,
                    "end_offset": len(content[0]["text"]),
                }
            ],
            "requirementness": unit["requirementness"],
            "requirement_kinds": unit.get("requirement_kinds", []),
            "confidence": 0.95,
            "decision_summary": unit["summary"],
            "reason_codes": ["prepared_stage_input"],
        }
        if unit.get("non_requirement_role"):
            record["non_requirement_role"] = unit["non_requirement_role"]
        decisions.append({"scope_id": scope["scope_id"], "items": [record]})
    state = write(
        "items", decisions={"retain_map_structure": True, "text_decisions": decisions}
    )
    baseline = read(state["selected"]["ITEMS"]["path"])
    by_key = {r["node_id"]: r["source_record_id"] for r in baseline["records"]}
    if recipe.get("initial_links"):
        write(
            "annotations",
            changes={
                "annotation_updates": [
                    {
                        "source_item_id": by_key[key],
                        "context_source_ids": [by_key[c] for c in contexts],
                        "reason": "Existing candidate association supplied for stage review.",
                    }
                    for key, contexts in recipe["initial_links"].items()
                ]
            },
        )
    write("review", analysis_policy="skip_no_internal_evidence")
    return {
        "preparation": "STAGE_FIXTURE",
        "model_produced": False,
        "task_id": task_id,
        "task_root": str(task_root),
        "baseline_sha256": baseline["artifact_sha256"],
        "map_sha256": baseline["document_map_artifact_sha256"],
        "source_bindings": by_key,
    }


def relation_checks(baseline, annotations, oracle):
    """Match by source quotations, never expected SRC labels or record counts.

    A quotation must identify one current original unit. A changed unit boundary
    is REVIEW_REQUIRED, not silently scored as either a correct or wrong link.
    """
    records = baseline["records"]
    resolved = {}
    ambiguous = []
    for key, quote in oracle["selectors"].items():
        matches = [
            r["source_record_id"]
            for r in records
            if quote
            in " ".join(
                " ".join(b.get("text", "") for b in r.get("text_bindings", [])).split()
            )
        ]
        if len(matches) != 1:
            ambiguous.append(key)
        else:
            resolved[key] = matches[0]
    if ambiguous:
        return {
            "verdict": "REVIEW_REQUIRED",
            "unresolved_source_selectors": ambiguous,
            "checks": [],
        }
    rows = []
    for kind in ("must_link", "must_not_link"):
        for owner, context in oracle[kind]:
            links = (
                annotations.get("records", {})
                .get(resolved[owner], {})
                .get("context_source_ids", [])
            )
            present = resolved[context] in links
            rows.append(
                {
                    "kind": kind,
                    "owner": owner,
                    "context": context,
                    "passed": present if kind == "must_link" else not present,
                }
            )
    return {
        "verdict": "PASS" if all(r["passed"] for r in rows) else "FAIL",
        "checks": rows,
    }


def grade_stage(run_dir, oracle_path, out):
    from .contracts import load_run

    root, run = load_run(run_dir)
    if run.get("preparation") != "STAGE_FIXTURE":
        raise ValueError("Stage grader requires a declared stage fixture")
    oracle = read(oracle_path)
    if oracle["source_sha256"] != run["source_sha256"]:
        raise ValueError("Oracle source differs from run")
    # Freeze before first execution: a changed oracle must be a new evaluation.
    if file_sha(oracle_path) != read(root / "case.json")["oracle_sha256"]:
        raise ValueError("Oracle changed after case preparation")
    task_root = Path(run["stage_fixture"]["task_root"])
    review = task_root / "review"
    head = read(review / "workspace-manifest.json")
    version = (review / head["current"]["version_path"]).resolve(strict=True)
    if not version.is_relative_to(review.resolve()):
        raise ValueError("Review version escapes its task")
    for name, sha in head["current"]["files"].items():
        path = (version / name).resolve(strict=True)
        if not path.is_relative_to(version) or file_sha(path) != sha:
            raise ValueError("Current review artifact changed")
    baseline = read(version / "source-requirement-baseline.json")
    annotations = (
        read(version / "image-annotations.json")
        if (version / "image-annotations.json").exists()
        else {}
    )
    relations = relation_checks(baseline, annotations, oracle)
    preserved = (
        baseline["artifact_sha256"]
        == read(root / "operator/stage-seed.json")["baseline_sha256"]
    )
    result = read(root / "result.json")
    report = {
        "schema_version": "reqpilot-stage-eval/v1",
        "run_id": run["run_id"],
        "case_id": run["case_id"],
        "preparation": "STAGE_FIXTURE",
        "level": "L2",
        "model": run["model"],
        "effort": run["effort"],
        "bundle_sha256": run["bundle_sha256"],
        "run_manifest_sha256": run["manifest_sha256"],
        "oracle_sha256": file_sha(oracle_path),
        "review_sha256": head["review_sha256"],
        "baseline_preserved": preserved,
        "relations": relations,
        "execution_completed": result.get("public_turn_completed", False),
        "verdict": relations["verdict"] if preserved else "REVIEW_REQUIRED",
        "semantic_review": "OPERATOR_SOURCE_BOUND_ASSERTIONS; no independent model grader",
        "limits": "Stage context relations only; not whole-document extraction or release acceptance.",
    }
    if not report["execution_completed"]:
        report["verdict"] = "INCOMPLETE"
    save(out, report)
    return report
