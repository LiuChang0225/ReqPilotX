from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable

from app.runtime_paths import resolve_storage_dir
from app.services.complex_table_mode import hydrate_complex_row_metadata
from app.services.table_assets import _refresh_enriched_entry_text_fields, normalize_preserved_entry_table_assets


APP_DIR = Path(__file__).resolve().parent
PROTOTYPE_DIR = APP_DIR.parent
STORAGE_DIR = resolve_storage_dir(PROTOTYPE_DIR)
DB_PATH = STORAGE_DIR / "app.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _to_dict_list(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _decode_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if not isinstance(payload, list):
        return [text]
    return [str(item).strip() for item in payload if str(item).strip()]


def _decode_json_payload(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return default
    if isinstance(default, list) and isinstance(payload, list):
        return payload
    if isinstance(default, dict) and isinstance(payload, dict):
        return payload
    return default


def _normalize_requirement_record(item: dict[str, Any]) -> dict[str, Any]:
    item["entry_kind"] = str(item.get("entry_kind") or "REQUIREMENT").strip().upper() or "REQUIREMENT"
    item["text_relation"] = str(item.get("text_relation") or "distinct").strip() or "distinct"
    item["is_deduped"] = bool(int(item.get("is_deduped") or 0))
    item["parent_section_code"] = str(item.get("parent_section_code") or "")
    item["parent_section_title"] = str(item.get("parent_section_title") or "")
    item["parent_section_label"] = str(item.get("parent_section_label") or "")
    item["heading_text"] = str(item.get("heading_text") or "")
    item["raw_text"] = str(item.get("raw_text") or item.get("requirement_text") or "")
    item["display_text"] = str(item.get("display_text") or item.get("requirement_text") or "")
    item["merged_from_req_codes"] = _decode_json_list(item.get("merged_from_req_codes"))
    item["merged_from_entry_ids"] = _decode_json_list(item.get("merged_from_entry_ids"))
    item["dedupe_version"] = str(item.get("dedupe_version") or "")
    item["table_refs"] = _decode_json_payload(item.get("table_refs"), default=[])
    
    # Backfill truly legacy rows that predate export-variant recommendation metadata.
    for ref in item["table_refs"]:
        if isinstance(ref, dict):
            export_variant = str(ref.get("export_variant") or "").strip().lower()
            has_source = bool(str(ref.get("source_snapshot_path") or "").strip())
            recommended_variant = str(ref.get("recommended_export_variant") or "").strip().lower()
            override_marker = ref.get("export_variant_overridden")
            if override_marker is None and not recommended_variant and export_variant == "slice" and has_source:
                ref["export_variant"] = "source"
            if not recommended_variant and has_source:
                ref["recommended_export_variant"] = "source"
                
    item["content_blocks"] = _decode_json_payload(item.get("content_blocks"), default=[])
    item["has_table_asset"] = bool(int(item.get("has_table_asset") or 0))
    item["table_render_mode"] = str(item.get("table_render_mode") or "")
    item["assessment_mode"] = str(item.get("assessment_mode") or "")
    item["profile_id"] = str(item.get("profile_id") or "")
    item["provider"] = str(item.get("provider") or "")
    item["retrieval_backend"] = str(item.get("retrieval_backend") or "")
    item["analysis_label"] = str(item.get("analysis_label") or "")
    item["analysis_basis"] = str(item.get("analysis_basis") or "")
    item["normalized_requirement"] = str(item.get("normalized_requirement") or "")
    item["risk_level"] = str(item.get("risk_level") or "")
    item["clarification_question"] = str(item.get("clarification_question") or "")
    item["model_name"] = str(item.get("model_name") or "")
    item["prompt_version"] = str(item.get("prompt_version") or "")
    if item["table_refs"] or item["content_blocks"]:
        item = normalize_preserved_entry_table_assets(item)
        item = _refresh_enriched_entry_text_fields(item)
    return hydrate_complex_row_metadata(item)


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.DatabaseError:
            pass
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sor_documents (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_path TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sor_documents_project_id ON sor_documents(project_id);

            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_path TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                doc_type TEXT NOT NULL DEFAULT 'REFERENCE',
                doc_version TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                effective_from TEXT NOT NULL DEFAULT '',
                effective_to TEXT NOT NULL DEFAULT '',
                retrieval_sync_status TEXT NOT NULL DEFAULT 'PENDING',
                retrieval_sync_error TEXT NOT NULL DEFAULT '',
                retrieval_synced_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_documents_project_id ON knowledge_documents(project_id);

            CREATE TABLE IF NOT EXISTS requirements (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                req_code TEXT NOT NULL,
                source_page INTEGER,
                source_anchor TEXT,
                requirement_text TEXT NOT NULL,
                requirement_type TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(sor_document_id, req_code),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_requirements_project_id ON requirements(project_id);
            CREATE INDEX IF NOT EXISTS idx_requirements_document_id ON requirements(sor_document_id);

            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY,
                requirement_id TEXT NOT NULL UNIQUE,
                profile_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                retrieval_backend TEXT NOT NULL DEFAULT '',
                ai_decision TEXT NOT NULL,
                ai_reason TEXT NOT NULL,
                ai_confidence REAL NOT NULL,
                assessment_mode TEXT NOT NULL DEFAULT 'heuristic',
                analysis_label TEXT NOT NULL DEFAULT 'REQUIREMENT',
                analysis_basis TEXT NOT NULL DEFAULT '',
                normalized_requirement TEXT NOT NULL DEFAULT '',
                risk_level TEXT NOT NULL DEFAULT 'HIGH',
                clarification_question TEXT NOT NULL DEFAULT '',
                final_decision TEXT,
                engineer_reason TEXT,
                model_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS evidences (
                id TEXT PRIMARY KEY,
                requirement_id TEXT NOT NULL,
                source_doc_id TEXT NOT NULL,
                source_doc_name TEXT NOT NULL,
                source_doc_version TEXT NOT NULL DEFAULT '',
                source_location TEXT NOT NULL,
                excerpt TEXT NOT NULL,
                relevance REAL NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_evidences_requirement_id ON evidences(requirement_id);

            CREATE TABLE IF NOT EXISTS assessment_runs (
                id TEXT PRIMARY KEY,
                requirement_id TEXT NOT NULL,
                profile_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL,
                retrieval_backend TEXT NOT NULL,
                model_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                input_payload_json TEXT NOT NULL,
                output_payload_json TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                fallback_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_assessment_runs_requirement_id ON assessment_runs(requirement_id);

            CREATE TABLE IF NOT EXISTS ai_analysis_jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                profile_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                assessment_mode TEXT NOT NULL DEFAULT '',
                retrieval_backend TEXT NOT NULL DEFAULT '',
                chat_model TEXT NOT NULL DEFAULT '',
                embedding_model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'PENDING',
                total_requirements INTEGER NOT NULL DEFAULT 0,
                completed_requirements INTEGER NOT NULL DEFAULT 0,
                analyzed_requirements INTEGER NOT NULL DEFAULT 0,
                info_requirements INTEGER NOT NULL DEFAULT 0,
                screened_requirements INTEGER NOT NULL DEFAULT 0,
                current_requirement_id TEXT NOT NULL DEFAULT '',
                current_requirement_code TEXT NOT NULL DEFAULT '',
                current_requirement_index INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT NOT NULL DEFAULT '',
                last_message TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_document_id ON ai_analysis_jobs(sor_document_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_status ON ai_analysis_jobs(status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS ai_analysis_events (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                profile_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                requirement_id TEXT NOT NULL DEFAULT '',
                requirement_code TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES ai_analysis_jobs(id) ON DELETE CASCADE,
                FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ai_analysis_events_job_id ON ai_analysis_events(job_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS knowledge_document_syncs (
                id TEXT PRIMARY KEY,
                knowledge_document_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                retrieval_backend TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'PENDING',
                error_message TEXT NOT NULL DEFAULT '',
                synced_at TEXT NOT NULL DEFAULT '',
                chunk_count INTEGER NOT NULL DEFAULT 0,
                payload_path TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(knowledge_document_id, profile_id),
                FOREIGN KEY(knowledge_document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_document_syncs_document_id ON knowledge_document_syncs(knowledge_document_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS review_logs (
                id TEXT PRIMARY KEY,
                requirement_id TEXT NOT NULL,
                action TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                comment TEXT,
                operator TEXT NOT NULL,
                operated_at TEXT NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_review_logs_requirement ON review_logs(requirement_id);

            CREATE TABLE IF NOT EXISTS export_jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                version_label TEXT,
                compare_export_id TEXT,
                snapshot_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS document_table_assets (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                asset_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(sor_document_id, table_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_document_table_assets_document_id ON document_table_assets(sor_document_id);
            """
        )
        _ensure_column(conn, "projects", "knowledge_base_version", "INTEGER DEFAULT 0")
        _ensure_column(conn, "requirements", "entry_kind", "TEXT DEFAULT 'REQUIREMENT'")
        _ensure_column(conn, "requirements", "text_relation", "TEXT DEFAULT 'distinct'")
        _ensure_column(conn, "requirements", "is_deduped", "INTEGER DEFAULT 0")
        _ensure_column(conn, "requirements", "parent_section_code", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirements", "parent_section_title", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirements", "parent_section_label", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirements", "heading_text", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirements", "raw_text", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirements", "display_text", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirements", "merged_from_req_codes", "TEXT DEFAULT '[]'")
        _ensure_column(conn, "requirements", "merged_from_entry_ids", "TEXT DEFAULT '[]'")
        _ensure_column(conn, "requirements", "dedupe_version", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirements", "table_refs", "TEXT DEFAULT '[]'")
        _ensure_column(conn, "requirements", "content_blocks", "TEXT DEFAULT '[]'")
        _ensure_column(conn, "requirements", "has_table_asset", "INTEGER DEFAULT 0")
        _ensure_column(conn, "requirements", "table_render_mode", "TEXT DEFAULT ''")
        _ensure_requirements_document_unique(conn)
        _cleanup_orphan_records(conn)
        _ensure_column(conn, "knowledge_documents", "doc_type", "TEXT DEFAULT 'REFERENCE'")
        _ensure_column(conn, "knowledge_documents", "doc_version", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "status", "TEXT DEFAULT 'ACTIVE'")
        _ensure_column(conn, "knowledge_documents", "effective_from", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "effective_to", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "retrieval_sync_status", "TEXT DEFAULT 'PENDING'")
        _ensure_column(conn, "knowledge_documents", "retrieval_sync_error", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "retrieval_synced_at", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "workflow_stage", "TEXT DEFAULT 'UPLOADED'")
        _ensure_column(conn, "sor_documents", "requirement_set_version", "INTEGER DEFAULT 0")
        _ensure_column(conn, "sor_documents", "extraction_locked_at", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "ai_status", "TEXT DEFAULT 'NOT_RUN'")
        _ensure_column(conn, "sor_documents", "ai_assessed_at", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "ai_requirement_set_version", "INTEGER DEFAULT 0")
        _ensure_column(conn, "sor_documents", "ai_knowledge_base_version", "INTEGER DEFAULT 0")
        _ensure_column(conn, "sor_documents", "updated_at", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "preview_status", "TEXT DEFAULT 'NOT_READY'")
        _ensure_column(conn, "sor_documents", "preview_generated_at", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "preview_file_mtime", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "preview_error", "TEXT DEFAULT ''")
        _ensure_column(conn, "export_jobs", "version_label", "TEXT")
        _ensure_column(conn, "export_jobs", "compare_export_id", "TEXT")
        _ensure_column(conn, "export_jobs", "snapshot_json", "TEXT")
        _ensure_column(conn, "assessments", "assessment_mode", "TEXT DEFAULT 'heuristic'")
        _ensure_column(conn, "assessments", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "provider", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "retrieval_backend", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "analysis_label", "TEXT DEFAULT 'REQUIREMENT'")
        _ensure_column(conn, "assessments", "analysis_basis", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "normalized_requirement", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "risk_level", "TEXT DEFAULT 'HIGH'")
        _ensure_column(conn, "assessments", "clarification_question", "TEXT DEFAULT ''")
        _ensure_column(conn, "evidences", "source_doc_version", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessment_runs", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessment_runs", "provider", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "provider", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "assessment_mode", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "retrieval_backend", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "chat_model", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "embedding_model", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_events", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_events", "provider", "TEXT DEFAULT ''")


def create_project(name: str, customer_name: str) -> dict[str, Any]:
    project_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, customer_name, created_at, knowledge_base_version) VALUES (?, ?, ?, ?, 0)",
            (project_id, name, customer_name, created_at),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _to_dict(row) or {}


def list_projects() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
    return _to_dict_list(rows)


def get_project(project_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _to_dict(row)


def add_sor_document(
    project_id: str, file_name: str, file_type: str, file_path: str, extracted_text: str
) -> dict[str, Any]:
    document_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sor_documents
            (id, project_id, file_name, file_type, file_path, extracted_text,
             created_at, workflow_stage, requirement_set_version, extraction_locked_at,
             ai_status, ai_assessed_at, ai_requirement_set_version, ai_knowledge_base_version, updated_at,
             preview_status, preview_generated_at, preview_file_mtime, preview_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'UPLOADED', 0, '', 'NOT_RUN', '', 0, 0, ?, 'NOT_READY', '', '', '')
            """,
            (document_id, project_id, file_name, file_type, file_path, extracted_text, created_at, created_at),
        )
        row = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ?", (document_id,)
        ).fetchone()
    return _to_dict(row) or {}


def get_sor_document(document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
    return _to_dict(row)


def list_sor_documents(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sor_documents WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return _to_dict_list(rows)


def backfill_sor_document_runtime(document_id: str) -> dict[str, Any]:
    updated_at = _now_iso()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return {}
        requirement_rows = conn.execute(
            "SELECT COUNT(*) AS count FROM requirements WHERE sor_document_id = ?",
            (document_id,),
        ).fetchone()
        requirement_count = int(requirement_rows["count"] or 0) if requirement_rows else 0
        if requirement_count <= 0:
            return _to_dict(row) or {}

        assessment_rows = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM assessments a
            INNER JOIN requirements r ON r.id = a.requirement_id
            WHERE r.sor_document_id = ?
            """,
            (document_id,),
        ).fetchone()
        assessment_count = int(assessment_rows["count"] or 0) if assessment_rows else 0
        project_row = conn.execute(
            "SELECT knowledge_base_version FROM projects WHERE id = ?",
            (row["project_id"],),
        ).fetchone()
        knowledge_base_version = int(project_row["knowledge_base_version"] or 0) if project_row else 0

        requirement_set_version = int(row["requirement_set_version"] or 0)
        workflow_stage = str(row["workflow_stage"] or "").strip().upper() or "UPLOADED"
        ai_status = str(row["ai_status"] or "").strip().upper() or "NOT_RUN"
        ai_requirement_set_version = int(row["ai_requirement_set_version"] or 0)
        ai_knowledge_base_version = int(row["ai_knowledge_base_version"] or 0)

        next_requirement_set_version = requirement_set_version or 1
        next_workflow_stage = workflow_stage
        if workflow_stage == "UPLOADED":
            next_workflow_stage = "AI_READY" if assessment_count > 0 else "REVIEWING"

        next_ai_status = ai_status
        if assessment_count > 0 and ai_status == "NOT_RUN":
            next_ai_status = "READY"

        next_ai_requirement_set_version = ai_requirement_set_version or (
            next_requirement_set_version if assessment_count > 0 else 0
        )
        next_ai_knowledge_base_version = ai_knowledge_base_version or (
            knowledge_base_version if assessment_count > 0 else 0
        )

        if (
            next_requirement_set_version == requirement_set_version
            and next_workflow_stage == workflow_stage
            and next_ai_status == ai_status
            and next_ai_requirement_set_version == ai_requirement_set_version
            and next_ai_knowledge_base_version == ai_knowledge_base_version
        ):
            return _to_dict(row) or {}

        conn.execute(
            """
            UPDATE sor_documents
            SET requirement_set_version = ?,
                workflow_stage = ?,
                ai_status = ?,
                ai_requirement_set_version = ?,
                ai_knowledge_base_version = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                next_requirement_set_version,
                next_workflow_stage,
                next_ai_status,
                next_ai_requirement_set_version,
                next_ai_knowledge_base_version,
                updated_at,
                document_id,
            ),
        )
        updated = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
    return _to_dict(updated) or {}


def increment_sor_document_requirement_set_version(document_id: str, workflow_stage: str) -> dict[str, Any]:
    updated_at = _now_iso()
    stage = str(workflow_stage or "EXTRACTED").strip().upper() or "EXTRACTED"
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT requirement_set_version, ai_status, ai_assessed_at
            FROM sor_documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            return {}
        next_version = int(row["requirement_set_version"] or 0) + 1
        current_ai_status = str(row["ai_status"] or "NOT_RUN").strip().upper() or "NOT_RUN"
        has_ai_result = bool(str(row["ai_assessed_at"] or "").strip()) or current_ai_status == "READY"
        next_ai_status = "STALE" if has_ai_result else "NOT_RUN"
        conn.execute(
            """
            UPDATE sor_documents
            SET workflow_stage = ?, requirement_set_version = ?, extraction_locked_at = '',
                ai_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (stage, next_version, next_ai_status, updated_at, document_id),
        )
        updated = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
    return _to_dict(updated) or {}


def lock_sor_document_extraction(document_id: str) -> dict[str, Any]:
    timestamp = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE sor_documents
            SET workflow_stage = 'EXTRACTION_LOCKED',
                extraction_locked_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, document_id),
        )
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
    return _to_dict(row) or {}


def update_sor_document_ai_state(
    document_id: str,
    *,
    ai_status: str,
    ai_assessed_at: str = "",
    ai_requirement_set_version: int | None = None,
    ai_knowledge_base_version: int | None = None,
    workflow_stage: str = "",
) -> dict[str, Any]:
    updated_at = _now_iso()
    status = str(ai_status or "NOT_RUN").strip().upper() or "NOT_RUN"
    stage = str(workflow_stage or "").strip().upper()
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
        if current is None:
            return {}
        conn.execute(
            """
            UPDATE sor_documents
            SET ai_status = ?,
                ai_assessed_at = ?,
                ai_requirement_set_version = ?,
                ai_knowledge_base_version = ?,
                workflow_stage = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                ai_assessed_at if status == "READY" else str(current["ai_assessed_at"] or ai_assessed_at or ""),
                int(ai_requirement_set_version if ai_requirement_set_version is not None else current["ai_requirement_set_version"] or 0),
                int(ai_knowledge_base_version if ai_knowledge_base_version is not None else current["ai_knowledge_base_version"] or 0),
                stage or str(current["workflow_stage"] or ""),
                updated_at,
                document_id,
            ),
        )
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
    return _to_dict(row) or {}


def update_sor_document_preview_state(
    document_id: str,
    *,
    preview_status: str | None = None,
    preview_generated_at: str | None = None,
    preview_file_mtime: str | None = None,
    preview_error: str | None = None,
) -> dict[str, Any]:
    updated_at = _now_iso()
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
        if current is None:
            return {}
        conn.execute(
            """
            UPDATE sor_documents
            SET preview_status = ?,
                preview_generated_at = ?,
                preview_file_mtime = ?,
                preview_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                str(preview_status if preview_status is not None else current["preview_status"] or "NOT_READY"),
                str(preview_generated_at if preview_generated_at is not None else current["preview_generated_at"] or ""),
                str(preview_file_mtime if preview_file_mtime is not None else current["preview_file_mtime"] or ""),
                str(preview_error if preview_error is not None else current["preview_error"] or ""),
                updated_at,
                document_id,
            ),
        )
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
    return _to_dict(row) or {}


def add_knowledge_document(
    project_id: str,
    file_name: str,
    file_type: str,
    file_path: str,
    extracted_text: str,
    *,
    doc_type: str = "REFERENCE",
    doc_version: str = "",
    status: str = "ACTIVE",
    effective_from: str = "",
    effective_to: str = "",
) -> dict[str, Any]:
    document_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_documents
            (id, project_id, file_name, file_type, file_path, extracted_text,
             doc_type, doc_version, status, effective_from, effective_to,
             retrieval_sync_status, retrieval_sync_error, retrieval_synced_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', '', '', ?)
            """,
            (
                document_id,
                project_id,
                file_name,
                file_type,
                file_path,
                extracted_text,
                doc_type,
                doc_version,
                status,
                effective_from,
                effective_to,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)
        ).fetchone()
    return _to_dict(row) or {}


def bump_project_knowledge_base_version(project_id: str) -> dict[str, Any]:
    updated_at = _now_iso()
    with get_conn() as conn:
        current = conn.execute(
            "SELECT knowledge_base_version FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if current is None:
            return {}
        next_version = int(current["knowledge_base_version"] or 0) + 1
        conn.execute(
            "UPDATE projects SET knowledge_base_version = ? WHERE id = ?",
            (next_version, project_id),
        )
        conn.execute(
            """
            UPDATE sor_documents
            SET ai_status = CASE
                    WHEN UPPER(COALESCE(ai_status, '')) = 'READY' OR COALESCE(ai_assessed_at, '') <> '' THEN 'STALE'
                    WHEN COALESCE(ai_status, '') = '' THEN 'NOT_RUN'
                    ELSE ai_status
                END,
                updated_at = ?
            WHERE project_id = ?
            """,
            (updated_at, project_id),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _to_dict(row) or {}


def list_knowledge_documents(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_documents WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return _to_dict_list(rows)

def list_knowledge_document_syncs(knowledge_document_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM knowledge_document_syncs
            WHERE knowledge_document_id = ?
            ORDER BY updated_at DESC
            """,
            (knowledge_document_id,),
        ).fetchall()
    return _to_dict_list(rows)

def get_knowledge_document_sync(knowledge_document_id: str, profile_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM knowledge_document_syncs
            WHERE knowledge_document_id = ? AND profile_id = ?
            """,
            (knowledge_document_id, profile_id),
        ).fetchone()
    return _to_dict(row)

def upsert_knowledge_document_sync(
    knowledge_document_id: str,
    *,
    profile_id: str,
    provider: str,
    retrieval_backend: str,
    status: str,
    error_message: str = "",
    synced_at: str = "",
    chunk_count: int = 0,
    payload_path: str = "",
) -> dict[str, Any]:
    sync_id = str(uuid.uuid4())
    updated_at = _now_iso()
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM knowledge_document_syncs
            WHERE knowledge_document_id = ? AND profile_id = ?
            """,
            (knowledge_document_id, profile_id),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE knowledge_document_syncs
                SET provider = ?, retrieval_backend = ?, status = ?, error_message = ?,
                    synced_at = ?, chunk_count = ?, payload_path = ?, updated_at = ?
                WHERE knowledge_document_id = ? AND profile_id = ?
                """,
                (
                    str(provider or "").strip(),
                    str(retrieval_backend or "").strip(),
                    str(status or "PENDING").strip().upper(),
                    str(error_message or ""),
                    str(synced_at or ""),
                    max(int(chunk_count or 0), 0),
                    str(payload_path or ""),
                    updated_at,
                    knowledge_document_id,
                    profile_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO knowledge_document_syncs
                (id, knowledge_document_id, profile_id, provider, retrieval_backend, status,
                 error_message, synced_at, chunk_count, payload_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sync_id,
                    knowledge_document_id,
                    str(profile_id or "").strip(),
                    str(provider or "").strip(),
                    str(retrieval_backend or "").strip(),
                    str(status or "PENDING").strip().upper(),
                    str(error_message or ""),
                    str(synced_at or ""),
                    max(int(chunk_count or 0), 0),
                    str(payload_path or ""),
                    updated_at,
                ),
            )
        row = conn.execute(
            """
            SELECT *
            FROM knowledge_document_syncs
            WHERE knowledge_document_id = ? AND profile_id = ?
            """,
            (knowledge_document_id, profile_id),
        ).fetchone()
    return _to_dict(row) or {}
def update_knowledge_document_retrieval_state(
    document_id: str,
    *,
    retrieval_sync_status: str | None = None,
    retrieval_sync_error: str | None = None,
    retrieval_synced_at: str | None = None,
) -> dict[str, Any]:
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        if current is None:
            return {}
        conn.execute(
            """
            UPDATE knowledge_documents
            SET retrieval_sync_status = ?,
                retrieval_sync_error = ?,
                retrieval_synced_at = ?
            WHERE id = ?
            """,
            (
                str(retrieval_sync_status if retrieval_sync_status is not None else current["retrieval_sync_status"] or "PENDING"),
                str(retrieval_sync_error if retrieval_sync_error is not None else current["retrieval_sync_error"] or ""),
                str(retrieval_synced_at if retrieval_synced_at is not None else current["retrieval_synced_at"] or ""),
                document_id,
            ),
        )
        row = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
    return _to_dict(row) or {}


def clear_requirements_for_document(sor_document_id: str) -> None:
    with get_conn() as conn:
        req_rows = conn.execute(
            "SELECT id FROM requirements WHERE sor_document_id = ?", (sor_document_id,)
        ).fetchall()
        req_ids = [row["id"] for row in req_rows]
        if req_ids:
            placeholders = ", ".join("?" for _ in req_ids)
            conn.execute(
                f"DELETE FROM review_logs WHERE requirement_id IN ({placeholders})", req_ids
            )
            conn.execute(
                f"DELETE FROM evidences WHERE requirement_id IN ({placeholders})", req_ids
            )
            conn.execute(
                f"DELETE FROM assessments WHERE requirement_id IN ({placeholders})", req_ids
            )
            conn.execute(
                f"DELETE FROM assessment_runs WHERE requirement_id IN ({placeholders})", req_ids
            )
        conn.execute("DELETE FROM document_table_assets WHERE sor_document_id = ?", (sor_document_id,))
        conn.execute("DELETE FROM requirements WHERE sor_document_id = ?", (sor_document_id,))


def insert_requirement(
    project_id: str,
    sor_document_id: str,
    req_code: str,
    source_page: int | None,
    source_anchor: str,
    requirement_text: str,
    requirement_type: str,
    priority: str,
    status: str = "DRAFT",
    *,
    entry_kind: str = "REQUIREMENT",
    text_relation: str = "distinct",
    is_deduped: bool = False,
    parent_section_code: str = "",
    parent_section_title: str = "",
    parent_section_label: str = "",
    heading_text: str = "",
    raw_text: str = "",
    display_text: str = "",
    merged_from_req_codes: str = "[]",
    merged_from_entry_ids: str = "[]",
    dedupe_version: str = "",
    table_refs: str = "[]",
    content_blocks: str = "[]",
    has_table_asset: bool = False,
    table_render_mode: str = "",
) -> dict[str, Any]:
    requirement_id = str(uuid.uuid4())
    timestamp = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO requirements
            (id, project_id, sor_document_id, req_code, source_page, source_anchor,
             requirement_text, requirement_type, priority, status,
             entry_kind, text_relation, is_deduped, parent_section_code, parent_section_title, parent_section_label,
             heading_text, raw_text, display_text,
             merged_from_req_codes, merged_from_entry_ids, dedupe_version,
             table_refs, content_blocks, has_table_asset, table_render_mode,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                requirement_id,
                project_id,
                sor_document_id,
                req_code,
                source_page,
                source_anchor,
                requirement_text,
                requirement_type,
                priority,
                status,
                entry_kind,
                text_relation,
                1 if is_deduped else 0,
                parent_section_code,
                parent_section_title,
                parent_section_label,
                heading_text,
                raw_text,
                display_text,
                merged_from_req_codes,
                merged_from_entry_ids,
                dedupe_version,
                table_refs,
                content_blocks,
                1 if has_table_asset else 0,
                table_render_mode,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM requirements WHERE id = ?", (requirement_id,)).fetchone()
    return _to_dict(row) or {}


def update_requirement_entry(
    requirement_id: str,
    *,
    source_page: int | None,
    source_anchor: str,
    requirement_text: str,
    requirement_type: str,
    priority: str,
    entry_kind: str,
    text_relation: str,
    is_deduped: bool,
    parent_section_code: str,
    parent_section_title: str,
    parent_section_label: str,
    heading_text: str,
    raw_text: str,
    display_text: str,
    merged_from_req_codes: str,
    merged_from_entry_ids: str,
    dedupe_version: str,
    table_refs: str,
    content_blocks: str,
    has_table_asset: bool,
    table_render_mode: str,
) -> dict[str, Any]:
    updated_at = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE requirements
            SET source_page = ?, source_anchor = ?, requirement_text = ?, requirement_type = ?, priority = ?,
                entry_kind = ?, text_relation = ?, is_deduped = ?, parent_section_code = ?, parent_section_title = ?,
                parent_section_label = ?, heading_text = ?, raw_text = ?, display_text = ?,
                merged_from_req_codes = ?, merged_from_entry_ids = ?, dedupe_version = ?,
                table_refs = ?, content_blocks = ?, has_table_asset = ?, table_render_mode = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                source_page,
                source_anchor,
                requirement_text,
                requirement_type,
                priority,
                entry_kind,
                text_relation,
                1 if is_deduped else 0,
                parent_section_code,
                parent_section_title,
                parent_section_label,
                heading_text,
                raw_text,
                display_text,
                merged_from_req_codes,
                merged_from_entry_ids,
                dedupe_version,
                table_refs,
                content_blocks,
                1 if has_table_asset else 0,
                table_render_mode,
                updated_at,
                requirement_id,
            ),
        )
        row = conn.execute("SELECT * FROM requirements WHERE id = ?", (requirement_id,)).fetchone()
    return _to_dict(row) or {}


def delete_requirement(requirement_id: str) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sor_document_id FROM requirements WHERE id = ?",
            (requirement_id,),
        ).fetchone()
        conn.execute("DELETE FROM review_logs WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM evidences WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM assessments WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM assessment_runs WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM requirements WHERE id = ?", (requirement_id,))
        if row is not None:
            conn.execute("DELETE FROM document_table_assets WHERE sor_document_id = ?", (row["sor_document_id"],))


def list_requirements(project_id: str, sor_document_id: str | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if sor_document_id:
            rows = conn.execute(
                """
                  SELECT r.*, a.profile_id, a.provider, a.retrieval_backend, a.ai_decision, a.ai_reason, a.ai_confidence, a.assessment_mode, a.analysis_label,
                       a.analysis_basis,
                      a.normalized_requirement, a.risk_level, a.clarification_question, a.model_name, a.prompt_version,
                       a.final_decision, a.engineer_reason
                FROM requirements r
                LEFT JOIN assessments a ON a.requirement_id = r.id
                WHERE r.project_id = ? AND r.sor_document_id = ?
                ORDER BY r.req_code
                """,
                (project_id, sor_document_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                  SELECT r.*, a.profile_id, a.provider, a.retrieval_backend, a.ai_decision, a.ai_reason, a.ai_confidence, a.assessment_mode, a.analysis_label,
                       a.analysis_basis,
                      a.normalized_requirement, a.risk_level, a.clarification_question, a.model_name, a.prompt_version,
                       a.final_decision, a.engineer_reason
                FROM requirements r
                LEFT JOIN assessments a ON a.requirement_id = r.id
                WHERE r.project_id = ?
                ORDER BY r.req_code
                """,
                (project_id,),
            ).fetchall()

        output: list[dict[str, Any]] = []
        for row in rows:
            item = _normalize_requirement_record(_to_dict(row) or {})
            evidence_rows = conn.execute(
                """
                SELECT source_doc_id, source_doc_name, source_doc_version, source_location, excerpt, relevance
                FROM evidences
                WHERE requirement_id = ?
                ORDER BY relevance DESC
                """,
                (row["id"],),
            ).fetchall()
            item["evidences"] = _to_dict_list(evidence_rows)
            output.append(item)
    return output


def get_requirement(requirement_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
                 SELECT r.*, a.profile_id, a.provider, a.retrieval_backend, a.ai_decision, a.ai_reason, a.ai_confidence, a.assessment_mode, a.analysis_label,
                   a.analysis_basis,
                     a.normalized_requirement, a.risk_level, a.clarification_question, a.model_name, a.prompt_version,
                   a.final_decision, a.engineer_reason
            FROM requirements r
            LEFT JOIN assessments a ON a.requirement_id = r.id
            WHERE r.id = ?
            """,
            (requirement_id,),
        ).fetchone()
        if row is None:
            return None
        item = _normalize_requirement_record(_to_dict(row) or {})
        evidence_rows = conn.execute(
            """
            SELECT source_doc_id, source_doc_name, source_doc_version, source_location, excerpt, relevance
            FROM evidences
            WHERE requirement_id = ?
            ORDER BY relevance DESC
            """,
            (requirement_id,),
        ).fetchall()
        item["evidences"] = _to_dict_list(evidence_rows)
    return item


def get_assessment(requirement_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM assessments WHERE requirement_id = ?", (requirement_id,)
        ).fetchone()
    return _to_dict(row)


def upsert_assessment(
    requirement_id: str,
    profile_id: str,
    provider: str,
    ai_decision: str,
    ai_reason: str,
    ai_confidence: float,
    assessment_mode: str,
    retrieval_backend: str,
    analysis_label: str,
    analysis_basis: str,
    normalized_requirement: str,
    risk_level: str,
    clarification_question: str,
    model_name: str,
    prompt_version: str,
) -> dict[str, Any]:
    assessment_id = str(uuid.uuid4())
    updated_at = _now_iso()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, final_decision, engineer_reason FROM assessments WHERE requirement_id = ?",
            (requirement_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE assessments
                SET profile_id = ?, provider = ?, retrieval_backend = ?,
                    ai_decision = ?, ai_reason = ?, ai_confidence = ?, assessment_mode = ?, analysis_label = ?,
                    analysis_basis = ?, normalized_requirement = ?, risk_level = ?, clarification_question = ?,
                    model_name = ?, prompt_version = ?, updated_at = ?
                WHERE requirement_id = ?
                """,
                (
                    profile_id,
                    provider,
                    retrieval_backend,
                    ai_decision,
                    ai_reason,
                    ai_confidence,
                    assessment_mode,
                    analysis_label,
                    analysis_basis,
                    normalized_requirement,
                    risk_level,
                    clarification_question,
                    model_name,
                    prompt_version,
                    updated_at,
                    requirement_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO assessments
                (id, requirement_id, profile_id, provider, retrieval_backend, ai_decision, ai_reason, ai_confidence,
                 assessment_mode, analysis_label, analysis_basis, normalized_requirement, risk_level, clarification_question,
                 final_decision, engineer_reason, model_name, prompt_version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    assessment_id,
                    requirement_id,
                    profile_id,
                    provider,
                    retrieval_backend,
                    ai_decision,
                    ai_reason,
                    ai_confidence,
                    assessment_mode,
                    analysis_label,
                    analysis_basis,
                    normalized_requirement,
                    risk_level,
                    clarification_question,
                    model_name,
                    prompt_version,
                    updated_at,
                ),
            )
        row = conn.execute(
            "SELECT * FROM assessments WHERE requirement_id = ?", (requirement_id,)
        ).fetchone()
    return _to_dict(row) or {}


def replace_evidences(requirement_id: str, evidences: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM evidences WHERE requirement_id = ?", (requirement_id,))
        for evidence in evidences:
            conn.execute(
                """
                INSERT INTO evidences
                (id, requirement_id, source_doc_id, source_doc_name, source_doc_version, source_location, excerpt, relevance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    requirement_id,
                    evidence["source_doc_id"],
                    evidence["source_doc_name"],
                    str(evidence.get("source_doc_version") or ""),
                    evidence["source_location"],
                    evidence["excerpt"],
                    float(evidence["relevance"]),
                ),
            )


def add_assessment_run(
    requirement_id: str,
    *,
    profile_id: str,
    provider: str,
    mode: str,
    retrieval_backend: str,
    model_name: str,
    prompt_version: str,
    input_payload_json: str,
    output_payload_json: str,
    validation_status: str,
    fallback_reason: str = "",
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO assessment_runs
            (id, requirement_id, profile_id, provider, mode, retrieval_backend, model_name, prompt_version,
             input_payload_json, output_payload_json, validation_status, fallback_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                requirement_id,
                profile_id,
                provider,
                mode,
                retrieval_backend,
                model_name,
                prompt_version,
                input_payload_json,
                output_payload_json,
                validation_status,
                fallback_reason,
                created_at,
            ),
        )
        row = conn.execute("SELECT * FROM assessment_runs WHERE id = ?", (run_id,)).fetchone()
    return _to_dict(row) or {}


def list_assessment_runs(requirement_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM assessment_runs
            WHERE requirement_id = ?
            ORDER BY created_at DESC
            """,
            (requirement_id,),
        ).fetchall()
    return _to_dict_list(rows)


def create_ai_analysis_job(
    project_id: str,
    sor_document_id: str,
    *,
    profile_id: str = "",
    provider: str = "",
    assessment_mode: str = "",
    retrieval_backend: str = "",
    chat_model: str = "",
    embedding_model: str = "",
    total_requirements: int,
    status: str = "PENDING",
    current_stage: str = "queued",
    last_message: str = "AI analysis queued.",
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    timestamp = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ai_analysis_jobs
            (id, project_id, sor_document_id, profile_id, provider, assessment_mode, retrieval_backend, chat_model, embedding_model, status, total_requirements,
             completed_requirements, analyzed_requirements, info_requirements, screened_requirements,
             current_requirement_id, current_requirement_code, current_requirement_index,
             current_stage, last_message, error_message,
             created_at, started_at, finished_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, '', '', 0, ?, ?, '', ?, '', '', ?)
            """,
            (
                job_id,
                project_id,
                sor_document_id,
                str(profile_id or "").strip(),
                str(provider or "").strip(),
                str(assessment_mode or "").strip(),
                str(retrieval_backend or "").strip(),
                str(chat_model or "").strip(),
                str(embedding_model or "").strip(),
                str(status or "PENDING").strip().upper() or "PENDING",
                max(int(total_requirements or 0), 0),
                str(current_stage or "queued").strip(),
                str(last_message or "AI analysis queued.").strip(),
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM ai_analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    return _to_dict(row) or {}


def get_ai_analysis_job(job_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ai_analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    return _to_dict(row)


def get_latest_ai_analysis_job(sor_document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM ai_analysis_jobs
            WHERE sor_document_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sor_document_id,),
        ).fetchone()
    return _to_dict(row)


def get_running_ai_analysis_job(sor_document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM ai_analysis_jobs
            WHERE sor_document_id = ?
              AND status IN ('PENDING', 'RUNNING')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sor_document_id,),
        ).fetchone()
    return _to_dict(row)


def update_ai_analysis_job(
    job_id: str,
    *,
    status: str | None = None,
    total_requirements: int | None = None,
    completed_requirements: int | None = None,
    analyzed_requirements: int | None = None,
    info_requirements: int | None = None,
    screened_requirements: int | None = None,
    current_requirement_id: str | None = None,
    current_requirement_code: str | None = None,
    current_requirement_index: int | None = None,
    current_stage: str | None = None,
    last_message: str | None = None,
    error_message: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    updated_at = _now_iso()
    assignments: list[str] = []
    values: list[Any] = []

    if status is not None:
        assignments.append("status = ?")
        values.append(str(status or "").strip().upper())
    if total_requirements is not None:
        assignments.append("total_requirements = ?")
        values.append(max(int(total_requirements or 0), 0))
    if completed_requirements is not None:
        assignments.append("completed_requirements = ?")
        values.append(max(int(completed_requirements or 0), 0))
    if analyzed_requirements is not None:
        assignments.append("analyzed_requirements = ?")
        values.append(max(int(analyzed_requirements or 0), 0))
    if info_requirements is not None:
        assignments.append("info_requirements = ?")
        values.append(max(int(info_requirements or 0), 0))
    if screened_requirements is not None:
        assignments.append("screened_requirements = ?")
        values.append(max(int(screened_requirements or 0), 0))
    if current_requirement_id is not None:
        assignments.append("current_requirement_id = ?")
        values.append(str(current_requirement_id or ""))
    if current_requirement_code is not None:
        assignments.append("current_requirement_code = ?")
        values.append(str(current_requirement_code or ""))
    if current_requirement_index is not None:
        assignments.append("current_requirement_index = ?")
        values.append(max(int(current_requirement_index or 0), 0))
    if current_stage is not None:
        assignments.append("current_stage = ?")
        values.append(str(current_stage or "").strip())
    if last_message is not None:
        assignments.append("last_message = ?")
        values.append(str(last_message or ""))
    if error_message is not None:
        assignments.append("error_message = ?")
        values.append(str(error_message or ""))
    if started_at is not None:
        assignments.append("started_at = ?")
        values.append(str(started_at or ""))
    if finished_at is not None:
        assignments.append("finished_at = ?")
        values.append(str(finished_at or ""))

    assignments.append("updated_at = ?")
    values.append(updated_at)
    values.append(job_id)

    with get_conn() as conn:
        conn.execute(
            f"UPDATE ai_analysis_jobs SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        row = conn.execute("SELECT * FROM ai_analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    return _to_dict(row) or {}


def add_ai_analysis_event(
    *,
    job_id: str,
    sor_document_id: str,
    profile_id: str = "",
    provider: str = "",
    event_type: str,
    message: str,
    stage: str = "",
    requirement_id: str = "",
    requirement_code: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ai_analysis_events
            (id, job_id, sor_document_id, profile_id, provider, event_type, stage, message,
             requirement_id, requirement_code, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                job_id,
                sor_document_id,
                str(profile_id or "").strip(),
                str(provider or "").strip(),
                str(event_type or "").strip(),
                str(stage or "").strip(),
                str(message or "").strip(),
                str(requirement_id or "").strip(),
                str(requirement_code or "").strip(),
                json.dumps(payload or {}, ensure_ascii=False),
                created_at,
            ),
        )
        row = conn.execute("SELECT * FROM ai_analysis_events WHERE id = ?", (event_id,)).fetchone()
    item = _to_dict(row) or {}
    item["payload"] = _decode_json_payload(item.get("payload_json"), default={})
    return item


def list_ai_analysis_events(job_id: str, *, limit: int = 80) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 80), 500))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM ai_analysis_events
            WHERE job_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (job_id, safe_limit),
        ).fetchall()
    items = list(reversed(_to_dict_list(rows)))
    for item in items:
        item["payload"] = _decode_json_payload(item.get("payload_json"), default={})
    return items


def update_requirement_parent_sections(updates: list[dict[str, str]]) -> None:
    if not updates:
        return
    timestamp = _now_iso()
    payload = [
        (
            str(item.get("parent_section_code") or ""),
            str(item.get("parent_section_title") or ""),
            str(item.get("parent_section_label") or ""),
            timestamp,
            str(item.get("id") or ""),
        )
        for item in updates
        if str(item.get("id") or "").strip()
    ]
    if not payload:
        return
    with get_conn() as conn:
        conn.executemany(
            """
            UPDATE requirements
            SET parent_section_code = ?, parent_section_title = ?, parent_section_label = ?, updated_at = ?
            WHERE id = ?
            """,
            payload,
        )


def update_requirement_status(requirement_id: str, status: str) -> None:
    timestamp = _now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE requirements SET status = ?, updated_at = ? WHERE id = ?",
            (status, timestamp, requirement_id),
        )


def update_final_decision(requirement_id: str, final_decision: str, engineer_reason: str) -> dict[str, Any]:
    updated_at = _now_iso()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT final_decision, engineer_reason FROM assessments WHERE requirement_id = ?",
            (requirement_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO assessments
                (id, requirement_id, ai_decision, ai_reason, ai_confidence,
                 assessment_mode, analysis_label, analysis_basis, normalized_requirement, risk_level, clarification_question,
                 final_decision, engineer_reason, model_name, prompt_version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    requirement_id,
                    "NEED_CLARIFICATION",
                    "No AI assessment available.",
                    0.0,
                    "manual",
                    "REQUIREMENT",
                    "",
                    "",
                    "HIGH",
                    "",
                    final_decision,
                    engineer_reason,
                    "manual",
                    "manual",
                    updated_at,
                ),
            )
            old_value: dict[str, Any] = {"final_decision": None, "engineer_reason": None}
        else:
            old_value = {
                "final_decision": row["final_decision"],
                "engineer_reason": row["engineer_reason"],
            }
            conn.execute(
                """
                UPDATE assessments
                SET final_decision = ?, engineer_reason = ?, updated_at = ?
                WHERE requirement_id = ?
                """,
                (final_decision, engineer_reason, updated_at, requirement_id),
            )

        conn.execute(
            "UPDATE requirements SET status = ?, updated_at = ? WHERE id = ?",
            ("IN_REVIEW", updated_at, requirement_id),
        )

        new_value = {"final_decision": final_decision, "engineer_reason": engineer_reason}
        conn.execute(
            """
            INSERT INTO review_logs (id, requirement_id, action, old_value, new_value, comment, operator, operated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                requirement_id,
                "EDIT_DECISION",
                json.dumps(old_value, ensure_ascii=False),
                json.dumps(new_value, ensure_ascii=False),
                "Manual review update",
                "engineer",
                updated_at,
            ),
        )

        updated = conn.execute(
            "SELECT * FROM assessments WHERE requirement_id = ?", (requirement_id,)
        ).fetchone()
    return _to_dict(updated) or {}


def list_review_logs(requirement_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT action, old_value, new_value, comment, operator, operated_at
            FROM review_logs
            WHERE requirement_id = ?
            ORDER BY operated_at DESC
            """,
            (requirement_id,),
        ).fetchall()
    return _to_dict_list(rows)


def create_export_job(
    project_id: str,
    file_name: str,
    file_path: str,
    *,
    version_label: str = "",
    compare_export_id: str = "",
    snapshot_json: str = "",
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    created_at = _now_iso()
    attempts = 4
    for attempt in range(attempts):
        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO export_jobs
                    (id, project_id, file_name, file_path, version_label, compare_export_id, snapshot_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        project_id,
                        file_name,
                        file_path,
                        version_label.strip(),
                        compare_export_id.strip(),
                        snapshot_json,
                        created_at,
                    ),
                )
                row = conn.execute("SELECT * FROM export_jobs WHERE id = ?", (job_id,)).fetchone()
            return _to_dict(row) or {}
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= attempts - 1:
                raise
            time.sleep(0.15 * (attempt + 1))
    return {}


def replace_document_table_assets(
    project_id: str,
    sor_document_id: str,
    assets: list[dict[str, Any]],
) -> None:
    timestamp = _now_iso()
    deduped_assets: list[dict[str, Any]] = []
    seen_table_ids: set[str] = set()
    for asset in assets:
        table_id = str(asset.get("table_id") or "").strip()
        if not table_id or table_id in seen_table_ids:
            continue
        seen_table_ids.add(table_id)
        deduped_assets.append(asset)
    with get_conn() as conn:
        conn.execute("DELETE FROM document_table_assets WHERE sor_document_id = ?", (sor_document_id,))
        for asset in deduped_assets:
            table_id = str(asset.get("table_id") or "").strip()
            if not table_id:
                continue
            conn.execute(
                """
                INSERT INTO document_table_assets
                (id, project_id, sor_document_id, table_id, asset_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    project_id,
                    sor_document_id,
                    table_id,
                    json.dumps(asset, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )


def list_document_table_assets(project_id: str, sor_document_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT asset_json
            FROM document_table_assets
            WHERE project_id = ? AND sor_document_id = ?
            ORDER BY table_id
            """,
            (project_id, sor_document_id),
        ).fetchall()
    assets: list[dict[str, Any]] = []
    for row in rows:
        payload = _decode_json_payload(row["asset_json"], default={})
        if isinstance(payload, dict):
            assets.append(payload)
    return assets


def get_document_table_asset(project_id: str, sor_document_id: str, table_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT asset_json
            FROM document_table_assets
            WHERE project_id = ? AND sor_document_id = ? AND table_id = ?
            """,
            (project_id, sor_document_id, table_id),
        ).fetchone()
    if row is None:
        return None
    payload = _decode_json_payload(row["asset_json"], default={})
    return payload if isinstance(payload, dict) else None


def list_export_jobs(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, project_id, file_name, file_path, version_label, compare_export_id, created_at
            FROM export_jobs
            WHERE project_id = ?
            ORDER BY created_at DESC
            """,
            (project_id,),
        ).fetchall()
    return _to_dict_list(rows)


def get_export_job(export_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM export_jobs WHERE id = ?", (export_id,)).fetchone()
    return _to_dict(row)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {str(row[1]) for row in rows}
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_requirements_document_unique(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'requirements'"
    ).fetchone()
    schema_sql = str(row[0] or "") if row else ""
    if "UNIQUE(sor_document_id, req_code)" in schema_sql:
        return

    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.executescript(
        """
        CREATE TABLE requirements_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            sor_document_id TEXT NOT NULL,
            req_code TEXT NOT NULL,
            source_page INTEGER,
            source_anchor TEXT,
            requirement_text TEXT NOT NULL,
            requirement_type TEXT NOT NULL,
            priority TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            entry_kind TEXT DEFAULT 'REQUIREMENT',
            text_relation TEXT DEFAULT 'distinct',
            is_deduped INTEGER DEFAULT 0,
            heading_text TEXT DEFAULT '',
            raw_text TEXT DEFAULT '',
            display_text TEXT DEFAULT '',
            merged_from_req_codes TEXT DEFAULT '[]',
            merged_from_entry_ids TEXT DEFAULT '[]',
            dedupe_version TEXT DEFAULT '',
            parent_section_code TEXT DEFAULT '',
            parent_section_title TEXT DEFAULT '',
            parent_section_label TEXT DEFAULT '',
            table_refs TEXT DEFAULT '[]',
            content_blocks TEXT DEFAULT '[]',
            has_table_asset INTEGER DEFAULT 0,
            table_render_mode TEXT DEFAULT '',
            UNIQUE(sor_document_id, req_code),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
        );

        INSERT INTO requirements_new (
            id, project_id, sor_document_id, req_code, source_page, source_anchor,
            requirement_text, requirement_type, priority, status, created_at, updated_at,
            entry_kind, text_relation, is_deduped, heading_text, raw_text, display_text,
            merged_from_req_codes, merged_from_entry_ids, dedupe_version,
            parent_section_code, parent_section_title, parent_section_label,
            table_refs, content_blocks, has_table_asset, table_render_mode
        )
        SELECT
            id, project_id, sor_document_id, req_code, source_page, source_anchor,
            requirement_text, requirement_type, priority, status, created_at, updated_at,
            entry_kind, text_relation, is_deduped, heading_text, raw_text, display_text,
            merged_from_req_codes, merged_from_entry_ids, dedupe_version,
            parent_section_code, parent_section_title, parent_section_label,
            COALESCE(table_refs, '[]'), COALESCE(content_blocks, '[]'),
            COALESCE(has_table_asset, 0), COALESCE(table_render_mode, '')
        FROM requirements;

        DROP TABLE requirements;
        ALTER TABLE requirements_new RENAME TO requirements;
        CREATE INDEX IF NOT EXISTS idx_requirements_project_id ON requirements(project_id);
        CREATE INDEX IF NOT EXISTS idx_requirements_document_id ON requirements(sor_document_id);
        """
    )
    conn.execute("PRAGMA foreign_keys = ON;")


def _cleanup_orphan_records(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM knowledge_documents
        WHERE project_id NOT IN (SELECT id FROM projects);

        DELETE FROM sor_documents
        WHERE project_id NOT IN (SELECT id FROM projects);

        DELETE FROM export_jobs
        WHERE project_id NOT IN (SELECT id FROM projects);

        DELETE FROM document_table_assets
        WHERE project_id NOT IN (SELECT id FROM projects)
           OR sor_document_id NOT IN (SELECT id FROM sor_documents);

        DELETE FROM requirements
        WHERE project_id NOT IN (SELECT id FROM projects)
           OR sor_document_id NOT IN (SELECT id FROM sor_documents);

        DELETE FROM review_logs
        WHERE requirement_id NOT IN (SELECT id FROM requirements);

        DELETE FROM evidences
        WHERE requirement_id NOT IN (SELECT id FROM requirements);

        DELETE FROM assessments
        WHERE requirement_id NOT IN (SELECT id FROM requirements);

        DELETE FROM assessment_runs
        WHERE requirement_id NOT IN (SELECT id FROM requirements);

          DELETE FROM ai_analysis_jobs
          WHERE project_id NOT IN (SELECT id FROM projects)
              OR sor_document_id NOT IN (SELECT id FROM sor_documents);

          DELETE FROM ai_analysis_events
          WHERE job_id NOT IN (SELECT id FROM ai_analysis_jobs)
              OR sor_document_id NOT IN (SELECT id FROM sor_documents);
        """
    )
