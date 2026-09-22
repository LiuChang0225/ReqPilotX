"""Separate execution, program evidence, source review and trajectory judgments."""

from __future__ import annotations

from pathlib import Path

from .contracts import (
    bundle_integrity,
    digest,
    file_sha,
    freeze_run,
    load_run,
    new_run_dir,
    read,
    save,
    selected_case,
)
from .traces import analyze


def imported(
    case_id, result_path, events, workspace, out, kind="MODEL_RUN", metadata=None
):
    case, rubrics = selected_case(case_id)
    workspace = Path(workspace).resolve(strict=True)
    original = read(result_path)
    meta = read(metadata) if metadata else {}
    inputs = read(workspace / "runtime-data/input-manifest.json")["inputs"]
    matched = [i for i in inputs if i["sha256"] == case["source_sha256"]]
    if len(matched) != 1:
        raise ValueError("History input does not uniquely match the case")
    source = (workspace / matched[0]["path"]).resolve(strict=True)
    if (
        not source.is_relative_to(workspace)
        or file_sha(source) != case["source_sha256"]
    ):
        raise ValueError("Historical source changed")
    package = workspace / ".reqpilot"
    expected = original.get("bundle_sha256") or meta.get("bundle_sha256")
    if not expected:
        raise ValueError("History lacks an independently recorded bundle identity")
    receipt = bundle_integrity(package, expected)
    out = new_run_dir(out, [workspace, Path(result_path).parent, Path(events).parent])
    external = {
        name: {"path": str(Path(path).resolve()), "sha256": file_sha(path)}
        for name, path in [
            ("result", result_path),
            ("events", events),
            *([("metadata", metadata)] if metadata else []),
        ]
    }
    run = freeze_run(
        out,
        case,
        rubrics,
        run_kind=kind,
        origin="HISTORICAL_IMPORT",
        model=original.get("model") or meta.get("model"),
        effort=original.get("effort") or meta.get("effort"),
        transport=meta.get("transport", "UNRECORDED"),
        source_path=str(source),
        workspace=str(workspace),
        bundle_path=str(package),
        bundle_sha256=receipt["bundle_sha256"],
        external=external,
        prompt_sha256=meta.get("common_business_prompt_sha256"),
        preparation=meta.get("preparation", "HISTORICAL_NOT_RECLASSIFIED"),
        historical_results_rewritten=False,
        observation_seconds=None,
        overall_timeout_seconds=meta.get("timeout_seconds"),
    )
    result = {
        k: original[k]
        for k in (
            "started_at",
            "elapsed_seconds",
            "exit_code",
            "timed_out",
            "interrupted",
            "model",
            "effort",
            "browser",
        )
        if k in original
    }
    result.update(
        run_id=run["run_id"],
        run_kind=kind,
        public_events_path=str(Path(events).resolve()),
        natural_completion=original.get("exit_code") == 0
        and not original.get("timed_out")
        and not original.get("interrupted"),
        completion_basis="ORIGINAL_RUNNER_RECORD; not delivery acceptance",
        source_sha256=case["source_sha256"],
    )
    save(out / "result.json", result)
    return {
        "status": "IMPORTED_READ_ONLY",
        "run_id": run["run_id"],
        "run_dir": str(out),
        "models_called": 0,
    }


def inspect_run(root, run):
    root = Path(root)
    result = read(root / "result.json")
    for value in run.get("external", {}).values():
        if file_sha(value["path"]) != value["sha256"]:
            raise ValueError("Historical evidence changed since import")
    events = Path(result["public_events_path"])
    start = events.parent / "start.json"
    metrics = analyze(
        events, Path(run["workspace"]), start if start.is_file() else None
    )
    # Original result duration is a ceiling for an imported observation. Later
    # legal continuation artifacts may not be relabelled as old timeout output.
    later = []
    if (
        run["origin"] == "HISTORICAL_IMPORT"
        and result.get("elapsed_seconds") is not None
    ):
        for op in metrics.get("saved_artifacts", {}).get("operations", []):
            seconds = op.get("seconds_since_run_start")
            if seconds is not None and seconds > result["elapsed_seconds"] + 2:
                later.append(op["operation_id"])
    basis = []
    for context in metrics.get("saved_artifacts", {}).get("task_context_files", []):
        index = Path(context).parent / "workspace-index.json"
        basis.append({"task_context": str(context), "index_sha256": file_sha(index)})
    return (
        result,
        metrics,
        {
            "source_sha256": run["source_sha256"],
            "task_indexes": basis,
            "evidence_basis_sha256": digest(
                {"source_sha256": run["source_sha256"], "task_indexes": basis}
            ),
            "later_artifacts": later,
        },
    )


def applicable(root, run):
    rubrics = read(root / "rubrics.json")["rubrics"]
    scope = run["scope"]
    # The first scoped-extraction runs froze the existing full-extraction rubric
    # wording before its applicability enum was updated. Reuse those exact checks
    # within the case's requested range; never rewrite a running task's rubric.
    legacy_scoped = scope == "SCOPED_EXTRACTION" and not any(
        scope in c["applies_to"] for r in rubrics for c in r["checks"]
    )
    matching_scope = "FULL_DOCUMENT" if legacy_scoped else scope
    selected = [
        (r, c)
        for r in rubrics
        for c in r["checks"]
        if matching_scope in c["applies_to"]
    ]
    if not selected:
        raise ValueError("NO_APPLICABLE_RUBRIC: an empty evaluation cannot pass")
    return selected


def review_template(run_dir, output):
    root, run = load_run(run_dir)
    _, _, basis = inspect_run(root, run)
    value = {
        "schema_version": "reqpilot-agent-eval-review/v1",
        "run_id": run["run_id"],
        "run_manifest_sha256": run["manifest_sha256"],
        "evidence_basis_sha256": basis["evidence_basis_sha256"],
        "reviewer": {"kind": "OPERATOR_REVIEW", "id": "UNASSIGNED"},
        "checks": [
            {
                "rubric_id": r["rubric_id"],
                "check_id": c["check_id"],
                "status": "REVIEW",
                "reason": "Not yet evaluated.",
                "evidence": [],
            }
            for r, c in applicable(root, run)
            if c.get("evaluator", "manual") == "manual"
        ],
    }
    ensure_report_out(output, run)
    save(output, value)
    return {
        "status": "REVIEW_TEMPLATE_WRITTEN",
        "output": str(output),
        "semantic_acceptance": "NOT_CLAIMED",
    }


def ensure_report_out(path, run):
    if Path(path).resolve().is_relative_to(Path(run["workspace"]).resolve()):
        raise ValueError("Evaluation output must stay outside the tested workspace")


def automatic(name, run, result, metrics, basis):
    artifacts = metrics.get("saved_artifacts", {})
    if name == "source_identity":
        okay = file_sha(run["source_path"]) == run["source_sha256"]
        return (
            "PASS" if okay else "FAIL",
            "Original bytes match the case." if okay else "Original bytes changed.",
        )
    if name == "bundle_integrity":
        try:
            bundle_integrity(run["bundle_path"], run["bundle_sha256"])
            return "PASS", "Frozen package files match."
        except (ValueError, OSError):
            return "FAIL", "Frozen package differs or is unavailable."
    if name == "artifact_integrity":
        if basis["later_artifacts"]:
            return "FAIL", "Evidence includes artifacts saved after the observed run."
        if artifacts.get("unverified_tasks"):
            unsupported={"ARTIFACT_KIND_UNSUPPORTED", "RECEIPT_SCHEMA_UNSUPPORTED"}
            errors=set(artifacts.get("error_codes", []))
            if errors and errors <= unsupported:
                return "REVIEW", "Evaluator lacks support for this artifact/receipt kind; no byte corruption established."
            return "FAIL", "Task/receipt/artifact integrity check failed."
        if not artifacts.get("task_context_files"):
            return "REVIEW", "No registered task artifacts are available."
        return (
            "PASS",
            "Registered task/source/receipt file integrity verified; semantic fidelity not inferred.",
        )
    if name == "required_artifacts":
        kinds = {
            kind
            for op in artifacts.get("operations", [])
            if op.get("effective_saved")
            for kind in op.get("artifact_kinds", [])
        }
        # Names from the existing shared artifact_kind_catalog, not a new ledger.
        needed = (
            {"DOCUMENT_MAP"}
            if run["scope"] == "MAP_ONLY"
            else {"DOCUMENT_MAP", "SOURCE_ITEMS", "DELIVERY"}
        )
        missing = needed - kinds
        return (
            (
                "REVIEW",
                "Required saved artifact kinds missing: " + ", ".join(sorted(missing)),
            )
            if missing
            else (
                "PASS",
                "Required registered artifact kinds exist; not full business acceptance.",
            )
        )
    if name == "autonomous_completion":
        if run["run_kind"] != "MODEL_RUN":
            return "N/A", "Deterministic replay cannot prove autonomous completion."
        if result.get("timed_out") or result.get("interrupted"):
            return "REVIEW", "Censored observation; final capability is unknown."
        complete = (
            result.get("natural_completion")
            and metrics.get("events", {}).get("terminal_integrity") == "COMPLETE"
        )
        return (
            (
                "PASS",
                "Natural model termination and public terminal event agree; delivery quality scored separately.",
            )
            if complete
            else (
                "REVIEW",
                "No confirmed natural termination and complete public terminal evidence.",
            )
        )
    return "REVIEW", "This criterion needs a source-grounded review."


def grade(run_dir, output, reviews=None):
    root, run = load_run(run_dir)
    ensure_report_out(output, run)
    result, metrics, basis = inspect_run(root, run)
    submitted = {}
    reviewer = None
    if reviews:
        review = read(reviews)
        if (
            review.get("run_id"),
            review.get("run_manifest_sha256"),
            review.get("evidence_basis_sha256"),
        ) != (run["run_id"], run["manifest_sha256"], basis["evidence_basis_sha256"]):
            raise ValueError("Review targets another run or artifact revision")
        reviewer = review["reviewer"]
        if (
            reviewer.get("kind")
            not in {"OPERATOR_REVIEW", "INDEPENDENT_AGENT", "HUMAN"}
            or not reviewer.get("id")
            or reviewer["id"] == "UNASSIGNED"
        ):
            raise ValueError("Review needs an actual reviewer identity")
        allowed = {
            (r["rubric_id"], c["check_id"])
            for r, c in applicable(root, run)
            if c.get("evaluator", "manual") == "manual"
        }
        for check in review["checks"]:
            key = (check["rubric_id"], check["check_id"])
            if key not in allowed or key in submitted:
                raise ValueError("Unknown or duplicate review criterion")
            if check["status"] not in {
                "PASS",
                "FAIL",
                "REVIEW",
                "N/A",
            } or not check.get("reason"):
                raise ValueError("Invalid review decision")
            if check["status"] in {"PASS", "FAIL", "N/A"} and not check.get("evidence"):
                raise ValueError("Conclusive review requires cited evidence")
            for evidence in check.get("evidence", []):
                if file_sha(evidence["path"]) != evidence["sha256"]:
                    raise ValueError("Review evidence changed")
            submitted[key] = check
    checks = []
    for rubric, criterion in applicable(root, run):
        name = criterion.get("evaluator", "manual")
        key = (rubric["rubric_id"], criterion["check_id"])
        if name != "manual":
            status, reason = automatic(name, run, result, metrics, basis)
            evidence = []
            author = "PROGRAM"
        else:
            choice = submitted.get(key, {})
            status = choice.get("status", "REVIEW")
            reason = choice.get(
                "reason", "No independent source/trajectory review supplied."
            )
            evidence = choice.get("evidence", [])
            author = reviewer if choice else "UNASSIGNED"
        checks.append(
            {
                "rubric_id": key[0],
                "check_id": key[1],
                "layer": criterion["layer"],
                "severity": criterion["severity"],
                "status": status,
                "reason": reason,
                "evaluator": author,
                "evidence": evidence,
            }
        )
    failures = [c for c in checks if c["status"] == "FAIL"]
    pending = [c for c in checks if c["status"] == "REVIEW"]
    invalid = any(
        c["status"] == "FAIL"
        and c["check_id"] in {"source-sha256", "frozen-bundle", "artifact-hashes"}
        for c in checks
    )
    verdict = (
        "INVALID_EVIDENCE"
        if invalid
        else "FAIL"
        if failures
        else "REVIEW"
        if pending
        else "PASS"
    )
    execution = (
        "CENSORED"
        if result.get("timed_out") or result.get("interrupted")
        else "NATURAL_TERMINATION"
        if result.get("natural_completion")
        else "ENVIRONMENT_BLOCKED"
        if any(str(code).startswith("HOST_") for code in metrics.get("error_codes", []))
        else "INCOMPLETE_OR_RUNTIME_FAILURE"
    )
    value = {
        "schema_version": "reqpilot-agent-eval-report/v1",
        "run_id": run["run_id"],
        "case_id": run["case_id"],
        "run_kind": run["run_kind"],
        "origin": run["origin"],
        "partition": run["partition"],
        "scope": run["scope"],
        "criterion_scope": {
            "scope": run["scope"],
            "coverage_basis": "EXPLICIT_CASE_RANGE" if run["scope"] == "SCOPED_EXTRACTION" else "CASE_SCOPE",
            "legacy_full_extraction_checks": run["scope"] == "SCOPED_EXTRACTION" and not any(
                run["scope"] in c["applies_to"]
                for r in read(root / "rubrics.json")["rubrics"] for c in r["checks"]
            ),
        },
        "tested_workspace": run["workspace"],
        "execution_classification": execution,
        "model": run.get("model"),
        "host": run.get("host", "codex"),
        "effort": run.get("effort"),
        "transport": result.get("transport_actual", run.get("transport")),
        "planned_transport": run.get("transport"),
        "tool_events_available": result.get(
            "tool_events_available", run.get("host") != "antigravity-desktop"
        ),
        "trace_coverage": result.get("trace_coverage", "Recorded public host events"),
        "verdict": verdict,
        "agent_acceptance": verdict
        if run["run_kind"] == "MODEL_RUN"
        else "NOT_A_MODEL_RUN",
        "human_acceptance": "NOT_CLAIMED",
        "completion": {
            k: result.get(k)
            for k in (
                "natural_completion",
                "timed_out",
                "interrupted",
                "elapsed_seconds",
            )
        },
        "identities": {
            k: run[k]
            for k in (
                "manifest_sha256",
                "case_sha256",
                "rubrics_sha256",
                "source_sha256",
                "bundle_sha256",
            )
        },
        "prompt_sha256": run.get("prompt_sha256"),
        "evaluated_result_sha256": file_sha(root / "result.json"),
        "grader_sha256": file_sha(Path(__file__)),
        "trace_analyzer_sha256": file_sha(Path(__file__).with_name("traces.py")),
        "review_file_sha256": file_sha(reviews) if reviews else None,
        "evidence_basis": basis,
        "checks": checks,
        "metrics": metrics,
        "comparison_limit": "One observation is not a repeatability or generalization estimate.",
    }
    save(output, value)
    return value


def table(reports, output):
    values = [read(p) for p in reports]
    for value in values:
        if (
            Path(output)
            .resolve()
            .is_relative_to(Path(value["tested_workspace"]).resolve())
        ):
            raise ValueError("Report must stay outside tested workspaces")
    lines = [
        "# Agent Eval observations",
        "",
        "| Case | Model | Scope / origin | Completion | Verdict | Calls |",
        "|---|---|---|---|---|---|",
    ]
    for v in values:
        counts = v["metrics"].get("events", {})
        state = v["execution_classification"]
        cells = [
            v["case_id"],
            str(v.get("model")),
            v["scope"] + " / " + v["origin"],
            state,
            v["verdict"],
            str(counts.get("unique_tool_calls", "unavailable"))
            if v.get("tool_events_available", True)
            else "unavailable",
        ]
        lines.append(
            "| "
            + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells)
            + " |"
        )
    lines += [
        "",
        "These are separate observations, not an aggregate model ranking. REVIEW is unresolved evidence, not a pass. Program replay is not autonomous Agent performance.",
    ]
    path = Path(output)
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return {"status": "REPORT_WRITTEN", "output": str(path), "runs": len(values)}
