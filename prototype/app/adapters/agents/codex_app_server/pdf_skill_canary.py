"""B1-C2 explicit-Skill and vision canary over a synthetic technical PDF."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping, Sequence

from app.adapters.agents.codex_app_server.lifecycle import (
    DEFAULT_CODEX_BIN,
    DEFAULT_PREFERRED_MODEL,
    CanaryRuntimeState,
    EventLedger,
    JsonlRpcSession,
    LifecycleProbeError,
    _assert_persistent_markers_absent,
    _canary_runtime_state,
    _delete_thread_with_fresh_session,
    _open_runtime_session,
    _preflight_session,
    _start_or_resume_thread,
    _validate_turn_start_result,
    _verify_thread_absent_after_restart,
)
from app.adapters.agents.codex_app_server.pdf_tool_host import (
    PdfCanaryContext,
    canonical_json,
    canonical_sha256,
    pdf_understanding_task_payload,
    prepare_pdf_canary,
    structure_task_payload,
    validate_structure_proposal,
    validate_understanding_proposal,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    VISUAL_CANARY_CODE,
)


PDF_SKILL_CANARY_SCHEMA = "reqpilotx-codex-pdf-skill-canary/v1"
_PDF_SKILL = "reqpilotx-pdf-understanding"
_STRUCTURE_SKILL = "reqpilotx-structure-tree-generation"
_EXPECTED_SKILLS = frozenset({_PDF_SKILL, _STRUCTURE_SKILL})
_SCHEMA_DIR = (
    Path(__file__).resolve().parents[4] / "spikes/codex_app_server/schemas"
)
_PDF_SCHEMA_PATH = _SCHEMA_DIR / "document_understanding_proposal_v1.schema.json"
_STRUCTURE_SCHEMA_PATH = _SCHEMA_DIR / "structure_proposal_v1.schema.json"


def run_pdf_skill_canary(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if model != DEFAULT_PREFERRED_MODEL:
        raise LifecycleProbeError("MODEL_NOT_PINNED")
    if timeout_seconds <= 0:
        raise LifecycleProbeError("TIMEOUT_INVALID")
    receipt: dict[str, Any]
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository_root,
    ) as base_runtime:
        context = prepare_pdf_canary(
            server_private_dir=base_runtime.tmp_dir,
            model_workspace=base_runtime.sterile_cwd,
        )
        skill_paths, skill_manifest = _copy_runtime_skills(
            base_runtime,
            repository_root=repository_root,
        )
        runtime, ambient_disabled_count = _isolate_runtime_skills(
            base_runtime,
            skill_paths=skill_paths,
            timeout_seconds=timeout_seconds,
        )
        workspace_before = _workspace_manifest(runtime.sterile_cwd)
        pdf_schema, pdf_schema_sha256 = _load_schema(_PDF_SCHEMA_PATH)
        structure_schema, structure_schema_sha256 = _load_schema(_STRUCTURE_SCHEMA_PATH)
        pdf_thread_id: str | None = None
        structure_thread_id: str | None = None
        deleted_threads: set[str] = set()
        try:
            (
                understanding_proposal,
                understanding_proposal_sha256,
                coverage_receipt,
                pdf_events,
                pdf_thread_id,
                platform,
                account_type,
            ) = _run_understanding_turn(
                runtime,
                context=context,
                skill_path=skill_paths[_PDF_SKILL],
                output_schema=pdf_schema,
                model=model,
                timeout_seconds=timeout_seconds,
            )
            deleted_threads.add(pdf_thread_id)
            (
                structure_proposal_sha256,
                structure_events,
                structure_thread_id,
            ) = _run_structure_turn(
                runtime,
                context=context,
                understanding_proposal=understanding_proposal,
                understanding_proposal_sha256=understanding_proposal_sha256,
                coverage_receipt=coverage_receipt,
                skill_path=skill_paths[_STRUCTURE_SKILL],
                output_schema=structure_schema,
                model=model,
                timeout_seconds=timeout_seconds,
            )
            deleted_threads.add(structure_thread_id)
            workspace_after = _workspace_manifest(runtime.sterile_cwd)
            if workspace_after != workspace_before:
                raise LifecycleProbeError("MODEL_WORKSPACE_MUTATED")
            post_restart_events = {
                pdf_thread_id: _verify_thread_absent_after_restart(
                    command=runtime.command,
                    environment=runtime.environment,
                    cwd=runtime.sterile_cwd,
                    timeout_seconds=timeout_seconds,
                    thread_id=pdf_thread_id,
                    codex_home=runtime.codex_home,
                ),
                structure_thread_id: _verify_thread_absent_after_restart(
                    command=runtime.command,
                    environment=runtime.environment,
                    cwd=runtime.sterile_cwd,
                    timeout_seconds=timeout_seconds,
                    thread_id=structure_thread_id,
                    codex_home=runtime.codex_home,
                ),
            }
            session_inventory = _assert_persistent_markers_absent(
                runtime.codex_home,
                extra_markers=(VISUAL_CANARY_CODE,),
            )
            receipt = {
                "schema": PDF_SKILL_CANARY_SCHEMA,
                "status": "PASS",
                "model": model,
                "account_type": account_type,
                "platform": platform,
                "protocol": dict(runtime.protocol),
                "fixture": {
                    "fixture_id": context.fixture.fixture_id,
                    "source_sha256": context.fixture.source_sha256,
                    "page_count": context.fixture.page_count,
                    "source_ir_status": context.source_ir.status.value,
                },
                "skills": {
                    "explicit_names": sorted(_EXPECTED_SKILLS),
                    "manifest": skill_manifest,
                    "ambient_disabled_count": ambient_disabled_count,
                    "enabled_count": len(_EXPECTED_SKILLS),
                },
                "schemas": {
                    "document_understanding_sha256": pdf_schema_sha256,
                    "structure_proposal_sha256": structure_schema_sha256,
                },
                "capture": {
                    "facts_sha256": context.facts_sha256,
                    "page_ledger_sha256": context.page_ledger_sha256,
                    "page_image_sha256": context.page_image_sha256,
                    "canonical_status": "PARTIAL",
                    "agent_inspected_pages": [2],
                    "canonical_unresolved_pages": [2],
                },
                "understanding": {
                    "proposal_sha256": understanding_proposal_sha256,
                    "coverage_receipt_sha256": canonical_sha256(coverage_receipt),
                    "coverage_status": coverage_receipt["status"],
                    "apply_eligible": coverage_receipt["apply_eligible"],
                    "events": pdf_events,
                },
                "structure": {
                    "proposal_sha256": structure_proposal_sha256,
                    "canonical_node_count": len(context.expected_canonical_nodes),
                    "visual_advisory_node_count": 2,
                    "apply_eligible": False,
                    "events": structure_events,
                },
                "thread_delete_events_observed": 2,
                "post_restart_threads_absent": True,
                "post_restart_events": post_restart_events,
                "codex_home_session_marker_absent": True,
                "codex_home_session_inventory": session_inventory,
                "workspace_manifest_unchanged": True,
                "run_temp_cleanup": False,
            }
        except BaseException as exc:
            for thread_id in (pdf_thread_id, structure_thread_id):
                if thread_id is None or thread_id in deleted_threads:
                    continue
                try:
                    _delete_thread_with_fresh_session(
                        runtime,
                        thread_id=thread_id,
                        timeout_seconds=min(timeout_seconds, 20.0),
                    )
                except LifecycleProbeError as cleanup_exc:
                    raise LifecycleProbeError("CLEANUP_FAILED") from cleanup_exc
            if isinstance(exc, LifecycleProbeError):
                raise
            raise LifecycleProbeError("PDF_SKILL_CANARY_FAILED") from exc
    receipt["run_temp_cleanup"] = True
    return receipt


def _run_understanding_turn(
    runtime: CanaryRuntimeState,
    *,
    context: PdfCanaryContext,
    skill_path: Path,
    output_schema: Mapping[str, Any],
    model: str,
    timeout_seconds: float,
) -> tuple[
    Mapping[str, Any],
    str,
    Mapping[str, Any],
    Mapping[str, Any],
    str,
    Mapping[str, str],
    str,
]:
    ledger = EventLedger()
    session = _open_runtime_session(
        runtime,
        ledger=ledger,
        timeout_seconds=timeout_seconds,
    )
    thread_id: str | None = None
    try:
        platform, account = _preflight_session(
            session,
            codex_home=runtime.codex_home,
            model=model,
        )
        _verify_skill_allowlist(
            session,
            runtime=runtime,
            expected_paths={_PDF_SKILL: skill_path, _STRUCTURE_SKILL: None},
        )
        thread_id = _start_or_resume_thread(
            session,
            ledger,
            model=model,
            sterile_cwd=runtime.sterile_cwd,
        )
        result = session.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": _understanding_turn_input(context, skill_path=skill_path),
                "cwd": str(runtime.sterile_cwd),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "model": model,
                "effort": "medium",
                "summary": "none",
                "outputSchema": dict(output_schema),
            },
        )
        turn_id = _validate_turn_start_result(result)
        ledger.bind_turn(turn_id)
        if session.wait_for_turn(turn_id) != "completed":
            raise LifecycleProbeError("PDF_UNDERSTANDING_TURN_NOT_COMPLETED")
        proposal = _parse_agent_json(ledger.final_agent_message)
        try:
            coverage_receipt, proposal_sha256 = validate_understanding_proposal(
                proposal,
                context,
            )
        except ValueError as exc:
            raise LifecycleProbeError(
                f"UNDERSTANDING_PROPOSAL_REJECTED_{_validation_reason(exc)}"
            ) from exc
        session.request("thread/delete", {"threadId": thread_id})
        session.wait_for_thread_deleted(thread_id)
        ledger.validate_normal_sequence(thread_id=thread_id, turn_id=turn_id)
        session.stop_gracefully()
        return (
            proposal,
            proposal_sha256,
            coverage_receipt,
            ledger.content_free_summary(),
            thread_id,
            platform,
            str(account["account_type"]),
        )
    except BaseException:
        session.close(force=True)
        if thread_id is not None:
            try:
                _delete_thread_with_fresh_session(
                    runtime,
                    thread_id=thread_id,
                    timeout_seconds=min(timeout_seconds, 20.0),
                )
            except LifecycleProbeError as cleanup_exc:
                raise LifecycleProbeError("CLEANUP_FAILED") from cleanup_exc
        raise
    finally:
        session.close()


def _run_structure_turn(
    runtime: CanaryRuntimeState,
    *,
    context: PdfCanaryContext,
    understanding_proposal: Mapping[str, Any],
    understanding_proposal_sha256: str,
    coverage_receipt: Mapping[str, Any],
    skill_path: Path,
    output_schema: Mapping[str, Any],
    model: str,
    timeout_seconds: float,
) -> tuple[str, Mapping[str, Any], str]:
    ledger = EventLedger()
    session = _open_runtime_session(
        runtime,
        ledger=ledger,
        timeout_seconds=timeout_seconds,
    )
    thread_id: str | None = None
    try:
        _preflight_session(session, codex_home=runtime.codex_home, model=model)
        _verify_skill_allowlist(
            session,
            runtime=runtime,
            expected_paths={_PDF_SKILL: None, _STRUCTURE_SKILL: skill_path},
        )
        thread_id = _start_or_resume_thread(
            session,
            ledger,
            model=model,
            sterile_cwd=runtime.sterile_cwd,
        )
        result = session.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": _structure_turn_input(
                    context,
                    understanding_proposal=understanding_proposal,
                    understanding_proposal_sha256=understanding_proposal_sha256,
                    coverage_receipt=coverage_receipt,
                    skill_path=skill_path,
                ),
                "cwd": str(runtime.sterile_cwd),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "model": model,
                "effort": "medium",
                "summary": "none",
                "outputSchema": dict(output_schema),
            },
        )
        turn_id = _validate_turn_start_result(result)
        ledger.bind_turn(turn_id)
        if session.wait_for_turn(turn_id) != "completed":
            raise LifecycleProbeError("STRUCTURE_TURN_NOT_COMPLETED")
        proposal = _parse_agent_json(ledger.final_agent_message)
        try:
            proposal_sha256 = validate_structure_proposal(
                proposal,
                context=context,
                understanding_proposal_sha256=understanding_proposal_sha256,
                coverage_receipt=coverage_receipt,
            )
        except ValueError as exc:
            raise LifecycleProbeError(
                f"STRUCTURE_PROPOSAL_REJECTED_{_validation_reason(exc)}"
            ) from exc
        session.request("thread/delete", {"threadId": thread_id})
        session.wait_for_thread_deleted(thread_id)
        ledger.validate_normal_sequence(thread_id=thread_id, turn_id=turn_id)
        session.stop_gracefully()
        return proposal_sha256, ledger.content_free_summary(), thread_id
    except BaseException:
        session.close(force=True)
        if thread_id is not None:
            try:
                _delete_thread_with_fresh_session(
                    runtime,
                    thread_id=thread_id,
                    timeout_seconds=min(timeout_seconds, 20.0),
                )
            except LifecycleProbeError as cleanup_exc:
                raise LifecycleProbeError("CLEANUP_FAILED") from cleanup_exc
        raise
    finally:
        session.close()


def _understanding_turn_input(
    context: PdfCanaryContext,
    *,
    skill_path: Path,
) -> list[dict[str, str]]:
    """Build the only model-visible payload for the visual-understanding turn."""
    task_payload = pdf_understanding_task_payload(context)
    task_text = f"${_PDF_SKILL}\nTASK_JSON:\n{canonical_json(task_payload)}"
    if VISUAL_CANARY_CODE in task_text:
        raise LifecycleProbeError("VISUAL_CANARY_LEAKED_TO_PROMPT")
    return [
        {"type": "text", "text": task_text},
        {"type": "skill", "name": _PDF_SKILL, "path": str(skill_path)},
        {"type": "localImage", "path": str(context.page_image_path)},
    ]


def _structure_turn_input(
    context: PdfCanaryContext,
    *,
    understanding_proposal: Mapping[str, Any],
    understanding_proposal_sha256: str,
    coverage_receipt: Mapping[str, Any],
    skill_path: Path,
) -> list[dict[str, str]]:
    """Build the structure-only turn input; visual pixels never cross this boundary."""
    task_payload = structure_task_payload(
        context=context,
        understanding_proposal=understanding_proposal,
        understanding_proposal_sha256=understanding_proposal_sha256,
        coverage_receipt=coverage_receipt,
    )
    return [
        {
            "type": "text",
            "text": f"${_STRUCTURE_SKILL}\nTASK_JSON:\n{canonical_json(task_payload)}",
        },
        {
            "type": "skill",
            "name": _STRUCTURE_SKILL,
            "path": str(skill_path),
        },
    ]


def _copy_runtime_skills(
    runtime: CanaryRuntimeState,
    *,
    repository_root: Path,
    skill_names: Sequence[str] = tuple(sorted(_EXPECTED_SKILLS)),
) -> tuple[dict[str, Path], dict[str, Mapping[str, str]]]:
    source_root = (
        repository_root / "prototype/runtime_assets/codex-workspace/.agents/skills"
    ).resolve(strict=True)
    target_root = runtime.sterile_cwd / ".agents/skills"
    target_root.mkdir(parents=True, mode=0o700)
    os.chmod(target_root.parent, 0o700)
    os.chmod(target_root, 0o700)
    paths: dict[str, Path] = {}
    manifest: dict[str, Mapping[str, str]] = {}
    normalized_skill_names = tuple(sorted(set(skill_names)))
    if not normalized_skill_names:
        raise LifecycleProbeError("PRODUCT_SKILL_ALLOWLIST_EMPTY")
    for skill_name in normalized_skill_names:
        source = source_root / skill_name
        target = target_root / skill_name
        shutil.copytree(source, target)
        skill_md = (target / "SKILL.md").resolve(strict=True)
        policy_path = (target / "agents/openai.yaml").resolve(strict=True)
        if policy_path.read_text(encoding="utf-8") != (
            "policy:\n  allow_implicit_invocation: false\n"
        ):
            raise LifecycleProbeError("SKILL_POLICY_NOT_EXPLICIT_ONLY")
        paths[skill_name] = skill_md
        manifest[skill_name] = {
            "skill_md_sha256": _sha256_file(skill_md),
            "policy_sha256": _sha256_file(policy_path),
        }
    return paths, manifest


def _isolate_runtime_skills(
    runtime: CanaryRuntimeState,
    *,
    skill_paths: Mapping[str, Path],
    timeout_seconds: float,
) -> tuple[CanaryRuntimeState, int]:
    expected_skill_names = frozenset(skill_paths)
    if not expected_skill_names:
        raise LifecycleProbeError("PRODUCT_SKILL_ALLOWLIST_EMPTY")
    first = _list_skills(runtime, timeout_seconds=timeout_seconds)
    expected_entry = _single_skill_entry(first, runtime.sterile_cwd)
    expected_paths = {str(path) for path in skill_paths.values()}
    observed_repo_paths = {
        str(skill.get("path"))
        for skill in expected_entry["skills"]
        if skill.get("name") in expected_skill_names
    }
    if observed_repo_paths != expected_paths:
        raise LifecycleProbeError("PRODUCT_SKILL_PATH_MISMATCH")
    ambient_paths: list[str] = []
    for skill in expected_entry["skills"]:
        if skill.get("name") in expected_skill_names:
            continue
        path = skill.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise LifecycleProbeError("AMBIENT_SKILL_PATH_INVALID")
        ambient_paths.append(path)
    records = ",".join(
        "{path=" + json.dumps(path) + ",enabled=false}"
        for path in sorted(set(ambient_paths))
    )
    isolated_command = (
        *runtime.command,
        "--config",
        f"skills.config=[{records}]",
    )
    isolated_runtime = replace(runtime, command=isolated_command)
    second = _list_skills(isolated_runtime, timeout_seconds=timeout_seconds)
    isolated_entry = _single_skill_entry(second, runtime.sterile_cwd)
    enabled = {
        str(skill.get("name")): str(skill.get("path"))
        for skill in isolated_entry["skills"]
        if skill.get("enabled") is True
    }
    if enabled != {name: str(path) for name, path in skill_paths.items()}:
        raise LifecycleProbeError("SKILL_ALLOWLIST_NOT_EXACT")
    return isolated_runtime, len(set(ambient_paths))


def _list_skills(
    runtime: CanaryRuntimeState,
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    session = _open_runtime_session(
        runtime,
        ledger=EventLedger(),
        timeout_seconds=timeout_seconds,
    )
    try:
        session.initialize()
        return session.request(
            "skills/list",
            {"cwds": [str(runtime.sterile_cwd)], "forceReload": True},
        )
    finally:
        session.close()


def _single_skill_entry(
    result: Mapping[str, Any],
    cwd: Path,
) -> Mapping[str, Any]:
    data = result.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise LifecycleProbeError("SKILLS_LIST_INVALID")
    entry = data[0]
    if (
        not isinstance(entry, Mapping)
        or entry.get("cwd") != str(cwd)
        or entry.get("errors") != []
        or not isinstance(entry.get("skills"), list)
    ):
        raise LifecycleProbeError("SKILLS_LIST_INVALID")
    return entry


def _verify_skill_allowlist(
    session: JsonlRpcSession,
    *,
    runtime: CanaryRuntimeState,
    expected_paths: Mapping[str, Path | None],
) -> None:
    expected_skill_names = frozenset(expected_paths)
    if not expected_skill_names:
        raise LifecycleProbeError("PRODUCT_SKILL_ALLOWLIST_EMPTY")
    entry = _single_skill_entry(
        session.request(
            "skills/list",
            {"cwds": [str(runtime.sterile_cwd)], "forceReload": True},
        ),
        runtime.sterile_cwd,
    )
    enabled = {
        str(skill.get("name")): str(skill.get("path"))
        for skill in entry["skills"]
        if skill.get("enabled") is True
    }
    if set(enabled) != expected_skill_names:
        raise LifecycleProbeError("SKILL_ALLOWLIST_NOT_EXACT")
    for name, path in expected_paths.items():
        if path is not None and enabled.get(name) != str(path):
            raise LifecycleProbeError("PRODUCT_SKILL_PATH_MISMATCH")


def _workspace_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LifecycleProbeError("MODEL_WORKSPACE_SYMLINK_FORBIDDEN")
        if path.is_file():
            manifest[str(path.relative_to(root))] = _sha256_file(path)
    return manifest


def _load_schema(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("PDF_SKILL_SCHEMA_INVALID") from exc
    if not isinstance(payload, Mapping):
        raise LifecycleProbeError("PDF_SKILL_SCHEMA_INVALID")
    return payload, hashlib.sha256(raw).hexdigest()


def _parse_agent_json(value: str | None) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise LifecycleProbeError("AGENT_JSON_MISSING")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LifecycleProbeError("AGENT_JSON_INVALID") from exc
    if not isinstance(payload, Mapping):
        raise LifecycleProbeError("AGENT_JSON_INVALID")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validation_reason(exc: ValueError) -> str:
    value = re.sub(r"[^A-Z0-9]+", "_", str(exc).upper()).strip("_")
    return value[:96] or "UNKNOWN"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the B1-C2 explicit PDF Skills canary."
    )
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path(os.environ.get("REQPILOT_CODEX_BIN", DEFAULT_CODEX_BIN)),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=(
            Path(value)
            if (value := os.environ.get("REQPILOT_CODEX_HOME", "").strip())
            else None
        ),
    )
    parser.add_argument("--model", default=DEFAULT_PREFERRED_MODEL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.codex_home is None:
        print(
            json.dumps(
                {
                    "schema": PDF_SKILL_CANARY_SCHEMA,
                    "status": "FAIL",
                    "reason_code": "DEDICATED_CODEX_HOME_REQUIRED",
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        result = run_pdf_skill_canary(
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            repository_root=Path(__file__).resolve().parents[3],
            model=str(args.model),
            timeout_seconds=float(args.timeout_seconds),
        )
    except LifecycleProbeError as exc:
        print(
            json.dumps(
                {
                    "schema": PDF_SKILL_CANARY_SCHEMA,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
