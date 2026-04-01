from __future__ import annotations

import argparse
import importlib
import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

REQUIRED_ENV_VARS = (
    "REQPILOT_AZURE_AUTH_MODE",
    "REQPILOT_AI_ASSESSMENT_MODE",
    "REQPILOT_RETRIEVAL_BACKEND",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_CHAT_DEPLOYMENT",
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "AZURE_AI_SEARCH_ENDPOINT",
    "AZURE_AI_SEARCH_INDEX_NAME",
)

TABLE_KNOWLEDGE_TEXT = """Cold-start validation reference table.
| Parameter | Condition | Expected |
| --- | --- | --- |
| Platform startup latency | cold condition | <= 150 ms |
| CAN bitrate | nominal mode | 500 kbps |
| CAN bitrate | high-speed mode | 1 Mbps |
| OTA rollback | validation failure | supported |
"""

db = None
app_main = None
load_ai_copilot_config = None
_azure_search_credential = None


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
        storage_root = (live_storage_dir / "smoke" / "azure_rag").resolve()

    if storage_root == live_storage_dir and not allow_live_storage:
        raise SystemExit(
            "Refusing to run azure_rag_smoke.py against live prototype/storage. "
            "Use --storage-dir to point at an isolated smoke storage root, or pass --allow-live-storage only if you explicitly want the legacy behavior."
        )

    os.environ["REQPILOT_STORAGE_DIR"] = str(storage_root)

    global db, app_main, load_ai_copilot_config, _azure_search_credential
    db = importlib.import_module("app.db")
    app_main = importlib.import_module("app.main")
    ai_copilot = importlib.import_module("app.services.ai_copilot")
    load_ai_copilot_config = ai_copilot.load_ai_copilot_config
    _azure_search_credential = ai_copilot._azure_search_credential
    return storage_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Azure Search + OpenAI RAG smoke or benchmark validation for ReqPilotX.")
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
        help="Path to a STANDARD knowledge document relative to prototype/. May be provided multiple times.",
    )
    parser.add_argument(
        "--reference-file",
        type=Path,
        action="append",
        dest="reference_files",
        help="Path to a REFERENCE knowledge document relative to prototype/. May be provided multiple times.",
    )
    parser.add_argument(
        "--skip-generated-table-reference",
        action="store_true",
        help="Skip uploading the built-in generated table reference document.",
    )
    parser.add_argument(
        "--project-name",
        default="Azure Smoke RAG",
        help="Project name used for the temporary validation project.",
    )
    parser.add_argument(
        "--max-requirements",
        type=int,
        default=0,
        help="If > 0, sample up to this many AI-eligible requirements after extraction for faster real-document benchmark runs.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/azure_smoke/rag_latest"),
        help="Directory for smoke summary artifacts relative to prototype/.",
    )
    parser.add_argument(
        "--storage-dir",
        type=Path,
        help="Isolated storage root for the smoke run relative to prototype/. Defaults to storage/smoke/azure_rag.",
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
    knowledge_inputs = _resolve_knowledge_inputs(
        project_dir=project_dir,
        paths=args.knowledge_files or [Path("examples/demo_knowledge.txt")],
        doc_type="STANDARD",
    )
    reference_inputs = _resolve_knowledge_inputs(
        project_dir=project_dir,
        paths=args.reference_files or [],
        doc_type="REFERENCE",
    )
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
        try:
            summary = _run_smoke(
                project_dir=project_dir,
                sor_path=sor_path,
                knowledge_inputs=knowledge_inputs,
                reference_inputs=reference_inputs,
                include_generated_table_reference=not args.skip_generated_table_reference,
                max_requirements=max(0, int(args.max_requirements or 0)),
                project_name=str(args.project_name or "Azure Smoke RAG").strip() or "Azure Smoke RAG",
            )
        except Exception as exc:
            summary = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "overall_pass": False,
                "error": str(exc),
                "checks": [],
                "assessment_runs": [],
                "knowledge_documents": [],
                "search_index_counts": {},
            }

    summary_path = out_dir / "azure_rag_smoke_summary.json"
    summary_md_path = out_dir / "azure_rag_smoke_summary.md"
    summary["storage_root"] = str(smoke_storage_root)
    summary["db_path"] = str(db.DB_PATH)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md_path.write_text(_render_md(summary), encoding="utf-8")

    print(f"[azure-rag-smoke] overall_pass: {summary['overall_pass']}")
    print(f"[azure-rag-smoke] storage_root: {smoke_storage_root}")
    print(f"[azure-rag-smoke] db_path: {db.DB_PATH}")
    print(f"[azure-rag-smoke] summary json: {summary_path}")
    print(f"[azure-rag-smoke] summary md: {summary_md_path}")

    if not summary["overall_pass"]:
        raise SystemExit(1)


def _resolve_knowledge_inputs(
    *,
    project_dir: Path,
    paths: list[Path],
    doc_type: str,
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for raw_path in paths:
        resolved_path = (project_dir / raw_path).resolve()
        if not resolved_path.exists():
            raise SystemExit(f"Knowledge sample file not found: {resolved_path}")
        inputs.append(
            {
                "path": resolved_path,
                "file_name": resolved_path.name,
                "doc_type": doc_type,
                "doc_version": "2026.03",
                "status": "ACTIVE",
            }
        )
    return inputs


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
                        f"[azure-rag-smoke] warning: could not remove {candidate}; leaving current smoke db in place.",
                        file=sys.stderr,
                    )
            if backup_path.exists() and not cleanup_failed:
                shutil.move(backup_path, db_path)
            elif backup_path.exists():
                print(
                    f"[azure-rag-smoke] warning: original database backup retained at {backup_path}.",
                    file=sys.stderr,
                )
            return False

    return _RestoreDb()


def _run_smoke(
    *,
    project_dir: Path,
    sor_path: Path,
    knowledge_inputs: list[dict[str, Any]],
    reference_inputs: list[dict[str, Any]],
    include_generated_table_reference: bool,
    max_requirements: int,
    project_name: str,
) -> dict[str, Any]:
    config = load_ai_copilot_config()
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
        for input_spec in [*knowledge_inputs, *reference_inputs]:
            uploaded_docs.append(
                _upload_knowledge_doc(
                    client=client,
                    project_id=project_id,
                    file_path=Path(input_spec["path"]),
                    file_name=str(input_spec["file_name"]),
                    media_type=_guess_media_type(str(input_spec["file_name"])),
                    doc_type=str(input_spec["doc_type"]),
                    doc_version=str(input_spec["doc_version"]),
                    status=str(input_spec["status"]),
                )
            )

        if include_generated_table_reference:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
                handle.write(TABLE_KNOWLEDGE_TEXT)
                table_path = Path(handle.name)
            try:
                uploaded_docs.append(
                    _upload_knowledge_doc(
                        client=client,
                        project_id=project_id,
                        file_path=table_path,
                        file_name="azure_smoke_table_reference.txt",
                        media_type=_guess_media_type("azure_smoke_table_reference.txt"),
                        doc_type="REFERENCE",
                        doc_version="2026.03",
                        status="ACTIVE",
                    )
                )
            finally:
                try:
                    table_path.unlink()
                except FileNotFoundError:
                    pass

        search_index_counts = _wait_for_indexed_chunks(config=config, uploaded_docs=uploaded_docs)

        print(f"[azure-rag-smoke] indexed knowledge documents: {len(uploaded_docs)}")
        analyze_resp = client.post(f"/api/projects/{project_id}/analyze", json={"document_id": document_id})
        analyze_resp.raise_for_status()
        analyze_payload = analyze_resp.json()

        requirement_sample = _apply_requirement_sample(
            project_id=project_id,
            document_id=document_id,
            max_requirements=max_requirements,
            extracted_requirement_count=int(analyze_payload.get("requirements_generated") or 0),
        )
        print(
            "[azure-rag-smoke] extracted requirements: "
            f"{requirement_sample['requirements_generated_total']} total / "
            f"{requirement_sample['eligible_requirements_total']} eligible"
        )
        if requirement_sample["sampling_applied"]:
            print(
                "[azure-rag-smoke] sampled eligible requirements: "
                f"retained {requirement_sample['eligible_requirements_sampled']} / "
                f"{requirement_sample['eligible_requirements_total']} "
                f"(max {requirement_sample['max_requirements']})"
            )

        lock_resp = client.post(f"/api/projects/{project_id}/sor-documents/{document_id}/lock-extraction")
        lock_resp.raise_for_status()

        print("[azure-rag-smoke] starting AI analysis")
        ai_resp = client.post(f"/api/projects/{project_id}/sor-documents/{document_id}/run-ai-analysis")
        payload = ai_resp.json()
        print("[azure-rag-smoke] AI analysis completed")

    assessment_rows = [
        {
            "req_code": str(row.get("req_code") or row.get("display_req_code") or ""),
            "analysis_label": str(row.get("analysis_label") or ""),
            "assessment_mode": str(row.get("assessment_mode") or ""),
            "ai_decision": str(row.get("ai_decision") or ""),
            "ai_reason": str(row.get("ai_reason") or ""),
            "analysis_basis": str(row.get("analysis_basis") or ""),
        }
        for row in (payload.get("requirements") or [])
        if app_main._is_ai_eligible_requirement(row)
    ]

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        doc_ids = [str(item.get("id") or "") for item in uploaded_docs]
        knowledge_docs = [
            dict(row)
            for row in conn.execute(
                "SELECT id, file_name, doc_type, doc_version, retrieval_sync_status, retrieval_sync_error, retrieval_synced_at "
                "FROM knowledge_documents ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        ]
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT mode, retrieval_backend, model_name, validation_status, fallback_reason, created_at "
                "FROM assessment_runs ORDER BY created_at DESC LIMIT 40"
            ).fetchall()
        ]
        evidence_count = conn.execute(
            f"SELECT COUNT(*) FROM evidences WHERE source_doc_id IN ({','.join('?' for _ in doc_ids)})",
            doc_ids,
        ).fetchone()[0]
        evidence_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT source_doc_id, source_doc_name, source_location, relevance "
                f"FROM evidences WHERE source_doc_id IN ({','.join('?' for _ in doc_ids)}) "
                "ORDER BY relevance DESC LIMIT 10",
                doc_ids,
            ).fetchall()
        ]

    checks = _evaluate_checks(
        ai_status=ai_resp.status_code,
        payload=payload,
        uploaded_docs=uploaded_docs,
        knowledge_docs=knowledge_docs,
        runs=runs,
        assessment_rows=assessment_rows,
        search_index_counts=search_index_counts,
        evidence_count=int(evidence_count or 0),
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_id": project_id,
        "document_id": document_id,
        "sor_file": str(sor_path),
        "knowledge_files": [str(item.get("path") or "") for item in knowledge_inputs],
        "reference_files": [str(item.get("path") or "") for item in reference_inputs],
        "generated_table_reference_included": include_generated_table_reference,
        "requirement_sampling": requirement_sample,
        "http_status": ai_resp.status_code,
        "response": payload,
        "uploaded_docs": uploaded_docs,
        "knowledge_documents": knowledge_docs,
        "search_index_counts": search_index_counts,
        "assessment_runs": runs,
        "assessment_rows": assessment_rows,
        "evidence_rows": evidence_rows,
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
    doc_type: str,
    doc_version: str,
    status: str,
) -> dict[str, Any]:
    with file_path.open("rb") as handle:
        response = client.post(
            f"/api/projects/{project_id}/knowledge-documents",
            files={"file": (file_name, handle, media_type)},
            data={
                "doc_type": doc_type,
                "doc_version": doc_version,
                "status": status,
            },
        )
    response.raise_for_status()
    return response.json()


def _guess_media_type(file_name: str) -> str:
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or "application/octet-stream"


def _apply_requirement_sample(
    *,
    project_id: str,
    document_id: str,
    max_requirements: int,
    extracted_requirement_count: int,
) -> dict[str, Any]:
    requirements = db.list_requirements(project_id, document_id)
    eligible_rows = [row for row in requirements if app_main._is_ai_eligible_requirement(row)]
    summary = {
        "sampling_applied": False,
        "max_requirements": int(max_requirements or 0),
        "requirements_generated_total": int(extracted_requirement_count or len(requirements)),
        "requirements_retained_total": len(requirements),
        "eligible_requirements_total": len(eligible_rows),
        "eligible_requirements_sampled": len(eligible_rows),
        "sampled_requirement_codes": [str(row.get("req_code") or "") for row in eligible_rows[:10]],
    }
    if max_requirements <= 0 or len(eligible_rows) <= max_requirements:
        return summary

    sampled_rows = _spread_sample_rows(eligible_rows, max_requirements)
    keep_ids = {str(row.get("id") or "") for row in sampled_rows}
    drop_ids = [str(row.get("id") or "") for row in eligible_rows if str(row.get("id") or "") not in keep_ids]
    if not drop_ids:
        return summary

    placeholders = ",".join("?" for _ in drop_ids)
    with db.get_conn() as conn:
        conn.execute(f"DELETE FROM requirements WHERE id IN ({placeholders})", drop_ids)

    summary["sampling_applied"] = True
    summary["requirements_retained_total"] = len(requirements) - len(drop_ids)
    summary["eligible_requirements_sampled"] = len(sampled_rows)
    summary["sampled_requirement_codes"] = [str(row.get("req_code") or "") for row in sampled_rows]
    return summary


def _spread_sample_rows(rows: list[dict[str, Any]], max_requirements: int) -> list[dict[str, Any]]:
    if max_requirements <= 0 or len(rows) <= max_requirements:
        return rows
    if max_requirements == 1:
        return [rows[0]]

    selected_indices: list[int] = []
    seen: set[int] = set()
    last_index = len(rows) - 1
    for offset in range(max_requirements):
        index = int((offset * last_index) / (max_requirements - 1))
        while index in seen and index < last_index:
            index += 1
        if index in seen:
            index = next(candidate for candidate in range(len(rows)) if candidate not in seen)
        seen.add(index)
        selected_indices.append(index)
    return [rows[index] for index in selected_indices]


def _wait_for_indexed_chunks(*, config: Any, uploaded_docs: list[dict[str, Any]]) -> dict[str, int]:
    from azure.search.documents import SearchClient

    search_client = SearchClient(
        endpoint=config.azure_search_endpoint,
        index_name=config.azure_search_index_name,
        credential=_azure_search_credential(config),
    )
    doc_ids = [str(item.get("id") or "") for item in uploaded_docs]
    counts = {doc_id: 0 for doc_id in doc_ids}
    deadline = time.monotonic() + 60.0

    while time.monotonic() < deadline:
        for doc_id in doc_ids:
            results = search_client.search(
                search_text="*",
                filter=f"source_doc_id eq '{doc_id}'",
                top=3,
                select=["chunk_id"],
            )
            counts[doc_id] = len(list(results))
        if all(counts.values()):
            return counts
        time.sleep(2.0)
    return counts


def _evaluate_checks(
    *,
    ai_status: int,
    payload: dict[str, Any],
    uploaded_docs: list[dict[str, Any]],
    knowledge_docs: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    assessment_rows: list[dict[str, Any]],
    search_index_counts: dict[str, int],
    evidence_count: int,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append({"field": "http_status", "expected": 200, "actual": ai_status, "passed": ai_status == 200})
    checks.append(
        {
            "field": "knowledge_docs_used",
            "expected": len(uploaded_docs),
            "actual": int(payload.get("knowledge_docs_used") or 0),
            "passed": int(payload.get("knowledge_docs_used") or 0) == len(uploaded_docs),
        }
    )
    checks.append(
        {
            "field": "ai_processed_requirements",
            "expected": ">=1",
            "actual": int(payload.get("ai_processed_requirements") or 0),
            "passed": int(payload.get("ai_processed_requirements") or 0) >= 1,
        }
    )

    uploaded_doc_ids = {str(item.get("id") or "") for item in uploaded_docs}
    ready_docs = [row for row in knowledge_docs if str(row.get("id") or "") in uploaded_doc_ids]
    checks.append(
        {
            "field": "knowledge_sync_ready_count",
            "expected": len(uploaded_docs),
            "actual": sum(1 for row in ready_docs if str(row.get("retrieval_sync_status") or "") == "READY"),
            "passed": sum(1 for row in ready_docs if str(row.get("retrieval_sync_status") or "") == "READY")
            == len(uploaded_docs),
        }
    )
    checks.append(
        {
            "field": "knowledge_sync_error_empty",
            "expected": True,
            "actual": all(not str(row.get("retrieval_sync_error") or "").strip() for row in ready_docs),
            "passed": all(not str(row.get("retrieval_sync_error") or "").strip() for row in ready_docs),
        }
    )
    checks.append(
        {
            "field": "azure_search_chunks_indexed",
            "expected": True,
            "actual": all(int(search_index_counts.get(doc_id) or 0) > 0 for doc_id in uploaded_doc_ids),
            "passed": all(int(search_index_counts.get(doc_id) or 0) > 0 for doc_id in uploaded_doc_ids),
        }
    )

    llm_shadow_runs = [
        row
        for row in runs
        if str(row.get("mode") or "") == "llm_shadow" and str(row.get("retrieval_backend") or "") == "azure_sdk"
    ]
    checks.append(
        {
            "field": "llm_shadow_azure_sdk_run_present",
            "expected": True,
            "actual": bool(llm_shadow_runs),
            "passed": bool(llm_shadow_runs),
        }
    )
    checks.append(
        {
            "field": "llm_shadow_azure_sdk_fallback_reason",
            "expected": "",
            "actual": str(llm_shadow_runs[0].get("fallback_reason") or "") if llm_shadow_runs else "missing",
            "passed": bool(llm_shadow_runs) and not str(llm_shadow_runs[0].get("fallback_reason") or "").strip(),
        }
    )
    checks.append(
        {
            "field": "evidence_rows_from_uploaded_docs",
            "expected": ">=1",
            "actual": evidence_count,
            "passed": evidence_count >= 1,
        }
    )

    llm_shadow_final_rows = [
        row for row in assessment_rows if str(row.get("assessment_mode") or "").strip().lower() == "llm_shadow"
    ]
    checks.append(
        {
            "field": "llm_shadow_final_result_present",
            "expected": True,
            "actual": bool(llm_shadow_final_rows),
            "passed": bool(llm_shadow_final_rows),
        }
    )

    llm_shadow_requirement_rows = [
        row
        for row in llm_shadow_final_rows
        if str(row.get("analysis_label") or "").strip().upper() == "REQUIREMENT"
    ]
    checks.append(
        {
            "field": "llm_shadow_final_reason_present",
            "expected": True,
            "actual": all(str(row.get("ai_reason") or "").strip() for row in llm_shadow_requirement_rows)
            if llm_shadow_requirement_rows
            else False,
            "passed": bool(llm_shadow_requirement_rows)
            and all(str(row.get("ai_reason") or "").strip() for row in llm_shadow_requirement_rows),
        }
    )
    checks.append(
        {
            "field": "llm_shadow_analysis_basis_has_stage_summary",
            "expected": True,
            "actual": all(
                "检索后端" in str(row.get("analysis_basis") or "") and "已完成" in str(row.get("analysis_basis") or "")
                for row in llm_shadow_requirement_rows
            )
            if llm_shadow_requirement_rows
            else False,
            "passed": bool(llm_shadow_requirement_rows)
            and all(
                "检索后端" in str(row.get("analysis_basis") or "") and "已完成" in str(row.get("analysis_basis") or "")
                for row in llm_shadow_requirement_rows
            ),
        }
    )
    return checks


def _render_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Azure RAG Smoke Summary",
        "",
        f"- generated_at: `{summary.get('generated_at', '')}`",
        f"- overall_pass: `{summary.get('overall_pass', False)}`",
        f"- storage_root: `{summary.get('storage_root', '')}`",
        f"- db_path: `{summary.get('db_path', '')}`",
        f"- project_id: `{summary.get('project_id', '')}`",
        f"- document_id: `{summary.get('document_id', '')}`",
        f"- http_status: `{summary.get('http_status', '')}`",
    ]
    sampling = summary.get("requirement_sampling") or {}
    if sampling:
        lines.extend(
            [
                f"- requirements_generated_total: `{sampling.get('requirements_generated_total', '')}`",
                f"- requirements_retained_total: `{sampling.get('requirements_retained_total', '')}`",
                f"- eligible_requirements_total: `{sampling.get('eligible_requirements_total', '')}`",
                f"- eligible_requirements_sampled: `{sampling.get('eligible_requirements_sampled', '')}`",
                f"- sampling_applied: `{sampling.get('sampling_applied', False)}`",
            ]
        )
    if summary.get("error"):
        lines.extend(["", f"- error: `{summary.get('error')}`"])
    lines.extend(["", "## Checks", ""])
    for item in summary.get("checks") or []:
        lines.append(
            f"- {item.get('field')}: expected `{item.get('expected')}` / actual `{item.get('actual')}` / pass `{item.get('passed')}`"
        )
    lines.extend(["", "## Search Index Counts", ""])
    for doc_id, count in (summary.get("search_index_counts") or {}).items():
        lines.append(f"- {doc_id}: `{count}` chunks visible")
    lines.extend(["", "## Assessment Runs", ""])
    for row in summary.get("assessment_runs") or []:
        lines.append(
            f"- {row.get('mode')}: model `{row.get('model_name')}` / retrieval `{row.get('retrieval_backend')}` / validation `{row.get('validation_status')}` / fallback `{row.get('fallback_reason')}`"
        )
    lines.extend(["", "## Final Assessment Rows", ""])
    for row in summary.get("assessment_rows") or []:
        lines.append(
            f"- {row.get('req_code')}: mode `{row.get('assessment_mode')}` / label `{row.get('analysis_label')}` / decision `{row.get('ai_decision')}` / reason `{row.get('ai_reason')}`"
        )
    lines.extend(["", "## Evidence Rows", ""])
    for row in summary.get("evidence_rows") or []:
        lines.append(
            f"- {row.get('source_doc_name')} / {row.get('source_location')}: relevance `{row.get('relevance')}`"
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    main()