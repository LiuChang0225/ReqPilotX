from __future__ import annotations

import argparse
import importlib
import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from fastapi.testclient import TestClient

REQUIRED_ENV_VARS = (
    "REQPILOT_ALIYUN_API_KEY",
    "REQPILOT_DASHSCOPE_BASE_URL",
    "REQPILOT_ALIYUN_CHAT_MODEL",
    "REQPILOT_ALIYUN_EMBEDDING_MODEL",
    "REQPILOT_ALIYUN_RETRIEVAL_BACKEND",
    "REQPILOT_ALIYUN_QDRANT_URL",
)
DEFAULT_PROFILE_ID = "aliyun"

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
        storage_root = (live_storage_dir / "smoke" / "aliyun_local_rag").resolve()

    if storage_root == live_storage_dir and not allow_live_storage:
        raise SystemExit(
            "Refusing to run aliyun_local_rag_smoke.py against live prototype/storage. "
            "Use --storage-dir to point at an isolated smoke storage root, or pass --allow-live-storage only if you explicitly want the legacy behavior."
        )

    os.environ["REQPILOT_STORAGE_DIR"] = str(storage_root)

    global db, app_main
    db = importlib.import_module("app.db")
    app_main = importlib.import_module("app.main")
    return storage_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DashScope + local Qdrant Aliyun POC smoke validation for ReqPilotX.")
    parser.add_argument(
        "--sor-file",
        type=Path,
        default=Path("examples/demo_sor.txt"),
        help="Path to the SOR sample file relative to prototype/.",
    )
    parser.add_argument(
        "--knowledge-file",
        type=Path,
        action="append",
        dest="knowledge_files",
        help="Path to a knowledge document relative to prototype/. May be provided multiple times.",
    )
    parser.add_argument(
        "--project-name",
        default="Aliyun Local RAG Smoke",
        help="Project name used for the temporary validation project.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/aliyun_local_poc/latest"),
        help="Directory for smoke summary artifacts relative to prototype/.",
    )
    parser.add_argument(
        "--storage-dir",
        type=Path,
        help="Isolated storage root for the smoke run relative to prototype/. Defaults to storage/smoke/aliyun_local_rag.",
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
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="Maximum time to wait for the background AI job to complete.",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    sor_path = (project_dir / args.sor_file).resolve()
    knowledge_files = [
        (project_dir / raw_path).resolve()
        for raw_path in (args.knowledge_files or [Path("examples/demo_knowledge.txt")])
    ]
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
    if str(os.getenv("REQPILOT_ALIYUN_RETRIEVAL_BACKEND") or "").strip().lower() != "qdrant":
        raise SystemExit("REQPILOT_ALIYUN_RETRIEVAL_BACKEND must be set to 'qdrant' for the Aliyun local POC smoke.")
    if not sor_path.exists():
        raise SystemExit(f"SOR sample file not found: {sor_path}")
    for knowledge_path in knowledge_files:
        if not knowledge_path.exists():
            raise SystemExit(f"Knowledge sample file not found: {knowledge_path}")

    _preflight_qdrant(
        url=str(os.getenv("REQPILOT_ALIYUN_QDRANT_URL") or os.getenv("REQPILOT_QDRANT_URL") or os.getenv("QDRANT_URL") or "").strip(),
        api_key=str(
            os.getenv("REQPILOT_ALIYUN_QDRANT_API_KEY")
            or os.getenv("REQPILOT_QDRANT_API_KEY")
            or os.getenv("QDRANT_API_KEY")
            or ""
        ).strip(),
    )

    db_path = db.DB_PATH
    backup_path = db_path.with_suffix(".appdb.backup")
    restore_context = _prepare_database(db_path=db_path, backup_path=backup_path, keep_db=args.keep_db)
    with restore_context:
        db.init_db()
        summary = _run_smoke(
            sor_path=sor_path,
            knowledge_files=knowledge_files,
            project_name=str(args.project_name or "Aliyun Local RAG Smoke").strip() or "Aliyun Local RAG Smoke",
            timeout_seconds=max(float(args.timeout_seconds or 120.0), 15.0),
        )

    summary_path = out_dir / "aliyun_local_rag_smoke_summary.json"
    summary_md_path = out_dir / "aliyun_local_rag_smoke_summary.md"
    summary["storage_root"] = str(smoke_storage_root)
    summary["db_path"] = str(db.DB_PATH)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md_path.write_text(_render_md(summary), encoding="utf-8")

    print(f"[aliyun-local-rag-smoke] overall_pass: {summary['overall_pass']}")
    print(f"[aliyun-local-rag-smoke] storage_root: {smoke_storage_root}")
    print(f"[aliyun-local-rag-smoke] db_path: {db.DB_PATH}")
    print(f"[aliyun-local-rag-smoke] summary json: {summary_path}")
    print(f"[aliyun-local-rag-smoke] summary md: {summary_md_path}")

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
                        f"[aliyun-local-rag-smoke] warning: could not remove {candidate}; leaving current smoke db in place.",
                        file=sys.stderr,
                    )
            if backup_path.exists() and not cleanup_failed:
                shutil.move(backup_path, db_path)
            elif backup_path.exists():
                print(
                    f"[aliyun-local-rag-smoke] warning: original database backup retained at {backup_path}.",
                    file=sys.stderr,
                )
            return False

    return _RestoreDb()


def _preflight_qdrant(*, url: str, api_key: str) -> None:
    normalized = str(url or "").strip().rstrip("/")
    if not normalized:
        raise SystemExit("Qdrant URL is missing. Set REQPILOT_ALIYUN_QDRANT_URL or REQPILOT_QDRANT_URL.")
    request = urllib_request.Request(f"{normalized}/collections")
    if api_key:
        request.add_header("api-key", api_key)
    try:
        with urllib_request.urlopen(request, timeout=2.0) as response:
            status_code = int(getattr(response, "status", 200) or 200)
        if not 200 <= status_code < 300:
            raise SystemExit(f"Qdrant preflight failed with HTTP {status_code} at {normalized}.")
    except urllib_error.HTTPError as exc:
        raise SystemExit(f"Qdrant preflight failed with HTTP {exc.code} at {normalized}.") from exc
    except Exception as exc:
        raise SystemExit(f"Qdrant preflight failed at {normalized}: {exc}") from exc


def _run_smoke(
    *,
    sor_path: Path,
    knowledge_files: list[Path],
    project_name: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    with TestClient(app_main.app) as client:
        project_resp = client.post(
            "/api/projects",
            json={"name": project_name, "customer_name": "Internal"},
        )
        project_resp.raise_for_status()
        project = project_resp.json()
        project_id = project["id"]

        with sor_path.open("rb") as handle:
            upload_resp = client.post(
                f"/api/projects/{project_id}/sor-documents",
                files={"file": (sor_path.name, handle, _guess_media_type(sor_path.name))},
            )
        upload_resp.raise_for_status()
        document = upload_resp.json()
        document_id = document["id"]

        uploaded_docs: list[dict[str, Any]] = []
        for knowledge_path in knowledge_files:
            uploaded_docs.append(
                _upload_knowledge_doc(
                    client=client,
                    project_id=project_id,
                    file_path=knowledge_path,
                    file_name=knowledge_path.name,
                    media_type=_guess_media_type(knowledge_path.name),
                )
            )

        analyze_resp = client.post(f"/api/projects/{project_id}/analyze", json={"document_id": document_id})
        analyze_resp.raise_for_status()

        lock_resp = client.post(f"/api/projects/{project_id}/sor-documents/{document_id}/lock-extraction")
        lock_resp.raise_for_status()

        start_resp = client.post(
            f"/api/projects/{project_id}/sor-documents/{document_id}/run-ai-analysis/start",
            json={"profile_id": DEFAULT_PROFILE_ID},
        )
        start_resp.raise_for_status()
        status_payload = _poll_ai_job(
            client=client,
            project_id=project_id,
            document_id=document_id,
            timeout_seconds=timeout_seconds,
        )
        detail_payload = _load_first_requirement_detail(
            client=client,
            project_id=project_id,
            document_id=document_id,
            payload=status_payload,
        )

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT profile_id, provider, mode, retrieval_backend, model_name, validation_status, fallback_reason, created_at "
                "FROM assessment_runs ORDER BY created_at DESC LIMIT 40"
            ).fetchall()
        ]
        sync_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT knowledge_document_id, profile_id, provider, retrieval_backend, status, error_message, synced_at, chunk_count "
                "FROM knowledge_document_syncs ORDER BY updated_at DESC LIMIT 40"
            ).fetchall()
        ]

    checks = _evaluate_checks(
        status_payload=status_payload,
        detail_payload=detail_payload,
        uploaded_docs=uploaded_docs,
        runs=runs,
        sync_rows=sync_rows,
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_id": project_id,
        "document_id": document_id,
        "profile_id": DEFAULT_PROFILE_ID,
        "sor_file": str(sor_path),
        "knowledge_files": [str(path) for path in knowledge_files],
        "uploaded_docs": uploaded_docs,
        "status_payload": status_payload,
        "detail_payload": detail_payload,
        "assessment_runs": runs,
        "knowledge_sync_rows": sync_rows,
        "checks": checks,
        "overall_pass": all(item["passed"] for item in checks),
    }


def _upload_knowledge_doc(
    *,
    client: TestClient,
    project_id: str,
    file_path: Path,
    file_name: str,
    media_type: str,
) -> dict[str, Any]:
    with file_path.open("rb") as handle:
        response = client.post(
            f"/api/projects/{project_id}/knowledge-documents",
            files={"file": (file_name, handle, media_type)},
            data={
                "doc_type": "REFERENCE",
                "doc_version": "2026.04",
                "status": "ACTIVE",
            },
        )
    response.raise_for_status()
    return response.json()


def _guess_media_type(file_name: str) -> str:
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or "application/octet-stream"


def _poll_ai_job(
    *,
    client: TestClient,
    project_id: str,
    document_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started_at = time.time()
    while True:
        response = client.get(f"/api/projects/{project_id}/sor-documents/{document_id}/ai-analysis-status")
        response.raise_for_status()
        payload = response.json()
        job = payload.get("job") or {}
        status = str(job.get("status") or "").strip().upper()
        if status in {"COMPLETED", "FAILED"}:
            return payload
        if time.time() - started_at > timeout_seconds:
            raise RuntimeError(f"Aliyun local POC smoke timed out after {timeout_seconds:.1f}s.")
        time.sleep(0.5)


def _load_first_requirement_detail(
    *,
    client: TestClient,
    project_id: str,
    document_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    workspace_resp = client.get(f"/api/projects/{project_id}/sor-documents/{document_id}/workspace")
    workspace_resp.raise_for_status()
    workspace = workspace_resp.json()
    requirements = [
        row
        for row in (workspace.get("requirements") or [])
        if app_main._is_ai_eligible_requirement(row)
    ]
    if not requirements:
        return {}
    requirement_id = str(requirements[0].get("id") or "")
    if not requirement_id:
        return {}
    detail_resp = client.get(f"/api/requirements/{requirement_id}/analysis-detail")
    detail_resp.raise_for_status()
    return detail_resp.json()


def _evaluate_checks(
    *,
    status_payload: dict[str, Any],
    detail_payload: dict[str, Any],
    uploaded_docs: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    sync_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    job = status_payload.get("job") or {}
    aliyun_runs = [row for row in runs if str(row.get("profile_id") or "").strip() == DEFAULT_PROFILE_ID]
    aliyun_qdrant_runs = [
        row
        for row in aliyun_runs
        if str(row.get("retrieval_backend") or "").strip() == "qdrant"
    ]
    aliyun_syncs = [
        row
        for row in sync_rows
        if str(row.get("profile_id") or "").strip() == DEFAULT_PROFILE_ID
    ]
    return [
        {
            "field": "job_completed",
            "actual": str(job.get("status") or ""),
            "passed": str(job.get("status") or "").strip().upper() == "COMPLETED",
        },
        {
            "field": "job_profile_is_aliyun",
            "actual": str(job.get("profile_id") or ""),
            "passed": str(job.get("profile_id") or "").strip() == DEFAULT_PROFILE_ID,
        },
        {
            "field": "uploaded_docs_present",
            "actual": len(uploaded_docs),
            "passed": len(uploaded_docs) > 0,
        },
        {
            "field": "aliyun_qdrant_runs_present",
            "actual": bool(aliyun_qdrant_runs),
            "passed": bool(aliyun_qdrant_runs),
        },
        {
            "field": "aliyun_knowledge_sync_ready",
            "actual": [str(row.get("status") or "") for row in aliyun_syncs],
            "passed": bool(aliyun_syncs)
            and all(str(row.get("status") or "").strip().upper() == "READY" for row in aliyun_syncs),
        },
        {
            "field": "analysis_detail_profile_is_aliyun",
            "actual": str(((detail_payload.get("assessment") or {}).get("profile_id") or "")),
            "passed": not detail_payload
            or str(((detail_payload.get("assessment") or {}).get("profile_id") or "")).strip() == DEFAULT_PROFILE_ID,
        },
    ]


def _render_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Aliyun Local RAG Smoke",
        "",
        f"- Generated at: {summary.get('generated_at')}",
        f"- Overall pass: {summary.get('overall_pass')}",
        f"- Profile: {summary.get('profile_id')}",
        f"- Storage root: {summary.get('storage_root')}",
        f"- DB path: {summary.get('db_path')}",
        "",
        "## Checks",
    ]
    for item in summary.get("checks") or []:
        status = "PASS" if item.get("passed") else "FAIL"
        lines.append(f"- [{status}] {item.get('field')}: {item.get('actual')}")
    lines.extend(
        [
            "",
            "## Latest Job",
            f"```json\n{json.dumps(summary.get('status_payload') or {}, ensure_ascii=False, indent=2)}\n```",
            "",
            "## Assessment Runs",
            f"```json\n{json.dumps(summary.get('assessment_runs') or [], ensure_ascii=False, indent=2)}\n```",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
