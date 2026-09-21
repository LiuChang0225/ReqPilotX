"""Small, source-pinned contracts shared by local evaluation commands."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parent
MODELS = {
    "gpt-5.6-luna": "max",
    "qwen3.8-max-0902": "high",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def registry():
    from jsonschema import Draft202012Validator

    cases = read(Path(os.environ.get("REQPILOTX_AGENT_EVAL_CASES", str(ROOT / "cases.json"))))
    rubrics = read(ROOT / "rubrics.json")
    for name, value in [("cases", cases), ("rubrics", rubrics)]:
        schema = read(ROOT / (name + ".schema.json"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    ids = [c["case_id"] for c in cases["cases"]]
    rubric_ids = [r["rubric_id"] for r in rubrics["rubrics"]]
    if len(ids) != len(set(ids)) or len(rubric_ids) != len(set(rubric_ids)):
        raise ValueError("Duplicate case/rubric identity")
    if any(set(c["rubric_ids"]) - set(rubric_ids) for c in cases["cases"]):
        raise ValueError("Unknown case rubric")
    for rubric in rubrics["rubrics"]:
        names = [c["check_id"] for c in rubric["checks"]]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate check identity")
    return cases, rubrics


def selected_case(case_id):
    cases, rubrics = registry()
    case = next((c for c in cases["cases"] if c["case_id"] == case_id), None)
    if case is None:
        raise ValueError("Unknown case_id")
    return case, {
        "schema_version": rubrics["schema_version"],
        "suite_id": rubrics["suite_id"],
        "rubrics": [
            r for r in rubrics["rubrics"] if r["rubric_id"] in case["rubric_ids"]
        ],
    }


def bundle_integrity(path, expected=None):
    root = Path(path).expanduser().absolute()
    if root.is_symlink():
        raise ValueError("Frozen bundle root cannot be a symlink")
    root = root.resolve(strict=True)
    receipt = read(root / "build-receipt.json")
    if receipt.get("bundle_sha256") != digest(
        {k: v for k, v in receipt.items() if k != "bundle_sha256"}
    ):
        raise ValueError("Bundle receipt hash changed")
    if expected and expected != receipt["bundle_sha256"]:
        raise ValueError("Different frozen bundle")
    # Use the trusted checkout's shared guard, never executable code from the
    # bundle under test. The guard itself reads only the frozen bundle.
    registry_path = Path(__file__).resolve().parents[2] / "agent-assets/scripts/asset_registry.py"
    if not registry_path.is_file() or registry_path.is_symlink():
        raise ValueError("Trusted Agent asset verifier is missing or unsafe")
    spec = importlib.util.spec_from_file_location("reqpilot_eval_asset_guard", registry_path)
    if spec is None or spec.loader is None:
        raise ValueError("Trusted Agent asset verifier cannot be loaded")
    assets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assets)
    asset_paths = [
        row["path"] for row in receipt["plugin_files"]
        if row["path"].startswith(("skills/", "capabilities/pdf/"))
    ]
    assets.verify_asset_inventory(root, asset_paths)
    if "agent_assets" in receipt:
        assets.verify_frozen_bundle(root)
    for area, entries in [
        (root, receipt["plugin_files"]),
        (root / "runtime/prototype", receipt["runtime_files"]),
    ]:
        for row in entries:
            p = (area / row["path"]).resolve(strict=True)
            if not p.is_relative_to(area.resolve()) or file_sha(p) != row["sha256"]:
                raise ValueError("Bundle file changed: " + row["path"])
    return receipt


def new_run_dir(path, protected=()):
    path = Path(path).expanduser().resolve()
    for root in [ROOT, *map(Path, protected)]:
        if path.is_relative_to(root.resolve()):
            raise ValueError(
                "Reports must be outside the suite and tested input/workspace"
            )
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def freeze_run(out, case, rubrics, **fields):
    save(out / "case.json", case)
    save(out / "rubrics.json", rubrics)
    value = {
        "schema_version": "reqpilot-agent-eval-run/v1",
        "run_id": "eval-" + uuid.uuid4().hex,
        "case_id": case["case_id"],
        "partition": case["partition"],
        "scope": case["scope"],
        "source_sha256": case["source_sha256"],
        "case_sha256": file_sha(out / "case.json"),
        "rubrics_sha256": file_sha(out / "rubrics.json"),
        **fields,
    }
    value["manifest_sha256"] = digest(value)
    save(out / "run.json", value)
    return value


def load_run(path):
    root = Path(path).expanduser().resolve(strict=True)
    run = read(root / "run.json")
    if run.get("schema_version") != "reqpilot-agent-eval-run/v1" or run.get(
        "manifest_sha256"
    ) != digest({k: v for k, v in run.items() if k != "manifest_sha256"}):
        raise ValueError("Run manifest changed")
    for key, file in [("case_sha256", "case.json"), ("rubrics_sha256", "rubrics.json")]:
        if file_sha(root / file) != run[key]:
            raise ValueError("Frozen case/rubric changed")
    case = read(root / "case.json")
    if any(
        run[key] != case[key]
        for key in ("case_id", "scope", "partition", "source_sha256")
    ):
        raise ValueError("Run identity differs from its frozen case")
    return root, run
