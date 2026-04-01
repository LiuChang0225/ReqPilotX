from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

REQUIRED_ENV_VARS = (
    "REQPILOT_AZURE_AUTH_MODE",
    "REQPILOT_AI_ASSESSMENT_MODE",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_CHAT_DEPLOYMENT",
)

db = None
app_main = None


def _resolve_storage_root(project_dir: Path, raw_value: str | Path | None) -> Path:
    text = str(raw_value or "").strip()
    if not text:
        return (project_dir / "storage").resolve()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = project_dir / candidate
    return candidate.resolve()


def _bootstrap_runtime(*, project_dir: Path, requested_storage_dir: Path | None, allow_live_storage: bool) -> Path:
    live_storage_dir = (project_dir / "storage").resolve()
    env_storage_value = str(os.getenv("REQPILOT_STORAGE_DIR") or "").strip()
    if requested_storage_dir is not None:
        storage_root = _resolve_storage_root(project_dir, requested_storage_dir)
    elif env_storage_value:
        storage_root = _resolve_storage_root(project_dir, env_storage_value)
    else:
        storage_root = (live_storage_dir / "smoke" / "azure_screening").resolve()

    if storage_root == live_storage_dir and not allow_live_storage:
        raise SystemExit(
            "Refusing to run azure_screening_smoke.py against live prototype/storage. "
            "Use --storage-dir to point at an isolated smoke storage root, or pass --allow-live-storage only if you explicitly want the legacy behavior."
        )

    os.environ["REQPILOT_STORAGE_DIR"] = str(storage_root)

    global db, app_main
    db = importlib.import_module("app.db")
    app_main = importlib.import_module("app.main")
    return storage_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Azure OpenAI screening-only smoke validation for ReqPilotX.")
    parser.add_argument(
        "--sor-file",
        type=Path,
        default=Path("examples/demo_sor.txt"),
        help="Path to the SOR sample file relative to prototype/.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/azure_smoke/latest"),
        help="Directory for smoke summary artifacts relative to prototype/.",
    )
    parser.add_argument(
        "--storage-dir",
        type=Path,
        help="Isolated storage root for the smoke run relative to prototype/. Defaults to storage/smoke/azure_screening.",
    )
    parser.add_argument(
        "--allow-live-storage",
        action="store_true",
        help="Allow running against prototype/storage. Use only if the live app is stopped and the risk is understood.",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Keep the smoke database instead of restoring the pre-run app.db snapshot.",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    sor_path = (project_dir / args.sor_file).resolve()
    out_dir = (project_dir / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    smoke_storage_root = _bootstrap_runtime(
        project_dir=project_dir,
        requested_storage_dir=args.storage_dir,
        allow_live_storage=args.allow_live_storage,
    )

    missing_env = [name for name in REQUIRED_ENV_VARS if not str(os.getenv(name) or "").strip()]
    if missing_env:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing_env)}")

    if not sor_path.exists():
        raise SystemExit(f"SOR sample file not found: {sor_path}")

    db_path = db.DB_PATH
    backup_path = db_path.with_suffix(".appdb.backup")
    restore_context = _prepare_database(db_path=db_path, backup_path=backup_path, keep_db=args.keep_db)
    with restore_context:
        db.init_db()
        summary = _run_smoke(project_dir=project_dir, sor_path=sor_path)

    summary_path = out_dir / "azure_screening_smoke_summary.json"
    summary_md_path = out_dir / "azure_screening_smoke_summary.md"
    summary["storage_root"] = str(smoke_storage_root)
    summary["db_path"] = str(db.DB_PATH)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md_path.write_text(_render_md(summary), encoding="utf-8")

    print(f"[azure-screening-smoke] overall_pass: {summary['overall_pass']}")
    print(f"[azure-screening-smoke] storage_root: {smoke_storage_root}")
    print(f"[azure-screening-smoke] db_path: {db.DB_PATH}")
    print(f"[azure-screening-smoke] summary json: {summary_path}")
    print(f"[azure-screening-smoke] summary md: {summary_md_path}")

    if not summary["overall_pass"]:
        raise SystemExit(1)


def _prepare_database(*, db_path: Path, backup_path: Path, keep_db: bool):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if keep_db:
        return nullcontext()
    if backup_path.exists():
        backup_path.unlink()
    if db_path.exists() and db_path.stat().st_size > 0:
        shutil.copy2(db_path, backup_path)
    if db_path.exists():
        db_path.unlink()

    class _RestoreDb:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            cleanup_failed = False
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{db_path}{suffix}")
                if not candidate.exists():
                    continue
                removed = False
                for _ in range(10):
                    try:
                        candidate.unlink()
                        removed = True
                        break
                    except PermissionError:
                        time.sleep(0.5)
                if not removed:
                    cleanup_failed = True
                    print(
                        f"[azure-screening-smoke] warning: could not remove {candidate}; leaving current smoke db in place.",
                        file=sys.stderr,
                    )
            if backup_path.exists() and not cleanup_failed:
                shutil.move(backup_path, db_path)
            elif backup_path.exists():
                print(
                    f"[azure-screening-smoke] warning: original database backup retained at {backup_path}.",
                    file=sys.stderr,
                )
            return False

    return _RestoreDb()


def _run_smoke(*, project_dir: Path, sor_path: Path) -> dict[str, Any]:
    with TestClient(app_main.app) as client:
        project_resp = client.post(
            "/api/projects",
            json={"name": "Azure Smoke Screening", "customer_name": "Internal"},
        )
        project_resp.raise_for_status()
        project = project_resp.json()
        project_id = project["id"]

        with sor_path.open("rb") as handle:
            upload_resp = client.post(
                f"/api/projects/{project_id}/sor-documents",
                files={"file": (sor_path.name, handle, "text/plain")},
            )
        upload_resp.raise_for_status()
        document = upload_resp.json()
        document_id = document["id"]

        analyze_resp = client.post(f"/api/projects/{project_id}/analyze", json={"document_id": document_id})
        analyze_resp.raise_for_status()

        lock_resp = client.post(f"/api/projects/{project_id}/sor-documents/{document_id}/lock-extraction")
        lock_resp.raise_for_status()

        ai_resp = client.post(f"/api/projects/{project_id}/sor-documents/{document_id}/run-ai-analysis")
        payload = ai_resp.json()

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT mode, retrieval_backend, model_name, validation_status, fallback_reason, created_at "
                "FROM assessment_runs ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        ]
        assessments = [
            dict(row)
            for row in conn.execute(
                "SELECT analysis_label, ai_decision, assessment_mode, model_name, prompt_version, updated_at "
                "FROM assessments ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
        ]

    checks = _evaluate_checks(ai_status=ai_resp.status_code, payload=payload, runs=runs)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_id": project_id,
        "document_id": document_id,
        "sor_file": str(sor_path),
        "http_status": ai_resp.status_code,
        "response": payload,
        "assessment_runs": runs,
        "assessments": assessments,
        "checks": checks,
        "overall_pass": all(item["passed"] for item in checks),
    }


def _evaluate_checks(*, ai_status: int, payload: dict[str, Any], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append({"field": "http_status", "expected": 200, "actual": ai_status, "passed": ai_status == 200})
    checks.append(
        {
            "field": "knowledge_docs_used",
            "expected": 0,
            "actual": int(payload.get("knowledge_docs_used") or 0),
            "passed": int(payload.get("knowledge_docs_used") or 0) == 0,
        }
    )
    checks.append(
        {
            "field": "ai_screened_requirements",
            "expected": ">=1",
            "actual": int(payload.get("ai_screened_requirements") or 0),
            "passed": int(payload.get("ai_screened_requirements") or 0) >= 1,
        }
    )

    llm_shadow_runs = [row for row in runs if str(row.get("mode") or "") == "screening_llm_shadow"]
    checks.append(
        {
            "field": "screening_llm_shadow_run_present",
            "expected": True,
            "actual": bool(llm_shadow_runs),
            "passed": bool(llm_shadow_runs),
        }
    )
    checks.append(
        {
            "field": "screening_llm_shadow_fallback_reason",
            "expected": "",
            "actual": str(llm_shadow_runs[0].get("fallback_reason") or "") if llm_shadow_runs else "missing",
            "passed": bool(llm_shadow_runs) and not str(llm_shadow_runs[0].get("fallback_reason") or "").strip(),
        }
    )
    return checks


def _render_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Azure Screening Smoke Summary",
        "",
        f"- generated_at: `{summary.get('generated_at', '')}`",
        f"- overall_pass: `{summary.get('overall_pass', False)}`",
        f"- storage_root: `{summary.get('storage_root', '')}`",
        f"- db_path: `{summary.get('db_path', '')}`",
        f"- project_id: `{summary.get('project_id', '')}`",
        f"- document_id: `{summary.get('document_id', '')}`",
        f"- http_status: `{summary.get('http_status', '')}`",
        "",
        "## Checks",
        "",
    ]
    for item in summary.get("checks") or []:
        lines.append(
            f"- {item.get('field')}: expected `{item.get('expected')}` / actual `{item.get('actual')}` / pass `{item.get('passed')}`"
        )
    lines.extend([
        "",
        "## Assessment Runs",
        "",
    ])
    for row in summary.get("assessment_runs") or []:
        lines.append(
            f"- {row.get('mode')}: model `{row.get('model_name')}` / retrieval `{row.get('retrieval_backend')}` / validation `{row.get('validation_status')}` / fallback `{row.get('fallback_reason')}`"
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    main()
