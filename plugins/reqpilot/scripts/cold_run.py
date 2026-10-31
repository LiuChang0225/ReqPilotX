#!/usr/bin/env python3
"""Run a new Codex validation with verified, invocation-only skill isolation.

Does not change global configuration, authentication, HOME or CODEX_HOME. The
prompt inventory is host-generated input, not prior model output or reasoning.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from portable_process import isolation_kwargs, stop_timed_out_process


def text_values(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "text" and isinstance(child, str):
                yield child
            else:
                yield from text_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from text_values(child)


def skill_inventory(payload: object) -> list[dict]:
    skills = []
    for text in text_values(payload):
        if "### Available skills" not in text:
            continue
        roots = dict(re.findall(r"- `(r[0-9]+)` = `([^`]+)`", text))
        for name, path in re.findall(r"^- ([a-zA-Z0-9:_-]+):[^\n]*\(file: ([^)]+/SKILL\.md)\)", text, re.MULTILINE):
            first, _, rest = path.partition("/")
            expanded = Path(roots[first]) / rest if first in roots else Path(path)
            skills.append({"name": name, "path": str(expanded), "resolved_path": str(expanded.resolve())})
    if not skills:
        raise ValueError("Host prompt did not expose a parseable skill inventory; do not claim isolated discovery")
    return skills


def run_probe(binary: str, workspace: Path, output: Path, config: list[str], *, persist: bool = True) -> list[dict]:
    command = [binary, "debug", "prompt-input", "--disable", "plugins", *config, "Inspect the project AGENTS and skill inventory only. Do not execute document work."]
    result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=False)
    if persist:
        output.write_text(result.stdout, encoding="utf-8")
        output.chmod(0o600)
        output.with_suffix(".stderr").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise ValueError(f"Prompt inventory probe failed with exit {result.returncode}; see private probe stderr")
    return skill_inventory(json.loads(result.stdout))


def prepare(binary: str, workspace: Path, output: Path) -> tuple[list[str], dict]:
    output.mkdir(parents=True, mode=0o700)
    plugin = (workspace / ".reqpilot").resolve(strict=True)
    # Do not expose the pre-isolation global skill context as a readable file to
    # the subsequently tested Agent. Only the sanitized inventory is retained.
    baseline = run_probe(binary, workspace, output / "prompt-before.json", [], persist=False)
    def local(skill):
        path = Path(skill["resolved_path"])
        if plugin in path.parents:
            return True
        copied_root = workspace / ".agents/skills"
        if path.parent.parent != copied_root or path.name != "SKILL.md":
            return False
        canonical = plugin / "skills" / path.parent.name
        if not canonical.is_dir():
            return False
        def hashes(directory):
            return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        return hashes(path.parent) == hashes(canonical)
    excluded = [skill for skill in baseline if not local(skill)]
    paths = sorted({path for skill in excluded for path in (skill["path"], skill["resolved_path"])})
    # JSON string quoting is used inside an argv TOML value, never shell code.
    config = ["-c", "skills.config=[" + ",".join("{path=" + json.dumps(path) + ",enabled=false}" for path in paths) + "]",
              '-c', 'project_doc_max_bytes=0']
    isolated = run_probe(binary, workspace, output / "prompt-isolated.json", config)
    unexpected = [skill for skill in isolated if not local(skill)]
    expected_names = {path.name for path in (plugin / "skills").iterdir() if (path / "SKILL.md").is_file()}
    observed_names = {skill["name"].rsplit(":", 1)[-1] for skill in isolated}
    if unexpected or observed_names != expected_names:
        raise ValueError("Per-invocation isolation did not produce exactly the packaged skill set; no model run was started")
    receipt = {"schema_version": "reqpilot-cold-discovery/v1", "host_binary": binary, "global_configuration_modified": False, "authentication_home_modified": False, "included_skills": isolated, "disabled_skill_count": len(excluded), "disabled_skill_path_sha256": [hashlib.sha256(skill["path"].encode("utf-8")).hexdigest() for skill in excluded], "project_agents_sha256": hashlib.sha256((workspace / "AGENTS.md").read_bytes()).hexdigest(), "bundle_receipt_sha256_before": hashlib.sha256((plugin / "build-receipt.json").read_bytes()).hexdigest(), "prompt_isolation": "VERIFIED_SKILL_INVENTORY", "prior_task_context": "NEW_EXEC_NO_RESUME", "semantic_execution": "NOT_RUN"}
    (output / "discovery-receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return config, receipt


def workbench_network_configuration(url):
    if url is None:
        return []
    parsed=urlsplit(url)
    if (parsed.scheme not in {'http','https'} or parsed.hostname not in {'127.0.0.1','localhost','::1'}
            or parsed.username or parsed.password):
        raise ValueError('Cold workbench test requires an explicit local application URL')
    query=parse_qs(parsed.query)
    if not query.get('projectId') or not query.get('documentId'):
        raise ValueError('Cold workbench URL must identify its actual project and document')
    return ['-c','sandbox_workspace_write.network_access=true',
        '-c','features.network_proxy.enabled=true',
        '-c','features.network_proxy.domains={' + json.dumps(parsed.hostname) + '="allow"}']


def execution_command(binary, workspace, output, config, *, model, effort, allow_delegation=False, workbench_url=None):
    if not model or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*', model):
        raise ValueError('Cold execution requires an explicit model identifier')
    if effort not in {'low', 'medium', 'high', 'xhigh', 'max'}:
        raise ValueError('Cold execution requires an explicit reasoning effort')
    return [binary, 'exec', '--ignore-user-config', '--disable', 'plugins',
        '--enable' if allow_delegation else '--disable', 'multi_agent',
        '--model', model, '-c', 'model_reasoning_effort=' + json.dumps(effort),
        '--skip-git-repo-check', '--sandbox', 'workspace-write', '-c', 'approval_policy="never"',
        *config, *workbench_network_configuration(workbench_url), '--cd', str(workspace), '--json', '--output-last-message', str(output / 'final.md'), '-']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument('--model', help='Required for execution; never silently use the host default')
    parser.add_argument('--reasoning-effort', choices=['low','medium','high','xhigh','max'])
    parser.add_argument('--timeout-seconds', type=int, default=900)
    parser.add_argument('--allow-delegation', action='store_true', help='Only for explicitly authorized multi-agent tests')
    parser.add_argument('--workbench-url', help='Explicit project/document URL for a local workbench test; enables command networking only to its loopback host')
    parser.add_argument("--review-dir", type=Path, help="Explicit supplied predecessor for a fresh-context revision test; never called a source-only extraction test")
    args = parser.parse_args()
    if not args.prepare_only and (not args.model or not args.reasoning_effort):
        parser.error('Execution requires --model and --reasoning-effort')
    if not 1 <= args.timeout_seconds <= 7200:
        parser.error('--timeout-seconds must be between 1 and 7200')
    workspace = args.workspace.expanduser().resolve(strict=True)
    workbench_network_configuration(args.workbench_url)
    review_path = None
    review_before = None
    if args.review_dir:
        review_path = (workspace / args.review_dir).resolve(strict=True)
        if workspace not in review_path.parents:
            raise ValueError("Supplied review must be inside the cold workspace")
        review_before = json.loads((review_path / "workspace-manifest.json").read_text())
        if review_before.get("schema_version") != "reqpilot-review-workspace/v1":
            raise ValueError("Revision test needs an imported source-bound review manifest")
    if not args.prepare_only and not review_path:
        prior_artifacts = [path for path in (workspace / "runtime-data").rglob("*") if path.name in {"source_ir.json", "document-map.json", "source-requirement-baseline.json", "matrix-snapshot.json", "matrix.xlsx"}]
        if prior_artifacts:
            raise ValueError("Cold execution refuses a workspace containing prior captures or results; bootstrap a new workspace from raw input")
    output = workspace / "runtime-data/cold-session"
    if output.exists():
        raise ValueError("Cold session output already exists; use a fresh workspace or a distinct test")
    config, receipt = prepare(args.codex_binary, workspace, output)
    original_build = json.loads((workspace / ".reqpilot/build-receipt.json").read_text(encoding="utf-8"))
    if args.prepare_only:
        print(json.dumps({"status": "DISCOVERY_VERIFIED", "skills": len(receipt["included_skills"]), "output": str(output), "execution": "NOT_RUN"}))
        return 0
    # Parent repository/global AGENTS must not silently become test background.
    # Only this bootstrapped workspace's actual instructions are included.
    prompt = 'Workspace instructions from AGENTS.md:\n' + (workspace / 'AGENTS.md').read_text(encoding='utf-8')
    prompt += '\n\nCurrent user request:\n' + (workspace / "request.md").read_text(encoding="utf-8")
    prompt += "\nFor this fresh-context validation use only this workspace's AGENTS, packaged ReqPilotX skills/tools and explicitly supplied inputs. Do not read another project or chat. Use .venv/bin/python when the workspace virtual environment exists. Report actual child-agent execution and named missing capabilities.\n"
    if args.workbench_url:
        prompt += '\nThe supplied original is registered in this task’s ReqPilotX workbench: '+args.workbench_url+'\nUse only this explicitly supplied project/document for workbench interaction; retain the source/version binding. Command networking to its loopback host is configured for this run.\n'
    if review_path:
        prompt += f"This is a revision test with an explicitly supplied predecessor at {review_path.relative_to(workspace)}. Its current baseline, annotations, derived/analysis state and feedback are authorized inputs, not expected answers. Read originals independently, produce supported corrections, preserve unaffected state, export and close the review. Do not claim source-only cold extraction.\n"
    else:
        prompt += "This is a source-only extraction test. Do not read historical candidates or feedback.\n"
    prompt += f'\nThis run has a {args.timeout_seconds}-second upper bound, not a target duration. Save useful source-bound scope artifacts and a factual handoff before exhaustion.\n'
    command = execution_command(args.codex_binary, workspace, output, config,
        model=args.model, effort=args.reasoning_effort, allow_delegation=args.allow_delegation,workbench_url=args.workbench_url)
    print(json.dumps({"status": "STARTING_COLD_SESSION", "skill_count": len(receipt["included_skills"]), "output": str(output)}), flush=True)
    started = time.monotonic()
    (output / 'execution-start.json').write_text(json.dumps({
        'started_at':datetime.now(timezone.utc).isoformat(),
        'started_epoch_seconds':time.time(), 'clock':'HOST_WALL_CLOCK',
        'semantic_execution':'STARTING'
    }, indent=2) + '\n', encoding='utf-8')
    timed_out = False
    environment=os.environ.copy()
    if args.workbench_url:
        environment['REQPILOT_WORKBENCH_URL']=args.workbench_url
    with (output / "events.jsonl").open("w", encoding="utf-8") as stdout, (output / "stderr.log").open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=workspace, env=environment, stdin=subprocess.PIPE,
                                   text=True, stdout=stdout, stderr=stderr, **isolation_kwargs())
        try:
            process.communicate(prompt, timeout=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            for force, interrupt in ((False, True), (False, False), (True, False)):
                stop_timed_out_process(process, force=force, interrupt=interrupt)
                try:
                    process.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired: pass
    exit_code = 124 if timed_out else process.returncode
    # The full argv contains disabled host-skill paths. Retain it only after the
    # model exits, so qualification inputs contain no ambient project hints.
    (output / "invocation.json").write_text(json.dumps({"argv": command, "stdin": prompt}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    from summarize_execution import summarize
    evidence = summarize(workspace, output / "events.jsonl", exit_code)
    evidence['execution_configuration'] = {'model':args.model, 'reasoning_effort':args.reasoning_effort,
        'allow_delegation':args.allow_delegation, 'timeout_seconds':args.timeout_seconds,
        'timed_out':timed_out, 'elapsed_seconds':round(time.monotonic()-started,3),
        'workspace_agents_explicitly_supplied':True, 'ambient_project_agents_disabled':True}
    evidence['execution_configuration']['workbench_network'] = {
        'enabled':bool(args.workbench_url), 'url':args.workbench_url,
        'command_host_allowlist':[urlsplit(args.workbench_url).hostname] if args.workbench_url else [],
        'proxy_enforcement_configured':bool(args.workbench_url)}
    evidence["validation_mode"] = "FRESH_CONTEXT_REVISION_WITH_SUPPLIED_PREDECESSOR" if review_path else "SOURCE_ONLY_COLD_EXTRACTION"
    if review_path:
        review_after = json.loads((review_path / "workspace-manifest.json").read_text())
        evidence["review_execution"] = {"path": str(review_path.relative_to(workspace)), "before_review_sha256": review_before["review_sha256"],
            "after_review_sha256": review_after["review_sha256"], "before_baseline_sha256": review_before["current"]["baseline_sha256"],
            "after_baseline_sha256": review_after["current"]["baseline_sha256"], "review_advanced": review_after["current"] != review_before["current"],
            "delivery": review_after.get("delivery"), "semantic_verification": "REQUIRES_INDEPENDENT_SOURCE_REVIEW"}
    mismatches = []
    for entry in original_build.get("plugin_files", []):
        path = workspace / ".reqpilot" / entry["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            mismatches.append(entry["path"])
    for entry in original_build["runtime_files"]:
        path = workspace / ".reqpilot/runtime/prototype" / entry["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            mismatches.append("runtime/prototype/" + entry["path"])
    evidence["package_integrity_after_run"] = {"status": "UNCHANGED" if not mismatches else "MODIFIED_DURING_TEST", "mismatches": mismatches}
    (output / "execution-evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "TIMED_OUT" if timed_out else "EXITED", "exit_code": exit_code, "output": str(output), "semantic_pass": evidence["semantic_pass"]}))
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "COLD_TEST_NOT_STARTED", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
