from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Iterable

from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.infrastructure.postgres_compat import PostgresCompatConnection
from reqpilotx_server.infrastructure.runtime_settings import (
    DatabaseBackend,
    RuntimeSettings,
)
from reqpilotx_server.runtime_paths import resolve_storage_dir
from reqpilotx_server.resource_paths import (
    configured_development_root,
    postgres_migration_dir,
    sqlite_migration_dir,
)
from reqpilotx_server.services.complex_table_mode import hydrate_complex_row_metadata
from reqpilotx_server.adapters.persistence.entry_commands import (
    claim_receipt, complete_receipt, read_receipt, request_hash, validate_command_id,
)
from reqpilotx_server.services.table_assets import (
    _refresh_enriched_entry_text_fields,
    normalize_preserved_entry_table_assets,
)

DEVELOPMENT_ROOT = configured_development_root()
PROTOTYPE_DIR = DEVELOPMENT_ROOT / "prototype" if DEVELOPMENT_ROOT else None
STORAGE_DIR = resolve_storage_dir(PROTOTYPE_DIR)
DB_PATH = STORAGE_DIR / "app.db"
_RUNTIME_SETTINGS = RuntimeSettings.from_environment(prototype_dir=PROTOTYPE_DIR)
_POSTGRES_DATABASE: PostgresDatabase | None = None


class RequirementSetMutationBlocked(PermissionError):
    """Raised when a serialized Requirement Set mutation is not permitted."""


class SQLiteMigrationDriftError(RuntimeError):
    """Raised when an applied lightweight SQLite migration changed."""


class RequirementSetPublicationConflict(RuntimeError):
    """Raised when a publication receipt is no longer the current truth."""


class DocumentExtractionPageStagingFenceError(RuntimeError):
    """Raised when a staged-page writer no longer owns its exact delivery."""


class RequirementSetPublicationCurrentnessConflict(
    RequirementSetMutationBlocked
):
    """Raised when Structure/table facts advanced during publication."""


_REQUIREMENT_SET_PUBLICATION_OWNER_RE = re.compile(
    r"^publication-owner-[0-9a-f]{32}$"
)
_DEFAULT_REQUIREMENT_SET_PUBLICATION_LEASE_SECONDS = 300
HISTORICAL_PUBLICATION_RECOVERY_CONFIRMATION = (
    "RECOVER_HISTORICAL_EMPTY_PUBLICATION_LEASE"
)


def new_requirement_set_publication_owner_token() -> str:
    return f"publication-owner-{uuid.uuid4().hex}"


def _requirement_set_publication_lease_values(
    *,
    owner_token: str,
    lease_seconds: int,
    now_utc: datetime | None,
) -> tuple[str, str, str]:
    normalized_owner = str(owner_token or "").strip()
    if not _REQUIREMENT_SET_PUBLICATION_OWNER_RE.fullmatch(normalized_owner):
        raise ValueError("publication owner token is invalid")
    if not 30 <= int(lease_seconds) <= 3600:
        raise ValueError("publication lease_seconds must be between 30 and 3600")
    current = _requirement_set_publication_now_utc(now_utc)
    return (
        normalized_owner,
        current.isoformat(),
        (current + timedelta(seconds=int(lease_seconds))).isoformat(),
    )


def _requirement_set_publication_now_utc(
    now_utc: datetime | None,
) -> datetime:
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("publication lease clock must be timezone-aware")
    return current.astimezone(timezone.utc)


def _publication_lease_is_expired(value: Any, *, now_utc: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    current = _requirement_set_publication_now_utc(now_utc)
    return parsed.astimezone(timezone.utc) <= current


def derived_requirement_set_publication_key(
    *,
    base_publication_key: str,
    source_requirement_set_id: str,
    target_requirement_set_id: str,
) -> str:
    """Bind an editable clone to its immutable semantic publication origin."""

    normalized_base = str(base_publication_key or "").strip().lower()
    source_id = str(source_requirement_set_id or "").strip()
    target_id = str(target_requirement_set_id or "").strip()
    if (
        re.fullmatch(r"[0-9a-f]{64}", normalized_base) is None
        or not source_id
        or not target_id
        or source_id == target_id
    ):
        raise ValueError("derived Requirement Set publication lineage is invalid")
    payload = {
        "schema_version": "derived-requirement-set-publication/v1",
        "base_publication_key": normalized_base,
        "source_requirement_set_id": source_id,
        "target_requirement_set_id": target_id,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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


def _normalize_assessment_record(item: dict[str, Any]) -> dict[str, Any]:
    item["compliance_status"] = str(item.get("compliance_status") or "")
    item["reference_status"] = str(
        item.get("reference_status") or "CURRENT"
    ).strip().upper()
    item["result_origin"] = str(
        item.get("result_origin") or "NATIVE"
    ).strip().upper()
    item["source_assessment_id"] = str(
        item.get("source_assessment_id") or ""
    )
    item["impact_receipt_id"] = str(item.get("impact_receipt_id") or "")
    item["analysis_input_schema_version"] = str(
        item.get("analysis_input_schema_version") or ""
    )
    item["analysis_input_sha256"] = str(
        item.get("analysis_input_sha256") or ""
    )
    item["evidence_findings"] = _decode_json_payload(
        item.get("evidence_findings_json"), default=[]
    )
    item["structured_information_gap"] = _decode_json_payload(
        item.get("structured_information_gap_json"), default={}
    )
    item["sub_judgments"] = _decode_json_payload(
        item.get("sub_judgments_json"), default=[]
    )
    return item


def _normalize_evidence_record(item: dict[str, Any]) -> dict[str, Any]:
    item["retrieval_evidence_id"] = str(
        item.get("retrieval_evidence_id") or ""
    )
    item["content_hash"] = str(item.get("content_hash") or "")
    item["evidence_contract_version"] = str(
        item.get("evidence_contract_version") or ""
    )
    item["content_kind"] = str(item.get("content_kind") or "")
    item["evidence_metadata"] = _decode_json_payload(
        item.get("evidence_metadata_json"), default={}
    )
    return item


def _canonical_json_text(value: Any, *, default: Any) -> str:
    normalized = _decode_json_payload(value, default=default)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_requirement_record(item: dict[str, Any]) -> dict[str, Any]:
    item["requirement_set_id"] = str(item.get("requirement_set_id") or "")
    item["prior_requirement_id"] = str(item.get("prior_requirement_id") or "")
    item["decision_reference"] = _decode_json_payload(
        item.get("decision_reference_json"), default={}
    )
    item["preview_generation_id"] = str(item.get("preview_generation_id") or "")
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
    item["assessment_reference_status"] = str(item.get("reference_status") or "CURRENT")
    item["assessment_source_requirement_id"] = str(item.get("source_requirement_id") or "")
    item["assessment_result_origin"] = str(item.get("result_origin") or "NATIVE")
    item["assessment_source_assessment_id"] = str(
        item.get("source_assessment_id") or ""
    )
    item["assessment_impact_receipt_id"] = str(
        item.get("impact_receipt_id") or ""
    )
    item["assessment_analysis_input_schema_version"] = str(
        item.get("analysis_input_schema_version") or ""
    )
    item["assessment_analysis_input_sha256"] = str(
        item.get("analysis_input_sha256") or ""
    )
    item = _normalize_assessment_record(item)
    if item["table_refs"] or item["content_blocks"]:
        item = normalize_preserved_entry_table_assets(item)
        item = _refresh_enriched_entry_text_fields(item)
    return hydrate_complex_row_metadata(item)


@contextmanager
def get_conn() -> Generator[Any, None, None]:
    if _RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        database = _postgres_database()
        with database.connection() as connection:
            yield PostgresCompatConnection(connection)
        return
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


def configure_runtime(
    settings: RuntimeSettings,
    *,
    postgres_database: PostgresDatabase | None = None,
) -> None:
    global _RUNTIME_SETTINGS, _POSTGRES_DATABASE
    if (
        _POSTGRES_DATABASE is not None
        and _POSTGRES_DATABASE is not postgres_database
    ):
        _POSTGRES_DATABASE.close()
    _RUNTIME_SETTINGS = settings
    _POSTGRES_DATABASE = postgres_database


def close_runtime_database() -> None:
    global _POSTGRES_DATABASE
    if _POSTGRES_DATABASE is not None:
        _POSTGRES_DATABASE.close()
        _POSTGRES_DATABASE = None


def is_postgresql_runtime() -> bool:
    return _RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL


def _postgres_database() -> PostgresDatabase:
    global _POSTGRES_DATABASE
    if _POSTGRES_DATABASE is None:
        _POSTGRES_DATABASE = PostgresDatabase(_RUNTIME_SETTINGS.database_url)
    return _POSTGRES_DATABASE


_SQLITE_MIGRATION_TABLE = "reqpilotx_local_schema_migrations"


def _apply_sqlite_migrations(conn: sqlite3.Connection) -> None:
    """Apply application-owned SQLite schema migrations exactly once."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SQLITE_MIGRATION_TABLE} (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        str(row["version"]): str(row["sha256"])
        for row in conn.execute(
            f"SELECT version, sha256 FROM {_SQLITE_MIGRATION_TABLE}"
        ).fetchall()
    }
    for path in sorted(sqlite_migration_dir().glob("*.sql")):
        version = path.stem
        sql = path.read_text(encoding="utf-8")
        sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        existing_sha256 = applied.get(version)
        if existing_sha256 is not None:
            if existing_sha256 != sha256:
                raise SQLiteMigrationDriftError(
                    f"SQLite migration {version} checksum changed after application."
                )
            continue
        # The baseline script is additive (CREATE ... IF NOT EXISTS), so a
        # crashed first initialization is safely repaired by the next startup.
        conn.executescript(sql)
        conn.execute(
            f"""
            INSERT INTO {_SQLITE_MIGRATION_TABLE}
            (version, name, sha256, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                version,
                path.name,
                sha256,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def init_db() -> None:
    if is_postgresql_runtime():
        _postgres_database().apply_migrations(postgres_migration_dir())
        return
    with get_conn() as conn:
        _apply_sqlite_migrations(conn)
        ai_job_columns_before_migration = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(ai_analysis_jobs)").fetchall()
        }
        backfill_ai_job_knowledge_snapshot = "knowledge_base_version" not in ai_job_columns_before_migration
        backfill_ai_job_set_version = "requirement_set_version" not in ai_job_columns_before_migration
        backfill_ai_job_tree_sha256 = "requirement_tree_sha256" not in ai_job_columns_before_migration
        backfill_ai_job_published_count = "published_requirements" not in ai_job_columns_before_migration

        _ensure_column(conn, "projects", "knowledge_base_version", "INTEGER DEFAULT 0")
        _ensure_column(
            conn,
            "projects",
            "processing_profile_json",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "projects",
            "processing_profile_revision",
            "INTEGER DEFAULT 0",
        )
        _ensure_column(
            conn,
            "projects",
            "processing_profile_sha256",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "reading_sessions",
            "effective_bundle_id",
            "TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            "reading_sessions",
            "visual_generation_id",
            "TEXT NOT NULL DEFAULT ''",
        )
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
        _ensure_column(conn, "requirements", "preview_generation_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "doc_type", "TEXT DEFAULT 'REFERENCE'")
        _ensure_column(conn, "knowledge_documents", "doc_version", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "status", "TEXT DEFAULT 'ACTIVE'")
        _ensure_column(conn, "knowledge_documents", "effective_from", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "effective_to", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "authority", "TEXT DEFAULT 'UNKNOWN'")
        _ensure_column(conn, "knowledge_documents", "claim_role", "TEXT DEFAULT 'UNKNOWN'")
        _ensure_column(conn, "knowledge_documents", "applicability", "TEXT DEFAULT 'UNKNOWN'")
        _ensure_column(conn, "knowledge_documents", "retrieval_sync_status", "TEXT DEFAULT 'PENDING'")
        _ensure_column(conn, "knowledge_documents", "retrieval_sync_error", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "retrieval_synced_at", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "source_sha256", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "source_size", "INTEGER DEFAULT 0")
        _ensure_column(conn, "knowledge_documents", "declared_extension", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "detected_format", "TEXT DEFAULT ''")
        _ensure_column(conn, "knowledge_documents", "preflight_schema_version", "TEXT DEFAULT ''")
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
        _ensure_column(conn, "sor_documents", "preview_generation_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "active_requirement_set_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "source_sha256", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "source_size", "INTEGER DEFAULT 0")
        _ensure_column(conn, "sor_documents", "declared_extension", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "detected_format", "TEXT DEFAULT ''")
        _ensure_column(conn, "sor_documents", "preflight_schema_version", "TEXT DEFAULT ''")
        # Legacy SQLite databases may predate ``source_sha256``. Build the
        # composite identity index only after the compatibility columns exist.
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_sor_documents_asset_identity
                ON sor_documents(id, project_id, source_sha256)
            """
        )
        _ensure_column(
            conn,
            "sor_documents",
            "publication_currentness_generation",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # Test-mode SQLite mirrors the PostgreSQL append trigger so
        # independent connections/processes exercise the same publication
        # currentness fence without changing the manual row-group adapter.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS table_projection_revisions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                structure_snapshot_id TEXT NOT NULL,
                logical_table_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                action TEXT NOT NULL,
                projection_id TEXT NOT NULL DEFAULT '',
                projection_sha256 TEXT NOT NULL DEFAULT '',
                projection_json TEXT NOT NULL DEFAULT '',
                evidence_bindings_json TEXT NOT NULL DEFAULT '[]',
                supersedes_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (
                    sor_document_id, source_sha256, structure_snapshot_id,
                    logical_table_id, revision
                )
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                trg_table_projection_publication_currentness
            AFTER INSERT ON table_projection_revisions
            FOR EACH ROW
            BEGIN
                UPDATE sor_documents
                SET publication_currentness_generation =
                        publication_currentness_generation + 1
                WHERE id = NEW.sor_document_id;
            END
            """
        )
        _ensure_column(conn, "requirement_sets", "lock_waiver_reason", "TEXT DEFAULT ''")
        _ensure_column(conn, "requirement_sets", "preview_status_at_lock", "TEXT DEFAULT ''")
        _ensure_column(
            conn,
            "requirement_sets",
            "requirement_tree_object_key",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "requirement_tree_sha256",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "requirement_tree_schema_version",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "structure_snapshot_id",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "structure_tree_sha256",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "structure_revision",
            "INTEGER DEFAULT 0",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "publication_key",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "publication_state",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "publication_owner_token",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "publication_lease_expires_at",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "publication_heartbeat_at",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "entry_classification_snapshot_id",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "entry_classification_snapshot_sha256",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "enrichment_publication_binding_id",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "requirement_sets",
            "enrichment_publication_binding_sha256",
            "TEXT DEFAULT ''",
        )
        _ensure_column(
            conn,
            "entry_classification_snapshots",
            "envelope_sha256",
            "TEXT DEFAULT ''",
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_requirement_sets_document_publication_key
            ON requirement_sets(sor_document_id, publication_key)
            WHERE publication_key <> ''
            """
        )
        _ensure_column(conn, "export_jobs", "version_label", "TEXT")
        _ensure_column(conn, "export_jobs", "compare_export_id", "TEXT")
        _ensure_column(conn, "export_jobs", "snapshot_json", "TEXT")
        _ensure_column(conn, "export_jobs", "sor_document_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "export_jobs", "requirement_set_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "export_jobs", "export_mode", "TEXT DEFAULT 'DRAFT'")
        _ensure_column(conn, "export_jobs", "source_hash", "TEXT DEFAULT ''")
        _ensure_column(conn, "export_jobs", "knowledge_base_version", "INTEGER DEFAULT 0")
        _ensure_column(conn, "export_jobs", "ai_run_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "export_jobs", "human_coverage", "REAL DEFAULT 0")
        _ensure_column(
            conn,
            "batch_decision_operations",
            "forked_from_requirement_set_id",
            "TEXT DEFAULT ''",
        )
        _ensure_column(conn, "assessments", "assessment_mode", "TEXT DEFAULT 'heuristic'")
        _ensure_column(conn, "assessments", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "provider", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "retrieval_backend", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "compliance_status", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "analysis_label", "TEXT DEFAULT 'REQUIREMENT'")
        _ensure_column(conn, "assessments", "analysis_basis", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "normalized_requirement", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "risk_level", "TEXT DEFAULT 'HIGH'")
        _ensure_column(conn, "assessments", "clarification_question", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "evidence_findings_json", "TEXT DEFAULT '[]'")
        _ensure_column(
            conn,
            "assessments",
            "structured_information_gap_json",
            "TEXT DEFAULT '{}'",
        )
        _ensure_column(conn, "assessments", "sub_judgments_json", "TEXT DEFAULT '[]'")
        _ensure_column(conn, "assessments", "reference_status", "TEXT DEFAULT 'CURRENT'")
        _ensure_column(conn, "assessments", "source_requirement_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "result_origin", "TEXT DEFAULT 'NATIVE'")
        _ensure_column(conn, "assessments", "source_assessment_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessments", "impact_receipt_id", "TEXT DEFAULT ''")
        _ensure_column(
            conn,
            "assessments",
            "analysis_input_schema_version",
            "TEXT DEFAULT ''",
        )
        _ensure_column(conn, "assessments", "analysis_input_sha256", "TEXT DEFAULT ''")
        _ensure_column(conn, "evidences", "source_doc_version", "TEXT DEFAULT ''")
        _ensure_column(conn, "evidences", "retrieval_evidence_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "evidences", "content_hash", "TEXT DEFAULT ''")
        _ensure_column(
            conn,
            "evidences",
            "evidence_contract_version",
            "TEXT DEFAULT ''",
        )
        _ensure_column(conn, "evidences", "content_kind", "TEXT DEFAULT ''")
        _ensure_column(conn, "evidences", "evidence_metadata_json", "TEXT DEFAULT '{}'")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_evidences_requirement_retrieval_id
            ON evidences(requirement_id, retrieval_evidence_id)
            WHERE retrieval_evidence_id <> ''
            """
        )
        _ensure_column(conn, "assessment_runs", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "assessment_runs", "provider", "TEXT DEFAULT ''")
        _ensure_column(
            conn,
            "assessment_runs",
            "analysis_node_attempt_id",
            "TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            "assessment_runs",
            "run_index",
            "INTEGER NOT NULL DEFAULT -1",
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_runs_node_attempt_index
            ON assessment_runs(analysis_node_attempt_id, run_index)
            WHERE analysis_node_attempt_id <> ''
            """
        )
        _ensure_column(conn, "ai_analysis_jobs", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "provider", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "assessment_mode", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "retrieval_backend", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "chat_model", "TEXT DEFAULT ''")
        _ensure_column(conn, "document_table_assets", "preview_generation_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "embedding_model", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "requirement_set_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "requirement_set_version", "INTEGER DEFAULT 0")
        _ensure_column(conn, "ai_analysis_jobs", "requirement_tree_sha256", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "knowledge_base_version", "INTEGER DEFAULT 0")
        _ensure_column(conn, "ai_analysis_jobs", "command_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "command_sha256", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "scope_schema_version", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "scope_kind", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_jobs", "scope_payload_json", "TEXT DEFAULT '{}'")
        _ensure_column(conn, "ai_analysis_jobs", "scope_sha256", "TEXT DEFAULT ''")
        _ensure_column(
            conn,
            "ai_analysis_jobs",
            "publication_mode",
            "TEXT DEFAULT 'ATOMIC_BATCH'",
        )
        _ensure_column(conn, "ai_analysis_jobs", "total_requirements", "INTEGER DEFAULT 0")
        _ensure_column(conn, "ai_analysis_jobs", "published_requirements", "INTEGER DEFAULT 0")
        _ensure_column(conn, "ai_analysis_events", "profile_id", "TEXT DEFAULT ''")
        _ensure_column(conn, "ai_analysis_events", "provider", "TEXT DEFAULT ''")
        _ensure_requirement_sets_schema(conn)
        _ensure_ai_analysis_job_scope_schema(
            conn,
            backfill_set_version=backfill_ai_job_set_version,
            backfill_tree_sha256=backfill_ai_job_tree_sha256,
            backfill_published_count=backfill_ai_job_published_count,
        )
        _backfill_export_job_lineage(conn)
        _backfill_requirement_set_table_assets(conn)
        _ensure_ai_analysis_job_lineage_schema(
            conn,
            backfill_knowledge_snapshot=backfill_ai_job_knowledge_snapshot,
        )
        _cleanup_orphan_records(conn)


def _register_stored_object_in_conn(
    conn: Any,
    *,
    storage_key: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    object_kind: str,
) -> None:
    normalized_key = str(storage_key or "").strip()
    normalized_sha256 = str(sha256 or "").strip().lower()
    if not normalized_key or Path(normalized_key).is_absolute():
        raise ValueError("PostgreSQL records require a relative storage_key.")
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
        raise ValueError("Stored objects require a lowercase SHA-256 identity.")
    normalized_size = int(size_bytes)
    if normalized_size < 0:
        raise ValueError("Stored object size must not be negative.")
    conn.execute(
        """
        INSERT INTO stored_objects (
            storage_key, sha256, size_bytes, media_type, object_kind
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (storage_key) DO NOTHING
        """,
        (
            normalized_key,
            normalized_sha256,
            normalized_size,
            str(media_type or "application/octet-stream"),
            str(object_kind or "object"),
        ),
    )
    row = conn.execute(
        """
        SELECT sha256, size_bytes
        FROM stored_objects
        WHERE storage_key = ?
        """,
        (normalized_key,),
    ).fetchone()
    if (
        row is None
        or str(row["sha256"]) != normalized_sha256
        or int(row["size_bytes"]) != normalized_size
    ):
        raise ValueError("storage_key is already bound to different object bytes.")


def register_stored_object(
    *,
    storage_key: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    object_kind: str,
) -> None:
    if not is_postgresql_runtime():
        raise RuntimeError("Stored object metadata requires PostgreSQL.")
    with get_conn() as conn:
        _register_stored_object_in_conn(
            conn,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            object_kind=object_kind,
        )


def _source_asset_object_advisory_lock(conn: Any, storage_key: str) -> None:
    """Serialize publication and deletion for one content-addressed object."""

    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(?, 90422))",
        (storage_key,),
    )


def _validate_source_asset_attempt_in_conn(
    conn: Any,
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    require_cancellation_closed: bool = False,
    allow_cancel_requested: bool = False,
) -> None:
    normalized_sha256 = str(source_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
        raise ValueError("Source asset ownership requires a lowercase SHA-256.")
    if int(attempt_generation) < 1:
        raise ValueError("Source asset attempt_generation must be positive.")
    allowed_statuses = (
        ("RUNNING", "CANCEL_REQUESTED")
        if allow_cancel_requested
        else ("RUNNING",)
    )
    row = conn.execute(
        """
        SELECT 1 AS present
        FROM runtime_jobs AS job
        INNER JOIN sor_documents AS document
            ON document.id = job.aggregate_id
        WHERE job.id = ?
          AND job.kind = 'document.extract'
          AND job.aggregate_type = 'sor-document'
          AND job.aggregate_id = ?
          AND job.generation = ?
          AND job.lease_expires_at >= now()
          AND job.status = ANY(?::text[])
          AND (? = FALSE OR COALESCE(job.progress->>'non_cancellable', 'false') = 'true')
          AND document.project_id = ?
          AND document.source_sha256 = ?
        """,
        (
            str(job_id or "").strip(),
            str(document_id or "").strip(),
            int(attempt_generation),
            list(allowed_statuses),
            bool(require_cancellation_closed),
            str(project_id or "").strip(),
            normalized_sha256,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("Source asset publication lost its job lease fence.")


def register_source_asset_staging_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
    storage_key: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
) -> None:
    """Register metadata and one fenced attempt reference before object I/O."""

    if not is_postgresql_runtime():
        raise RuntimeError("Source asset references require PostgreSQL.")
    normalized_key = str(storage_key or "").strip()
    with get_conn() as conn:
        _source_asset_object_advisory_lock(conn, normalized_key)
        _validate_source_asset_attempt_in_conn(
            conn,
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
            attempt_generation=attempt_generation,
        )
        metadata_existed = (
            conn.execute(
                "SELECT 1 AS present FROM stored_objects WHERE storage_key = ?",
                (normalized_key,),
            ).fetchone()
            is not None
        )
        managed_existed = (
            conn.execute(
                """
                SELECT 1 AS present
                FROM source_asset_managed_objects
                WHERE storage_key = ?
                """,
                (normalized_key,),
            ).fetchone()
            is not None
        )
        _register_stored_object_in_conn(
            conn,
            storage_key=normalized_key,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            object_kind="source-ir-asset",
        )
        # Only durable metadata observed under the advisory fence can prove a
        # pre-lifecycle object.  A lock-before-stat result is intentionally not
        # accepted: GC may have removed that object before this transaction.
        if not managed_existed:
            legacy_protected = bool(metadata_existed)
            conn.execute(
                """
                INSERT INTO source_asset_managed_objects (
                    storage_key, legacy_protected
                )
                VALUES (?, ?)
                ON CONFLICT (storage_key) DO NOTHING
                """,
                (normalized_key, legacy_protected),
            )
        conn.execute(
            """
            INSERT INTO source_asset_staging_references (
                job_id, attempt_generation, project_id, document_id,
                source_sha256, storage_key
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, attempt_generation, storage_key) DO NOTHING
            """,
            (
                str(job_id or "").strip(),
                int(attempt_generation),
                str(project_id or "").strip(),
                str(document_id or "").strip(),
                str(source_sha256 or "").strip().lower(),
                normalized_key,
            ),
        )


def confirm_source_asset_staging_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    storage_key: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
) -> bool:
    """Confirm ownership after object I/O, repairing a recoverable GC record."""

    if not is_postgresql_runtime():
        raise RuntimeError("Source asset references require PostgreSQL.")
    normalized_key = str(storage_key or "").strip()
    with get_conn() as conn:
        _source_asset_object_advisory_lock(conn, normalized_key)
        staging = conn.execute(
            """
            SELECT 1 AS present
            FROM source_asset_staging_references
            WHERE job_id = ?
              AND attempt_generation = ?
              AND project_id = ?
              AND document_id = ?
              AND source_sha256 = ?
              AND storage_key = ?
            """,
            (
                job_id,
                int(attempt_generation),
                project_id,
                document_id,
                source_sha256,
                normalized_key,
            ),
        ).fetchone()
        lease_valid = True
        try:
            _validate_source_asset_attempt_in_conn(
                conn,
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                job_id=job_id,
                attempt_generation=attempt_generation,
            )
        except RuntimeError:
            lease_valid = False
        if staging is not None and lease_valid:
            return True
        # A stale-attempt reaper may have removed metadata after the publisher
        # registered its staging row but before object-store I/O completed.
        # Restore a bounded managed record so zero-ref GC can recover the bytes.
        metadata_existed = (
            conn.execute(
                "SELECT 1 AS present FROM stored_objects WHERE storage_key = ?",
                (normalized_key,),
            ).fetchone()
            is not None
        )
        _register_stored_object_in_conn(
            conn,
            storage_key=normalized_key,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            object_kind="source-ir-asset",
        )
        conn.execute(
            """
            INSERT INTO source_asset_managed_objects (
                storage_key, legacy_protected
            )
            VALUES (?, ?)
            ON CONFLICT (storage_key) DO NOTHING
            """,
            (normalized_key, bool(metadata_existed)),
        )
        return False


def seal_source_asset_publication_attempt(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
) -> None:
    """Validate that one attempt is ready for queue-atomic publication."""

    if not is_postgresql_runtime():
        return
    with get_conn() as conn:
        # Commit is a business publication boundary. Serialize it against the
        # next lease claim, then require the exact generation and current
        # Context-owned worker before replacing the document's committed set.
        job_row = conn.execute(
            "SELECT lease_owner FROM runtime_jobs WHERE id = ? FOR UPDATE",
            (job_id,),
        ).fetchone()
        if job_row is None or str(job_row["lease_owner"] or "") != str(
            worker_id or ""
        ).strip():
            raise RuntimeError("Source asset publication lost its job lease fence.")
        _validate_source_asset_attempt_in_conn(
            conn,
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
            attempt_generation=attempt_generation,
            require_cancellation_closed=True,
        )
        sealed = conn.execute(
            """
            UPDATE runtime_jobs
            SET progress = progress || jsonb_build_object(
                    'source_asset_publication_generation', ?
                ),
                updated_at = now()
            WHERE id = ?
              AND generation = ?
              AND lease_owner = ?
              AND status = 'RUNNING'
              AND lease_expires_at >= now()
              AND COALESCE(progress->>'non_cancellable', 'false') = 'true'
            """,
            (
                int(attempt_generation),
                job_id,
                int(attempt_generation),
                worker_id,
            ),
        )
        if sealed.rowcount != 1:
            raise RuntimeError("Source asset publication lost its job lease fence.")


def abort_source_asset_publication_attempt(
    *,
    job_id: str,
    attempt_generation: int,
) -> tuple[str, ...]:
    """Discard only the failed delivery's staging refs."""

    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        rows = conn.execute(
            """
            DELETE FROM source_asset_staging_references
            WHERE job_id = ? AND attempt_generation = ?
            RETURNING storage_key
            """,
            (str(job_id or "").strip(), int(attempt_generation)),
        ).fetchall()
    return tuple(sorted({str(row["storage_key"]) for row in rows}))


def reap_stale_source_asset_staging_references() -> tuple[str, ...]:
    """Release refs whose durable delivery can no longer publish."""

    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        rows = conn.execute(
            """
            DELETE FROM source_asset_staging_references AS staging
            USING runtime_jobs AS job
            WHERE staging.job_id = job.id
              AND (
                  job.generation <> staging.attempt_generation
                  OR job.status NOT IN ('RUNNING', 'CANCEL_REQUESTED')
                  OR job.lease_expires_at IS NULL
                  OR job.lease_expires_at < now()
              )
            RETURNING staging.storage_key
            """
        ).fetchall()
    return tuple(sorted({str(row["storage_key"]) for row in rows}))


def stage_document_extraction_page_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
    processing_plan_sha256: str,
    result_runtime_sha256: str,
    adapter_id: str,
    adapter_version: str,
    page_number: int,
    total_pages: int,
    completed_at_utc: str,
    storage_key: str,
    artifact_sha256: str,
    size_bytes: int,
    media_type: str,
) -> None:
    """Own one staged read-only page before object-store I/O."""

    if not is_postgresql_runtime():
        raise RuntimeError("Extraction page staging requires PostgreSQL.")
    normalized_key = str(storage_key or "").strip()
    normalized_artifact_sha256 = str(artifact_sha256 or "").strip().lower()
    normalized_plan_sha256 = str(processing_plan_sha256 or "").strip().lower()
    normalized_runtime_sha256 = str(result_runtime_sha256 or "").strip().lower()
    if (
        not re.fullmatch(r"[0-9a-f]{64}", normalized_artifact_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", normalized_plan_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", normalized_runtime_sha256)
        or int(page_number) < 1
        or int(total_pages) < int(page_number)
        or int(size_bytes) < 1
        or not str(adapter_id or "").strip()
        or not str(adapter_version or "").strip()
    ):
        raise ValueError("Extraction page staging metadata is invalid.")
    with get_conn() as conn:
        job_row = conn.execute(
            "SELECT lease_owner, payload FROM runtime_jobs WHERE id = ? FOR UPDATE",
            (job_id,),
        ).fetchone()
        if job_row is None or str(job_row["lease_owner"] or "") != str(
            worker_id or ""
        ).strip():
            raise DocumentExtractionPageStagingFenceError(
                "Extraction page staging lost its job lease fence."
            )
        try:
            _validate_source_asset_attempt_in_conn(
                conn,
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                job_id=job_id,
                attempt_generation=attempt_generation,
            )
        except RuntimeError as exc:
            raise DocumentExtractionPageStagingFenceError(
                "Extraction page staging lost its exact source/generation fence."
            ) from exc
        job_payload = _decode_json_payload(job_row["payload"], default={})
        job_plan = _decode_json_payload(job_payload.get("processing_plan"), default={})
        if (
            str(job_plan.get("canonical_sha256") or "")
            != normalized_plan_sha256
            or str(job_payload.get("result_runtime_sha256") or "")
            != normalized_runtime_sha256
        ):
            raise DocumentExtractionPageStagingFenceError(
                "Extraction page staging plan binding changed."
            )
        _source_asset_object_advisory_lock(conn, normalized_key)
        metadata_existed = (
            conn.execute(
                "SELECT 1 AS present FROM stored_objects WHERE storage_key = ?",
                (normalized_key,),
            ).fetchone()
            is not None
        )
        managed_existed = (
            conn.execute(
                "SELECT 1 AS present FROM source_asset_managed_objects WHERE storage_key = ?",
                (normalized_key,),
            ).fetchone()
            is not None
        )
        _register_stored_object_in_conn(
            conn,
            storage_key=normalized_key,
            sha256=normalized_artifact_sha256,
            size_bytes=int(size_bytes),
            media_type=media_type,
            object_kind="document-extraction-page-staging",
        )
        if not managed_existed:
            conn.execute(
                """
                INSERT INTO source_asset_managed_objects (
                    storage_key, legacy_protected
                ) VALUES (?, ?)
                ON CONFLICT (storage_key) DO NOTHING
                """,
                (normalized_key, bool(metadata_existed)),
            )
        conn.execute(
            """
            INSERT INTO document_extraction_page_staging_references (
                job_id, attempt_generation, project_id, document_id,
                source_sha256, processing_plan_sha256,
                result_runtime_sha256, adapter_id, adapter_version,
                page_number, total_pages, completed_at_utc, storage_key,
                artifact_sha256, size_bytes, media_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, attempt_generation, page_number) DO NOTHING
            """,
            (
                job_id,
                int(attempt_generation),
                project_id,
                document_id,
                source_sha256,
                normalized_plan_sha256,
                normalized_runtime_sha256,
                str(adapter_id),
                str(adapter_version),
                int(page_number),
                int(total_pages),
                completed_at_utc,
                normalized_key,
                normalized_artifact_sha256,
                int(size_bytes),
                str(media_type or "application/json"),
            ),
        )
        staged = conn.execute(
            """
            SELECT storage_key, artifact_sha256, size_bytes,
                   processing_plan_sha256, result_runtime_sha256
            FROM document_extraction_page_staging_references
            WHERE job_id = ? AND attempt_generation = ? AND page_number = ?
            """,
            (job_id, int(attempt_generation), int(page_number)),
        ).fetchone()
        if (
            staged is None
            or str(staged["storage_key"]) != normalized_key
            or str(staged["artifact_sha256"]) != normalized_artifact_sha256
            or int(staged["size_bytes"]) != int(size_bytes)
            or str(staged["processing_plan_sha256"]) != normalized_plan_sha256
            or str(staged["result_runtime_sha256"])
            != normalized_runtime_sha256
        ):
            raise RuntimeError("Extraction page staging is not immutable.")


def confirm_document_extraction_page_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
    processing_plan_sha256: str,
    result_runtime_sha256: str,
    page_number: int,
    storage_key: str,
    artifact_sha256: str,
    size_bytes: int,
) -> bool:
    """Confirm the object only while the exact generation still owns lease."""

    if not is_postgresql_runtime():
        raise RuntimeError("Extraction page staging requires PostgreSQL.")
    with get_conn() as conn:
        job_row = conn.execute(
            "SELECT lease_owner, payload FROM runtime_jobs WHERE id = ? FOR UPDATE",
            (job_id,),
        ).fetchone()
        _source_asset_object_advisory_lock(conn, storage_key)
        if job_row is None or str(job_row["lease_owner"] or "") != str(
            worker_id or ""
        ).strip():
            raise DocumentExtractionPageStagingFenceError(
                "Extraction page staging lost its job lease fence."
            )
        try:
            _validate_source_asset_attempt_in_conn(
                conn,
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                job_id=job_id,
                attempt_generation=attempt_generation,
            )
        except RuntimeError as exc:
            raise DocumentExtractionPageStagingFenceError(
                "Extraction page staging lost its exact source/generation fence."
            ) from exc
        job_payload = _decode_json_payload(job_row["payload"], default={})
        job_plan = _decode_json_payload(job_payload.get("processing_plan"), default={})
        if (
            str(job_plan.get("canonical_sha256") or "")
            != str(processing_plan_sha256 or "")
            or str(job_payload.get("result_runtime_sha256") or "")
            != str(result_runtime_sha256 or "")
        ):
            raise DocumentExtractionPageStagingFenceError(
                "Extraction page staging plan binding changed."
            )
        updated = conn.execute(
            """
            UPDATE document_extraction_page_staging_references
            SET confirmed_at = now()
            WHERE job_id = ? AND attempt_generation = ? AND page_number = ?
              AND project_id = ? AND document_id = ? AND source_sha256 = ?
              AND processing_plan_sha256 = ? AND result_runtime_sha256 = ?
              AND storage_key = ? AND artifact_sha256 = ? AND size_bytes = ?
            """,
            (
                job_id,
                int(attempt_generation),
                int(page_number),
                project_id,
                document_id,
                source_sha256,
                processing_plan_sha256,
                result_runtime_sha256,
                storage_key,
                artifact_sha256,
                int(size_bytes),
            ),
        )
        return updated.rowcount == 1


def abort_document_extraction_page_reference(
    *,
    job_id: str,
    attempt_generation: int,
    page_number: int | None = None,
) -> tuple[str, ...]:
    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        if page_number is None:
            rows = conn.execute(
                """
                DELETE FROM document_extraction_page_staging_references
                WHERE job_id = ? AND attempt_generation = ?
                RETURNING storage_key
                """,
                (job_id, int(attempt_generation)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                DELETE FROM document_extraction_page_staging_references
                WHERE job_id = ? AND attempt_generation = ? AND page_number = ?
                RETURNING storage_key
                """,
                (job_id, int(attempt_generation), int(page_number)),
            ).fetchall()
    return tuple(sorted({str(row["storage_key"]) for row in rows}))


def reap_stale_document_extraction_page_references() -> tuple[str, ...]:
    """Hide and release pages from terminal, retried, or expired attempts."""

    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        rows = conn.execute(
            """
            DELETE FROM document_extraction_page_staging_references AS staging
            USING runtime_jobs AS job
            WHERE staging.job_id = job.id
              AND (
                  job.generation <> staging.attempt_generation
                  OR job.status <> 'RUNNING'
                  OR job.lease_expires_at IS NULL
                  OR job.lease_expires_at < now()
              )
            RETURNING staging.storage_key
            """
        ).fetchall()
    return tuple(sorted({str(row["storage_key"]) for row in rows}))


def get_document_extraction_page_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    page_number: int,
) -> dict[str, Any] | None:
    if not is_postgresql_runtime():
        return None
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT staging.*
            FROM document_extraction_page_staging_references AS staging
            INNER JOIN runtime_jobs AS job ON job.id = staging.job_id
            INNER JOIN sor_documents AS document ON document.id = staging.document_id
            WHERE staging.project_id = ? AND staging.document_id = ?
              AND staging.source_sha256 = ? AND staging.job_id = ?
              AND staging.attempt_generation = ? AND staging.page_number = ?
              AND staging.confirmed_at IS NOT NULL
              AND job.kind = 'document.extract'
              AND job.aggregate_type = 'sor-document'
              AND job.aggregate_id = staging.document_id
              AND job.generation = staging.attempt_generation
              AND job.status = 'RUNNING'
              AND job.lease_expires_at >= now()
              AND document.project_id = staging.project_id
              AND document.source_sha256 = staging.source_sha256
            """,
            (
                project_id,
                document_id,
                source_sha256,
                job_id,
                int(attempt_generation),
                int(page_number),
            ),
        ).fetchone()
    return dict(row) if row is not None else None


def get_document_extraction_page_staging_summary(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
) -> dict[str, Any] | None:
    if not is_postgresql_runtime() or not str(job_id or "").strip():
        return None
    with get_conn() as conn:
        active = conn.execute(
            """
            SELECT job.generation, job.payload, job.progress
            FROM runtime_jobs AS job
            INNER JOIN sor_documents AS document ON document.id = job.aggregate_id
            WHERE job.id = ? AND job.kind = 'document.extract'
              AND job.aggregate_type = 'sor-document'
              AND job.aggregate_id = ? AND job.status = 'RUNNING'
              AND job.lease_expires_at >= now()
              AND document.project_id = ? AND document.source_sha256 = ?
            """,
            (job_id, document_id, project_id, source_sha256),
        ).fetchone()
        if active is None:
            return None
        attempt_generation = int(active["generation"])
        job_payload = _decode_json_payload(active["payload"], default={})
        job_plan = _decode_json_payload(job_payload.get("processing_plan"), default={})
        progress = _decode_json_payload(active["progress"], default={})
        processing_plan_sha256 = str(job_plan.get("canonical_sha256") or "")
        result_runtime_sha256 = str(
            job_payload.get("result_runtime_sha256") or ""
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", processing_plan_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", result_runtime_sha256)
        ):
            return None
        rows = conn.execute(
            """
            SELECT staging.attempt_generation, staging.processing_plan_sha256,
                   staging.result_runtime_sha256, staging.total_pages,
                   staging.page_number
            FROM document_extraction_page_staging_references AS staging
            INNER JOIN runtime_jobs AS job ON job.id = staging.job_id
            INNER JOIN sor_documents AS document ON document.id = staging.document_id
            WHERE staging.project_id = ? AND staging.document_id = ?
              AND staging.source_sha256 = ? AND staging.job_id = ?
              AND staging.attempt_generation = ?
              AND staging.confirmed_at IS NOT NULL
              AND job.kind = 'document.extract'
              AND job.aggregate_type = 'sor-document'
              AND job.aggregate_id = staging.document_id
              AND job.generation = staging.attempt_generation
              AND job.status = 'RUNNING'
              AND job.lease_expires_at >= now()
              AND document.project_id = staging.project_id
              AND document.source_sha256 = staging.source_sha256
            ORDER BY staging.page_number
            """,
            (
                project_id,
                document_id,
                source_sha256,
                job_id,
                attempt_generation,
            ),
        ).fetchall()
    total_pages = max(
        [int(row["total_pages"]) for row in rows]
        + [int(progress.get("total_pages") or 0)]
    )
    return {
        "attempt_generation": attempt_generation,
        "processing_plan_sha256": processing_plan_sha256,
        "result_runtime_sha256": result_runtime_sha256,
        "total_pages": total_pages,
        "ready_page_count": len(rows),
        "page_numbers": [int(row["page_number"]) for row in rows],
    }


def list_unreferenced_managed_source_asset_objects(
    *,
    limit: int = 256,
) -> tuple[str, ...]:
    """List bounded GC candidates; deletion performs a locked recheck."""

    if not is_postgresql_runtime():
        return ()
    bounded_limit = int(limit)
    if not 1 <= bounded_limit <= 10_000:
        raise ValueError("Source asset GC limit must be between 1 and 10000.")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT managed.storage_key
            FROM source_asset_managed_objects AS managed
            WHERE managed.legacy_protected = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM source_asset_references AS committed
                  WHERE committed.storage_key = managed.storage_key
              )
              AND NOT EXISTS (
                  SELECT 1 FROM source_asset_staging_references AS staging
                  WHERE staging.storage_key = managed.storage_key
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM document_extraction_page_staging_references AS page_staging
                  WHERE page_staging.storage_key = managed.storage_key
              )
              AND NOT EXISTS (
                  SELECT 1 FROM source_capture_asset_references AS committed
                  WHERE committed.storage_key = managed.storage_key
              )
              AND NOT EXISTS (
                  SELECT 1 FROM source_capture_asset_staging_references AS staging
                  WHERE staging.storage_key = managed.storage_key
              )
            ORDER BY managed.created_at, managed.storage_key
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
    return tuple(str(row["storage_key"]) for row in rows)


def stage_extraction_result_artifact_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
    storage_key: str,
    artifact_sha256: str,
    size_bytes: int,
    media_type: str,
) -> None:
    """Create managed inventory and bind one live delivery before object I/O."""

    if not is_postgresql_runtime():
        raise RuntimeError("Extraction result artifact refs require PostgreSQL.")
    normalized_key = str(storage_key or "").strip()
    normalized_digest = str(artifact_sha256 or "").strip().lower()
    with get_conn() as conn:
        owner = conn.execute(
            "SELECT lease_owner FROM runtime_jobs WHERE id = ? FOR UPDATE",
            (job_id,),
        ).fetchone()
        if owner is None or str(owner["lease_owner"] or "") != str(
            worker_id or ""
        ).strip():
            raise RuntimeError(
                "Extraction result artifact publication lost its job lease fence."
            )
        _validate_source_asset_attempt_in_conn(
            conn,
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
            attempt_generation=attempt_generation,
            require_cancellation_closed=True,
        )
        _source_asset_object_advisory_lock(conn, normalized_key)
        _register_extraction_result_artifact_managed_in_conn(
            conn,
            storage_key=normalized_key,
            artifact_sha256=normalized_digest,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        conn.execute(
            """
            INSERT INTO extraction_result_artifact_staging_references (
                job_id, attempt_generation, project_id, document_id,
                source_sha256, storage_key, artifact_sha256, size_bytes,
                media_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, attempt_generation) DO UPDATE
            SET storage_key = EXCLUDED.storage_key,
                artifact_sha256 = EXCLUDED.artifact_sha256,
                size_bytes = EXCLUDED.size_bytes,
                media_type = EXCLUDED.media_type
            """,
            (
                job_id,
                int(attempt_generation),
                project_id,
                document_id,
                source_sha256,
                normalized_key,
                normalized_digest,
                int(size_bytes),
                str(media_type or "application/json"),
            ),
        )


def confirm_extraction_result_artifact_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
    storage_key: str,
    artifact_sha256: str,
    size_bytes: int,
    media_type: str,
) -> bool:
    """Confirm post-put ownership or restore retryable zero-ref inventory."""

    if not is_postgresql_runtime():
        raise RuntimeError("Extraction result artifact refs require PostgreSQL.")
    normalized_key = str(storage_key or "").strip()
    with get_conn() as conn:
        owner = conn.execute(
            "SELECT lease_owner FROM runtime_jobs WHERE id = ? FOR UPDATE",
            (job_id,),
        ).fetchone()
        _source_asset_object_advisory_lock(conn, normalized_key)
        _register_extraction_result_artifact_managed_in_conn(
            conn,
            storage_key=normalized_key,
            artifact_sha256=artifact_sha256,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        staging = conn.execute(
            """
            SELECT 1 AS present
            FROM extraction_result_artifact_staging_references
            WHERE job_id = ? AND attempt_generation = ?
              AND project_id = ? AND document_id = ? AND source_sha256 = ?
              AND storage_key = ? AND artifact_sha256 = ? AND size_bytes = ?
            """,
            (
                job_id,
                int(attempt_generation),
                project_id,
                document_id,
                source_sha256,
                normalized_key,
                str(artifact_sha256 or "").strip().lower(),
                int(size_bytes),
            ),
        ).fetchone()
        if owner is None or str(owner["lease_owner"] or "") != str(
            worker_id or ""
        ).strip():
            return False
        try:
            _validate_source_asset_attempt_in_conn(
                conn,
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                job_id=job_id,
                attempt_generation=attempt_generation,
                require_cancellation_closed=True,
            )
        except RuntimeError:
            return False
        return staging is not None


def abort_extraction_result_artifact_reference(
    *,
    job_id: str,
    attempt_generation: int,
) -> tuple[str, ...]:
    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        rows = conn.execute(
            """
            DELETE FROM extraction_result_artifact_staging_references
            WHERE job_id = ? AND attempt_generation = ?
            RETURNING storage_key
            """,
            (job_id, int(attempt_generation)),
        ).fetchall()
    return tuple(sorted({str(row["storage_key"]) for row in rows}))


def reap_stale_extraction_result_artifact_references() -> tuple[str, ...]:
    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        rows = conn.execute(
            """
            DELETE FROM extraction_result_artifact_staging_references AS staging
            USING runtime_jobs AS job
            WHERE staging.job_id = job.id
              AND (
                  job.generation <> staging.attempt_generation
                  OR job.status NOT IN ('RUNNING', 'CANCEL_REQUESTED')
                  OR job.lease_expires_at IS NULL
                  OR job.lease_expires_at < now()
              )
            RETURNING staging.storage_key
            """
        ).fetchall()
    return tuple(sorted({str(row["storage_key"]) for row in rows}))


def stage_extraction_result_artifact_cas_operation(
    *,
    operation_id: str,
    job_id: str,
    expected_result: dict[str, Any],
    project_id: str,
    document_id: str,
    source_sha256: str,
    storage_key: str,
    artifact_sha256: str,
    size_bytes: int,
    media_type: str,
) -> None:
    """Durably own a Structure-workspace artifact before its object-store put."""

    if not is_postgresql_runtime():
        raise RuntimeError("Extraction result artifact refs require PostgreSQL.")
    normalized_operation_id = str(operation_id or "").strip()
    normalized_key = str(storage_key or "").strip()
    expected_key = str(expected_result.get("storage_key") or "").strip()
    if not normalized_operation_id or not expected_key:
        raise ValueError("Artifact CAS ownership requires stable operation keys.")
    expected_json = json.dumps(
        expected_result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with get_conn() as conn:
        job = conn.execute(
            """
            SELECT payload
            FROM runtime_jobs
            WHERE id = ? AND status = 'SUCCEEDED' AND result = ?::jsonb
            FOR UPDATE
            """,
            (job_id, expected_json),
        ).fetchone()
        if job is None:
            raise RuntimeError("Extraction result artifact CAS source changed.")
        payload = _decode_json_payload(job["payload"], default={})
        if (
            str(payload.get("project_id") or "") != project_id
            or str(payload.get("document_id") or "") != document_id
            or str(payload.get("source_sha256") or "").lower()
            != str(source_sha256 or "").lower()
        ):
            raise RuntimeError("Extraction result artifact CAS identity changed.")
        _source_asset_object_advisory_lock(conn, normalized_key)
        _register_extraction_result_artifact_managed_in_conn(
            conn,
            storage_key=normalized_key,
            artifact_sha256=artifact_sha256,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        conn.execute(
            """
            INSERT INTO extraction_result_artifact_cas_operations (
                operation_id, job_id, project_id, document_id, source_sha256,
                expected_storage_key, new_storage_key, new_artifact_sha256,
                new_size_bytes, media_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_operation_id,
                job_id,
                project_id,
                document_id,
                str(source_sha256 or "").lower(),
                expected_key,
                normalized_key,
                str(artifact_sha256 or "").lower(),
                int(size_bytes),
                str(media_type or "application/json"),
            ),
        )


def confirm_extraction_result_artifact_cas_operation(
    *,
    operation_id: str,
    storage_key: str,
    artifact_sha256: str,
    size_bytes: int,
    media_type: str,
) -> bool:
    """Post-put fence for a CAS operation, with orphan inventory repair."""

    if not is_postgresql_runtime():
        raise RuntimeError("Extraction result artifact refs require PostgreSQL.")
    normalized_key = str(storage_key or "").strip()
    with get_conn() as conn:
        _source_asset_object_advisory_lock(conn, normalized_key)
        _register_extraction_result_artifact_managed_in_conn(
            conn,
            storage_key=normalized_key,
            artifact_sha256=artifact_sha256,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        operation = conn.execute(
            """
            SELECT 1 AS present
            FROM extraction_result_artifact_cas_operations
            WHERE operation_id = ? AND new_storage_key = ?
              AND new_artifact_sha256 = ? AND new_size_bytes = ?
              AND expires_at >= now()
            """,
            (
                operation_id,
                normalized_key,
                str(artifact_sha256 or "").lower(),
                int(size_bytes),
            ),
        ).fetchone()
        return operation is not None


def abort_extraction_result_artifact_cas_operation(
    *, operation_id: str
) -> tuple[str, ...]:
    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        rows = conn.execute(
            """
            DELETE FROM extraction_result_artifact_cas_operations
            WHERE operation_id = ?
            RETURNING new_storage_key
            """,
            (str(operation_id or "").strip(),),
        ).fetchall()
    return tuple(sorted({str(row["new_storage_key"]) for row in rows}))


def reap_stale_extraction_result_artifact_cas_operations() -> tuple[str, ...]:
    """Resolve expired CAS owners from the durable job result."""

    if not is_postgresql_runtime():
        return ()
    released: set[str] = set()
    with get_conn() as conn:
        operations = conn.execute(
            """
            SELECT *
            FROM extraction_result_artifact_cas_operations
            WHERE expires_at < now()
            ORDER BY expires_at, operation_id
            FOR UPDATE SKIP LOCKED
            LIMIT 256
            """
        ).fetchall()
        for operation in operations:
            new_key = str(operation["new_storage_key"])
            _source_asset_object_advisory_lock(conn, new_key)
            job = conn.execute(
                "SELECT result FROM runtime_jobs WHERE id = ? FOR UPDATE",
                (str(operation["job_id"]),),
            ).fetchone()
            result = (
                _decode_json_payload(job["result"], default={})
                if job is not None
                else {}
            )
            if (
                str(result.get("storage_key") or "") == new_key
                and str(result.get("sha256") or "").lower()
                == str(operation["new_artifact_sha256"]).lower()
                and int(result.get("size") or 0)
                == int(operation["new_size_bytes"])
            ):
                conn.execute(
                    """
                    INSERT INTO extraction_result_artifact_references (
                        job_id, project_id, document_id, source_sha256,
                        storage_key, artifact_sha256, size_bytes, media_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (job_id) DO UPDATE
                    SET storage_key = EXCLUDED.storage_key,
                        artifact_sha256 = EXCLUDED.artifact_sha256,
                        size_bytes = EXCLUDED.size_bytes,
                        media_type = EXCLUDED.media_type
                    """,
                    (
                        str(operation["job_id"]),
                        str(operation["project_id"]),
                        str(operation["document_id"]),
                        str(operation["source_sha256"]),
                        new_key,
                        str(operation["new_artifact_sha256"]),
                        int(operation["new_size_bytes"]),
                        str(operation["media_type"]),
                    ),
                )
            conn.execute(
                "DELETE FROM extraction_result_artifact_cas_operations WHERE operation_id = ?",
                (str(operation["operation_id"]),),
            )
            released.add(new_key)
    return tuple(sorted(released))


def _register_extraction_result_artifact_managed_in_conn(
    conn: Any,
    *,
    storage_key: str,
    artifact_sha256: str,
    size_bytes: int,
    media_type: str,
) -> None:
    normalized_key = str(storage_key or "").strip()
    normalized_digest = str(artifact_sha256 or "").strip().lower()
    if not normalized_key or re.fullmatch(r"[0-9a-f]{64}", normalized_digest) is None:
        raise ValueError("Extraction result artifact identity is invalid.")
    if int(size_bytes) < 1:
        raise ValueError("Extraction result artifact size must be positive.")
    existing = conn.execute(
        """
        SELECT artifact_sha256, size_bytes, media_type
        FROM extraction_result_artifact_managed_objects
        WHERE storage_key = ?
        FOR UPDATE
        """,
        (normalized_key,),
    ).fetchone()
    normalized_media_type = str(media_type or "application/json")
    if existing is not None:
        if (
            str(existing["artifact_sha256"]).lower() != normalized_digest
            or int(existing["size_bytes"]) != int(size_bytes)
            or str(existing["media_type"]) != normalized_media_type
        ):
            raise RuntimeError(
                "Extraction result artifact storage key has different metadata."
            )
        return
    conn.execute(
        """
        INSERT INTO extraction_result_artifact_managed_objects (
            storage_key, artifact_sha256, size_bytes, media_type,
            legacy_protected
        )
        VALUES (?, ?, ?, ?, FALSE)
        """,
        (normalized_key, normalized_digest, int(size_bytes), normalized_media_type),
    )


@contextmanager
def claim_unreferenced_extraction_result_artifact_deletion(
    storage_key: str,
) -> Generator[bool, None, None]:
    """Fence zero-ref object deletion and retire inventory only on success."""

    if not is_postgresql_runtime():
        yield False
        return
    normalized_key = str(storage_key or "").strip()
    if not normalized_key:
        yield False
        return
    with get_conn() as conn:
        _source_asset_object_advisory_lock(conn, normalized_key)
        managed = conn.execute(
            """
            SELECT legacy_protected
            FROM extraction_result_artifact_managed_objects
            WHERE storage_key = ?
            FOR UPDATE
            """,
            (normalized_key,),
        ).fetchone()
        referenced = conn.execute(
            """
            SELECT 1 AS present
            FROM (
                SELECT storage_key FROM extraction_result_artifact_references
                UNION ALL
                SELECT storage_key
                FROM extraction_result_artifact_staging_references
                UNION ALL
                SELECT new_storage_key AS storage_key
                FROM extraction_result_artifact_cas_operations
            ) AS refs
            WHERE refs.storage_key = ?
            LIMIT 1
            """,
            (normalized_key,),
        ).fetchone()
        allowed = (
            managed is not None
            and not bool(managed["legacy_protected"])
            and referenced is None
        )
        try:
            yield allowed
        except BaseException:
            raise
        else:
            if allowed:
                conn.execute(
                    """
                    DELETE FROM extraction_result_artifact_managed_objects
                    WHERE storage_key = ?
                    """,
                    (normalized_key,),
                )


def list_unreferenced_extraction_result_artifacts(
    *,
    limit: int = 256,
) -> tuple[str, ...]:
    """List retryable managed zero-ref candidates."""

    bounded_limit = int(limit)
    if not 1 <= bounded_limit <= 10_000:
        raise ValueError("Artifact GC limit must be between 1 and 10000.")
    if not is_postgresql_runtime():
        return ()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT managed.storage_key
            FROM extraction_result_artifact_managed_objects AS managed
            WHERE managed.legacy_protected = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM extraction_result_artifact_references AS refs
                  WHERE refs.storage_key = managed.storage_key
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM extraction_result_artifact_staging_references AS staging
                  WHERE staging.storage_key = managed.storage_key
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM extraction_result_artifact_cas_operations AS operation
                  WHERE operation.new_storage_key = managed.storage_key
              )
            ORDER BY managed.created_at, managed.storage_key
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
    return tuple(str(row["storage_key"]) for row in rows)


@contextmanager
def claim_unreferenced_source_asset_object_deletion(
    storage_key: str,
) -> Generator[bool, None, None]:
    """Fence one global zero-ref check across object and metadata deletion."""

    if not is_postgresql_runtime():
        yield False
        return
    normalized_key = str(storage_key or "").strip()
    if not normalized_key:
        yield False
        return
    with get_conn() as conn:
        _source_asset_object_advisory_lock(conn, normalized_key)
        managed = conn.execute(
            """
            SELECT legacy_protected
            FROM source_asset_managed_objects
            WHERE storage_key = ?
            """,
            (normalized_key,),
        ).fetchone()
        if managed is None or bool(managed["legacy_protected"]):
            yield False
            return
        referenced = conn.execute(
            """
            SELECT 1 AS present
            FROM (
                SELECT storage_key FROM source_asset_references
                UNION ALL
                SELECT storage_key FROM source_asset_staging_references
                UNION ALL
                SELECT storage_key
                FROM document_extraction_page_staging_references
                UNION ALL
                SELECT storage_key FROM source_capture_asset_references
                UNION ALL
                SELECT storage_key FROM source_capture_asset_staging_references
                UNION ALL
                SELECT storage_key FROM sor_documents
                UNION ALL
                SELECT storage_key FROM knowledge_documents
                UNION ALL
                SELECT storage_key FROM export_jobs
                UNION ALL
                SELECT payload_storage_key AS storage_key
                FROM knowledge_document_syncs
                WHERE payload_storage_key IS NOT NULL
            ) AS refs
            WHERE refs.storage_key = ?
            LIMIT 1
            """,
            (normalized_key,),
        ).fetchone()
        if referenced is not None:
            yield False
            return
        yield True
        deleted = conn.execute(
            "DELETE FROM stored_objects WHERE storage_key = ?",
            (normalized_key,),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("Source asset metadata deletion lost its object claim.")


def get_entry_command_result(*, kind: str, scope: str, command_id: str,
                             request_sha256: str | None = None) -> dict | None:
    validate_command_id(command_id)
    from reqpilotx_server.saas.context import project_command_id
    if kind == "project.create":
        command_id = project_command_id(command_id)
    validate_command_id(command_id)
    with get_conn() as conn:
        return read_receipt(conn, kind=kind, scope=scope, command_id=command_id,
                            request_sha256=request_sha256)


def create_project(name: str, customer_name: str, *, command_id: str | None = None) -> dict[str, Any]:
    validate_command_id(command_id)
    from reqpilotx_server.saas.context import current_scope, project_command_id
    from reqpilotx_server.saas.config import SaaSSettings
    owner = current_scope()
    if SaaSSettings.from_environment().enabled and owner is None:
        raise PermissionError("saas_project_requires_trusted_owner")
    command_id = project_command_id(command_id)
    name, customer_name = name.strip(), customer_name.strip()
    project_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        replay = claim_receipt(conn, kind="project.create", scope="", command_id=command_id,
                               request_sha256=request_hash({"name": name, "customer_name": customer_name}))
        if replay is not None:
            return replay
        conn.execute(
            "INSERT INTO projects (id, name, customer_name, created_at, knowledge_base_version) VALUES (?, ?, ?, ?, 0)",
            (project_id, name, customer_name, created_at),
        )
        if owner is not None:
            # Business row, ownership and command receipt commit together.
            conn.execute("INSERT INTO saas_project_owners(project_id, account_id) VALUES(?,?)",
                         (project_id, owner.account_id))
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        result = _to_dict(row) or {}
        complete_receipt(conn, kind="project.create", scope="", command_id=command_id, result=result)
    return result


def list_projects() -> list[dict[str, Any]]:
    from reqpilotx_server.saas.context import current_scope
    from reqpilotx_server.saas.config import SaaSSettings
    owner = current_scope()
    if SaaSSettings.from_environment().enabled and owner is None:
        raise PermissionError("saas_catalog_requires_trusted_owner")
    with get_conn() as conn:
        if owner is None:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute("""SELECT p.* FROM projects p JOIN saas_project_owners o
                ON o.project_id=p.id WHERE o.account_id=? ORDER BY p.created_at DESC""",
                (owner.account_id,)).fetchall()
    return _to_dict_list(rows)


def get_project(project_id: str) -> dict[str, Any] | None:
    from reqpilotx_server.saas.context import current_scope
    from reqpilotx_server.saas.store import owns_project
    owner = current_scope()
    if owner is not None and ((owner.project_id and owner.project_id != project_id)
                              or not owns_project(owner.account_id, project_id)):
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _to_dict(row)


def update_project_processing_profile(
    *,
    project_id: str,
    expected_revision: int,
    profile_json: str,
    profile_sha256: str,
) -> dict[str, Any] | None:
    """Optimistically replace one project's canonical processing profile."""

    with get_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE projects
            SET processing_profile_json = ?,
                processing_profile_revision = ?,
                processing_profile_sha256 = ?
            WHERE id = ? AND processing_profile_revision = ?
            """,
            (
                str(profile_json),
                int(expected_revision) + 1,
                str(profile_sha256),
                str(project_id),
                int(expected_revision),
            ),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?",
            (str(project_id),),
        ).fetchone()
    return _to_dict(row)


def add_sor_document(
    project_id: str,
    file_name: str,
    file_type: str,
    file_path: str,
    extracted_text: str,
    *,
    source_sha256: str = "",
    source_size: int = 0,
    declared_extension: str = "",
    detected_format: str = "",
    preflight_schema_version: str = "",
    media_type: str = "application/octet-stream",
    command_id: str | None = None,
) -> dict[str, Any]:
    document_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        replay = claim_receipt(conn, kind="document.import", scope=project_id, command_id=command_id,
                               request_sha256=sor_import_request_hash(project_id=project_id,
                                   file_name=file_name, source_sha256=source_sha256,
                                   source_size=source_size, media_type=media_type))
        if replay is not None:
            return replay
        if is_postgresql_runtime():
            _register_stored_object_in_conn(
                conn,
                storage_key=file_path,
                sha256=source_sha256,
                size_bytes=source_size,
                media_type=media_type,
                object_kind="sor-document",
            )
        conn.execute(
            """
            INSERT INTO sor_documents
            (id, project_id, file_name, file_type, file_path, extracted_text,
             source_sha256, source_size, declared_extension, detected_format, preflight_schema_version,
             created_at, workflow_stage, requirement_set_version, extraction_locked_at,
             ai_status, ai_assessed_at, ai_requirement_set_version, ai_knowledge_base_version, updated_at,
             preview_status, preview_generated_at, preview_file_mtime, preview_error, preview_generation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UPLOADED', 0, '', 'NOT_RUN', '', 0, 0, ?, 'NOT_READY', '', '', '', '')
            """,
            (
                document_id,
                project_id,
                file_name,
                file_type,
                file_path,
                extracted_text,
                str(source_sha256 or ""),
                max(int(source_size or 0), 0),
                str(declared_extension or "").lower(),
                str(detected_format or "").lower(),
                str(preflight_schema_version or ""),
                created_at,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ?", (document_id,)
        ).fetchone()
        result = _to_dict(row) or {}
        complete_receipt(conn, kind="document.import", scope=project_id, command_id=command_id, result=result)
    return result


def sor_import_request_hash(*, project_id: str, file_name: str, source_sha256: str,
                            source_size: int, media_type: str) -> str:
    return request_hash({"project_id": project_id, "file_name": file_name,
                         "source_sha256": source_sha256, "source_size": source_size,
                         "media_type": media_type})


def get_sor_document(document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
    result = _to_dict(row)
    if result is not None:
        from reqpilotx_server.saas.context import current_scope
        if current_scope() is not None and get_project(result["project_id"]) is None:
            return None
    return result


def get_sor_document_publication_currentness_generation(
    project_id: str,
    document_id: str,
) -> int | None:
    """Read the DB-bound generation for Structure/table publication facts."""

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT publication_currentness_generation
            FROM sor_documents
            WHERE id = ? AND project_id = ?
            """,
            (document_id, project_id),
        ).fetchone()
    if row is None:
        return None
    return int(row["publication_currentness_generation"] or 0)


def list_sor_documents(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sor_documents WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return _to_dict_list(rows)


def _document_source_hash(document: sqlite3.Row | dict[str, Any]) -> str:
    if isinstance(document, sqlite3.Row):
        source_sha256 = str(document["source_sha256"] or "") if "source_sha256" in document.keys() else ""
    else:
        source_sha256 = str(document.get("source_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        return source_sha256
    text = str(document["extracted_text"] if isinstance(document, sqlite3.Row) else document.get("extracted_text") or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_requirement_set(requirement_set_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT rs.*,
                   (SELECT j.id FROM ai_analysis_jobs j
                    WHERE j.requirement_set_id = rs.id
                    ORDER BY j.created_at DESC LIMIT 1) AS latest_ai_run_id,
                   (SELECT j.status FROM ai_analysis_jobs j
                    WHERE j.requirement_set_id = rs.id
                    ORDER BY j.created_at DESC LIMIT 1) AS latest_ai_run_status,
                   (SELECT j.finished_at FROM ai_analysis_jobs j
                    WHERE j.requirement_set_id = rs.id
                    ORDER BY j.created_at DESC LIMIT 1) AS latest_ai_run_finished_at
            FROM requirement_sets rs
            WHERE rs.id = ?
            """,
            (requirement_set_id,),
        ).fetchone()
    return _to_dict(row)


def get_active_requirement_set(sor_document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT rs.*
            FROM requirement_sets rs
            INNER JOIN sor_documents d ON d.id = rs.sor_document_id
            WHERE rs.sor_document_id = ?
              AND (rs.id = d.active_requirement_set_id OR rs.is_active = 1)
            ORDER BY (rs.id = d.active_requirement_set_id) DESC, rs.version DESC
            LIMIT 1
            """,
            (sor_document_id,),
        ).fetchone()
    return _to_dict(row)


def list_requirement_sets(project_id: str, sor_document_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT rs.*,
                   COUNT(r.id) AS requirement_count,
                   SUM(CASE WHEN UPPER(COALESCE(r.entry_kind, 'REQUIREMENT')) IN ('REQUIREMENT', 'HYBRID') THEN 1 ELSE 0 END)
                       AS decision_eligible_count,
                   SUM(CASE WHEN UPPER(COALESCE(r.entry_kind, 'REQUIREMENT')) = 'HEADING' THEN 1 ELSE 0 END)
                       AS heading_count,
                   SUM(CASE WHEN UPPER(COALESCE(r.entry_kind, 'REQUIREMENT')) IN ('REQUIREMENT', 'HYBRID')
                                  AND NULLIF(TRIM(COALESCE(a.final_decision, '')), '') IS NOT NULL THEN 1 ELSE 0 END)
                       AS human_decision_count,
                   (SELECT j.id FROM ai_analysis_jobs j WHERE j.requirement_set_id = rs.id
                    ORDER BY j.created_at DESC LIMIT 1) AS latest_ai_run_id,
                   (SELECT j.status FROM ai_analysis_jobs j WHERE j.requirement_set_id = rs.id
                    ORDER BY j.created_at DESC LIMIT 1) AS latest_ai_run_status,
                   (SELECT j.finished_at FROM ai_analysis_jobs j WHERE j.requirement_set_id = rs.id
                    ORDER BY j.created_at DESC LIMIT 1) AS latest_ai_run_finished_at
            FROM requirement_sets rs
            LEFT JOIN requirements r ON r.requirement_set_id = rs.id
            LEFT JOIN assessments a ON a.requirement_id = r.id
            WHERE rs.project_id = ? AND rs.sor_document_id = ?
            GROUP BY rs.id
            ORDER BY rs.version DESC
            """,
            (project_id, sor_document_id),
        ).fetchall()
    output = _to_dict_list(rows)
    for item in output:
        item["is_active"] = bool(int(item.get("is_active") or 0))
        for field in ("requirement_count", "decision_eligible_count", "heading_count", "human_decision_count"):
            item[field] = int(item.get(field) or 0)
    return output


def create_requirement_set(
    project_id: str,
    sor_document_id: str,
    *,
    source_hash: str = "",
    preview_generation_id: str = "",
    supersedes_id: str = "",
) -> dict[str, Any]:
    set_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active_job = _get_active_ai_analysis_job_in_conn(conn, sor_document_id)
        if active_job is not None:
            raise RequirementSetMutationBlocked(
                f"AI analysis job {active_job['id']} is still {active_job['status']}; "
                "stop or finish it before creating a Requirement Set."
            )
        document = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ? AND project_id = ?",
            (sor_document_id, project_id),
        ).fetchone()
        if document is None:
            return {}
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM requirement_sets WHERE sor_document_id = ?",
            (sor_document_id,),
        ).fetchone()
        version = int(row["next_version"] or 1)
        current = conn.execute(
            "SELECT id FROM requirement_sets WHERE sor_document_id = ? AND is_active = 1",
            (sor_document_id,),
        ).fetchone()
        parent_id = str(supersedes_id or (current["id"] if current else "")).strip()
        conn.execute(
            """
            INSERT INTO requirement_sets
            (id, project_id, sor_document_id, version, status, is_active,
             source_hash, preview_generation_id, supersedes_id,
             created_at, activated_at, locked_at)
            VALUES (?, ?, ?, ?, 'DRAFT', 0, ?, ?, ?, ?, '', '')
            """,
            (
                set_id,
                project_id,
                sor_document_id,
                version,
                str(source_hash or _document_source_hash(document)),
                str(preview_generation_id or ""),
                parent_id,
                created_at,
            ),
        )
        created = conn.execute("SELECT * FROM requirement_sets WHERE id = ?", (set_id,)).fetchone()
    return _to_dict(created) or {}


def create_or_get_requirement_set_for_publication(
    project_id: str,
    sor_document_id: str,
    *,
    publication_key: str,
    publication_owner_token: str,
    expected_publication_currentness_generation: int,
    source_hash: str = "",
    preview_generation_id: str = "",
    supersedes_id: str = "",
    entry_classification_snapshot_id: str = "",
    entry_classification_snapshot_sha256: str = "",
    entry_classification_lineage: tuple[str, ...] = (),
    publication_lease_seconds: int = (
        _DEFAULT_REQUIREMENT_SET_PUBLICATION_LEASE_SECONDS
    ),
    now_utc: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create one persistent DRAFT for a canonical working-tree publication.

    A no-op document update provides the per-document write lock on PostgreSQL;
    ``BEGIN IMMEDIATE`` provides the corresponding SQLite serialization.  The
    partial unique index remains the final integrity boundary, so two workers
    cannot allocate different versions for the same publication identity.
    """

    normalized_key = str(publication_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_key):
        raise ValueError("publication_key must be a lowercase SHA-256")
    if (
        isinstance(expected_publication_currentness_generation, bool)
        or int(expected_publication_currentness_generation) < 0
    ):
        raise ValueError(
            "expected_publication_currentness_generation must be non-negative"
        )
    expected_currentness_generation = int(
        expected_publication_currentness_generation
    )
    classification_snapshot_id = str(
        entry_classification_snapshot_id or ""
    ).strip()
    classification_snapshot_sha256 = str(
        entry_classification_snapshot_sha256 or ""
    ).strip()
    if bool(classification_snapshot_id) != bool(
        classification_snapshot_sha256
    ):
        raise ValueError(
            "entry classification snapshot ID and SHA-256 must be paired"
        )
    if classification_snapshot_id and (
        len(classification_snapshot_id) > 200
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            classification_snapshot_sha256,
        )
    ):
        raise ValueError(
            "entry classification snapshot identity is invalid"
        )
    classification_lineage = tuple(
        str(value or "") for value in entry_classification_lineage
    )
    if classification_snapshot_id:
        required_lineage_positions = (0, 1, 2, 3, 4, 5, 7)
        if len(classification_lineage) != 8 or any(
            not classification_lineage[index]
            for index in required_lineage_positions
        ):
            raise ValueError(
                "entry classification lineage is required for publication"
            )
    elif classification_lineage:
        raise ValueError(
            "entry classification lineage requires a paired snapshot identity"
        )
    owner_token, heartbeat_at, lease_expires_at = (
        _requirement_set_publication_lease_values(
            owner_token=publication_owner_token,
            lease_seconds=publication_lease_seconds,
            now_utc=now_utc,
        )
    )
    lease_now = datetime.fromisoformat(heartbeat_at)

    set_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Acquire one row-level lock in PostgreSQL and one write reservation in
        # SQLite before reading either the existing key or the next version.
        locked = conn.execute(
            """
            UPDATE sor_documents
            SET updated_at = updated_at
            WHERE id = ? AND project_id = ?
            """,
            (sor_document_id, project_id),
        )
        if locked.rowcount != 1:
            return {}, False
        document = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ? AND project_id = ?",
            (sor_document_id, project_id),
        ).fetchone()
        if document is None:
            return {}, False
        if int(document["publication_currentness_generation"] or 0) != (
            expected_currentness_generation
        ):
            raise RequirementSetPublicationCurrentnessConflict(
                "Structure or table projection changed before publication began."
            )
        if classification_snapshot_id:
            expected_source_hash = str(
                source_hash or _document_source_hash(document)
            )
            if classification_lineage[:3] != (
                project_id,
                sor_document_id,
                expected_source_hash,
            ):
                raise ValueError(
                    "entry classification lineage does not match the document"
                )
            if is_postgresql_runtime():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(?))",
                    (
                        "\x1f".join(
                            (
                                "entry-classification-snapshot/v1",
                                *classification_lineage,
                            )
                        ),
                    ),
                )
            current_classification = conn.execute(
                """
                SELECT id, payload_sha256, status
                FROM entry_classification_snapshots
                WHERE project_id = ? AND sor_document_id = ?
                  AND source_sha256 = ? AND working_tree_id = ?
                  AND working_tree_sha256 = ?
                  AND effective_structure_id = ?
                  AND semantic_snapshot_id = ? AND policy_version = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                classification_lineage,
            ).fetchone()
            if (
                current_classification is None
                or str(current_classification["id"] or "")
                != classification_snapshot_id
                or str(current_classification["payload_sha256"] or "")
                != classification_snapshot_sha256
                or str(current_classification["status"] or "").upper()
                != "ACCEPTED"
            ):
                raise RequirementSetPublicationConflict(
                    "accepted entry classification is no longer current"
                )
        conn.execute(
            """
            UPDATE requirement_sets
            SET publication_state = publication_state
            WHERE sor_document_id = ? AND publication_key = ?
            """,
            (sor_document_id, normalized_key),
        )
        existing = conn.execute(
            """
            SELECT * FROM requirement_sets
            WHERE sor_document_id = ? AND publication_key = ?
            LIMIT 1
            """,
            (sor_document_id, normalized_key),
        ).fetchone()
        if existing is not None:
            existing_snapshot_id = str(
                existing["entry_classification_snapshot_id"] or ""
            ).strip()
            existing_snapshot_sha256 = str(
                existing["entry_classification_snapshot_sha256"] or ""
            ).strip()
            if (
                existing_snapshot_id != classification_snapshot_id
                or existing_snapshot_sha256
                != classification_snapshot_sha256
            ):
                raise RuntimeError(
                    "publication key is bound to a different entry "
                    "classification snapshot"
                )
            existing_owner = str(
                existing["publication_owner_token"] or ""
            ).strip()
            existing_expiry = str(
                existing["publication_lease_expires_at"] or ""
            ).strip()
            existing_heartbeat = str(
                existing["publication_heartbeat_at"] or ""
            ).strip()
            lease_is_complete = bool(
                existing_owner and existing_expiry and existing_heartbeat
            )
            takeover_allowed = bool(
                lease_is_complete
                and _REQUIREMENT_SET_PUBLICATION_OWNER_RE.fullmatch(
                    existing_owner
                )
                and _publication_lease_is_expired(
                    existing_expiry,
                    now_utc=lease_now,
                )
                and str(existing["status"] or "").upper() == "DRAFT"
                and not bool(int(existing["is_active"] or 0))
                and not str(existing["activated_at"] or "").strip()
                and str(existing["publication_state"] or "").upper()
                == "PUBLISHING"
                and not str(
                    existing["requirement_tree_object_key"] or ""
                ).strip()
                and not str(existing["requirement_tree_sha256"] or "").strip()
            )
            if not takeover_allowed:
                # Empty lease fields identify historical rows. They are
                # deliberately not assigned an age or deleted by inference.
                return _to_dict(existing) or {}, False
            _delete_requirement_set_requirement_rows_in_conn(
                conn,
                sor_document_id=sor_document_id,
                requirement_set_id=str(existing["id"]),
            )
            updated = conn.execute(
                """
                UPDATE requirement_sets
                SET publication_owner_token = ?,
                    publication_heartbeat_at = ?,
                    publication_lease_expires_at = ?
                WHERE id = ? AND sor_document_id = ?
                  AND publication_key = ?
                  AND status = 'DRAFT' AND is_active = 0
                  AND COALESCE(activated_at, '') = ''
                  AND publication_state = 'PUBLISHING'
                  AND COALESCE(requirement_tree_object_key, '') = ''
                  AND COALESCE(requirement_tree_sha256, '') = ''
                  AND publication_owner_token = ?
                  AND publication_lease_expires_at = ?
                  AND publication_heartbeat_at = ?
                """,
                (
                    owner_token,
                    heartbeat_at,
                    lease_expires_at,
                    str(existing["id"]),
                    sor_document_id,
                    normalized_key,
                    existing_owner,
                    existing_expiry,
                    existing_heartbeat,
                ),
            )
            if updated.rowcount != 1:
                # The partial rows were deleted in this transaction.  Never
                # commit that cleanup unless the exact expired owner was
                # replaced by this claimant as the same atomic operation.
                raise RequirementSetPublicationConflict(
                    "publication ownership changed during expired-lease takeover"
                )
            claimed = conn.execute(
                "SELECT * FROM requirement_sets WHERE id = ?",
                (str(existing["id"]),),
            ).fetchone()
            return _to_dict(claimed) or {}, False

        active_job = _get_active_ai_analysis_job_in_conn(conn, sor_document_id)
        if active_job is not None:
            raise RequirementSetMutationBlocked(
                f"AI analysis job {active_job['id']} is still {active_job['status']}; "
                "stop or finish it before creating a Requirement Set."
            )
        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM requirement_sets
            WHERE sor_document_id = ?
            """,
            (sor_document_id,),
        ).fetchone()
        version = int(row["next_version"] or 1)
        current = conn.execute(
            """
            SELECT id FROM requirement_sets
            WHERE sor_document_id = ? AND is_active = 1
            """,
            (sor_document_id,),
        ).fetchone()
        parent_id = str(
            supersedes_id or (current["id"] if current else "")
        ).strip()
        conn.execute(
            """
            INSERT INTO requirement_sets
            (id, project_id, sor_document_id, version, status, is_active,
             source_hash, preview_generation_id, supersedes_id,
             created_at, activated_at, locked_at,
             publication_key, publication_state,
             publication_owner_token, publication_lease_expires_at,
             publication_heartbeat_at,
             entry_classification_snapshot_id,
             entry_classification_snapshot_sha256)
            VALUES (?, ?, ?, ?, 'DRAFT', 0, ?, ?, ?, ?, '', '', ?, 'PUBLISHING', ?, ?, ?, ?, ?)
            """,
            (
                set_id,
                project_id,
                sor_document_id,
                version,
                str(source_hash or _document_source_hash(document)),
                str(preview_generation_id or ""),
                parent_id,
                created_at,
                normalized_key,
                owner_token,
                lease_expires_at,
                heartbeat_at,
                classification_snapshot_id,
                classification_snapshot_sha256,
            ),
        )
        created = conn.execute(
            "SELECT * FROM requirement_sets WHERE id = ?",
            (set_id,),
        ).fetchone()
    return _to_dict(created) or {}, True


def claim_publication_key_for_existing_draft(
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str,
    *,
    publication_key: str,
) -> dict[str, Any]:
    """Bind a verified pre-key DRAFT to its canonical publication identity.

    Only an immutable-snapshot DRAFT can be claimed.  If another worker has
    already bound the key, that canonical row is returned instead.
    """

    normalized_key = str(publication_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_key):
        raise ValueError("publication_key must be a lowercase SHA-256")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        locked = conn.execute(
            """
            UPDATE sor_documents
            SET updated_at = updated_at
            WHERE id = ? AND project_id = ?
            """,
            (sor_document_id, project_id),
        )
        if locked.rowcount != 1:
            return {}
        existing = conn.execute(
            """
            SELECT * FROM requirement_sets
            WHERE sor_document_id = ? AND publication_key = ?
            LIMIT 1
            """,
            (sor_document_id, normalized_key),
        ).fetchone()
        if existing is not None:
            return _to_dict(existing) or {}
        target = conn.execute(
            """
            SELECT * FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
              AND status = 'DRAFT'
              AND COALESCE(publication_key, '') = ''
              AND COALESCE(requirement_tree_object_key, '') <> ''
              AND COALESCE(requirement_tree_sha256, '') <> ''
            """,
            (requirement_set_id, project_id, sor_document_id),
        ).fetchone()
        if target is None:
            return {}
        updated = conn.execute(
            """
            UPDATE requirement_sets
            SET publication_key = ?, publication_state = 'READY'
            WHERE id = ? AND COALESCE(publication_key, '') = ''
            """,
            (normalized_key, requirement_set_id),
        )
        if updated.rowcount != 1:
            return {}
        claimed = conn.execute(
            "SELECT * FROM requirement_sets WHERE id = ?",
            (requirement_set_id,),
        ).fetchone()
    return _to_dict(claimed) or {}


def heartbeat_requirement_set_publication(
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str,
    *,
    publication_owner_token: str,
    publication_lease_seconds: int = (
        _DEFAULT_REQUIREMENT_SET_PUBLICATION_LEASE_SECONDS
    ),
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Extend only the exact active owner of an incomplete publication."""

    owner_token, heartbeat_at, lease_expires_at = (
        _requirement_set_publication_lease_values(
            owner_token=publication_owner_token,
            lease_seconds=publication_lease_seconds,
            now_utc=now_utc,
        )
    )
    lease_now = datetime.fromisoformat(heartbeat_at)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_requirement_set_document_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=sor_document_id,
        ):
            return {}
        target = conn.execute(
            """
            SELECT * FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (requirement_set_id, project_id, sor_document_id),
        ).fetchone()
        if (
            target is None
            or str(target["status"] or "").upper() != "DRAFT"
            or bool(int(target["is_active"] or 0))
            or str(target["activated_at"] or "").strip()
            or str(target["publication_state"] or "").upper()
            != "PUBLISHING"
            or not str(target["publication_key"] or "").strip()
            or str(target["publication_owner_token"] or "").strip()
            != owner_token
            or not str(target["publication_heartbeat_at"] or "").strip()
            or not str(
                target["publication_lease_expires_at"] or ""
            ).strip()
            or _publication_lease_is_expired(
                target["publication_lease_expires_at"],
                now_utc=lease_now,
            )
            or str(target["requirement_tree_object_key"] or "").strip()
            or str(target["requirement_tree_sha256"] or "").strip()
        ):
            return {}
        previous_expiry = str(target["publication_lease_expires_at"])
        updated = conn.execute(
            """
            UPDATE requirement_sets
            SET publication_heartbeat_at = ?,
                publication_lease_expires_at = ?
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
              AND publication_state = 'PUBLISHING'
              AND publication_owner_token = ?
              AND publication_lease_expires_at = ?
              AND COALESCE(requirement_tree_object_key, '') = ''
              AND COALESCE(requirement_tree_sha256, '') = ''
            """,
            (
                heartbeat_at,
                lease_expires_at,
                requirement_set_id,
                project_id,
                sor_document_id,
                owner_token,
                previous_expiry,
            ),
        )
        if updated.rowcount != 1:
            return {}
        row = conn.execute(
            "SELECT * FROM requirement_sets WHERE id = ?",
            (requirement_set_id,),
        ).fetchone()
    return _to_dict(row) or {}


def recover_historical_requirement_set_publication(
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str,
    *,
    publication_key: str,
    operator_confirmation: str,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Convert one pre-lease publication to an explicit expired fence.

    Empty lease fields have no trustworthy age, so ordinary publication
    retries deliberately fail closed.  This recovery primitive requires the
    operator to identify the exact document, set and publication key and to
    acknowledge that publishers have been quiesced. It writes a complete
    already-expired lease, allowing the next ordinary publisher to take over
    immediately using the normal fenced cleanup path. Completed, active,
    tree-attached, leased or enrichment publications never satisfy the CAS.
    """

    if str(operator_confirmation or "") != (
        HISTORICAL_PUBLICATION_RECOVERY_CONFIRMATION
    ):
        raise ValueError(
            "historical publication recovery requires explicit operator "
            "quiescence confirmation: "
            f"{HISTORICAL_PUBLICATION_RECOVERY_CONFIRMATION}"
        )
    normalized_key = str(publication_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_key):
        raise ValueError("publication_key must be a lowercase SHA-256")
    current = _requirement_set_publication_now_utc(now_utc)
    expired_at = current - timedelta(microseconds=1)
    owner_token = new_requirement_set_publication_owner_token()
    heartbeat_at = expired_at.isoformat()
    lease_expires_at = expired_at.isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_requirement_set_document_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=sor_document_id,
        ):
            return {}
        conn.execute(
            """
            UPDATE requirement_sets
            SET publication_state = publication_state
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (requirement_set_id, project_id, sor_document_id),
        )
        updated = conn.execute(
            """
            UPDATE requirement_sets
            SET publication_owner_token = ?,
                publication_heartbeat_at = ?,
                publication_lease_expires_at = ?
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
              AND publication_key = ?
              AND status = 'DRAFT' AND is_active = 0
              AND COALESCE(activated_at, '') = ''
              AND publication_state = 'PUBLISHING'
              AND COALESCE(requirement_tree_object_key, '') = ''
              AND COALESCE(requirement_tree_sha256, '') = ''
              AND COALESCE(enrichment_publication_binding_id, '') = ''
              AND COALESCE(enrichment_publication_binding_sha256, '') = ''
              AND COALESCE(publication_owner_token, '') = ''
              AND COALESCE(publication_heartbeat_at, '') = ''
              AND COALESCE(publication_lease_expires_at, '') = ''
            """,
            (
                owner_token,
                heartbeat_at,
                lease_expires_at,
                requirement_set_id,
                project_id,
                sor_document_id,
                normalized_key,
            ),
        )
        if updated.rowcount != 1:
            return {}
        # Do not clean partial rows here. The next ordinary publisher owns the
        # expired-lease CAS and performs cleanup + takeover atomically.
        claimed = conn.execute(
            "SELECT * FROM requirement_sets WHERE id = ?",
            (requirement_set_id,),
        ).fetchone()
    return _to_dict(claimed) or {}


def attach_requirement_tree_snapshot(
    requirement_set_id: str,
    *,
    object_key: str,
    tree_sha256: str,
    schema_version: str,
    structure_snapshot_id: str,
    structure_tree_sha256: str,
    structure_revision: int,
    publication_owner_token: str = "",
    expected_publication_currentness_generation: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Attach one immutable tree object identity to a Requirement Set.

    Idempotent retries may write the same identity. A different second identity
    is rejected so a published version can never silently follow current
    Structure state.
    """

    normalized_set_id = str(requirement_set_id or "").strip()
    normalized_key = str(object_key or "").strip()
    normalized_schema = str(schema_version or "").strip()
    normalized_snapshot_id = str(structure_snapshot_id or "").strip()
    if not normalized_set_id or not normalized_key or not normalized_schema:
        raise ValueError("requirement tree snapshot binding is incomplete")
    for name, value in (
        ("tree_sha256", tree_sha256),
        ("structure_tree_sha256", structure_tree_sha256),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value or "")):
            raise ValueError(f"{name} must be lowercase SHA-256")
    if isinstance(structure_revision, bool) or int(structure_revision) < 0:
        raise ValueError("structure_revision must be a non-negative integer")

    candidate = {
        "requirement_tree_object_key": normalized_key,
        "requirement_tree_sha256": tree_sha256,
        "requirement_tree_schema_version": normalized_schema,
        "structure_snapshot_id": normalized_snapshot_id,
        "structure_tree_sha256": structure_tree_sha256,
        "structure_revision": int(structure_revision),
    }
    normalized_owner_token = str(publication_owner_token or "").strip()
    if normalized_owner_token and not _REQUIREMENT_SET_PUBLICATION_OWNER_RE.fullmatch(
        normalized_owner_token
    ):
        raise ValueError("publication owner token is invalid")
    if (
        expected_publication_currentness_generation is not None
        and (
            isinstance(expected_publication_currentness_generation, bool)
            or int(expected_publication_currentness_generation) < 0
        )
    ):
        raise ValueError(
            "expected_publication_currentness_generation must be non-negative"
        )
    current_time = _requirement_set_publication_now_utc(now_utc)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = conn.execute(
            """
            SELECT project_id, sor_document_id
            FROM requirement_sets WHERE id = ?
            """,
            (normalized_set_id,),
        ).fetchone()
        if identity is None or not _lock_requirement_set_document_in_conn(
            conn,
            project_id=str(identity["project_id"] or ""),
            sor_document_id=str(identity["sor_document_id"] or ""),
        ):
            return {}
        currentness = conn.execute(
            """
            SELECT publication_currentness_generation
            FROM sor_documents
            WHERE id = ? AND project_id = ?
            """,
            (
                str(identity["sor_document_id"] or ""),
                str(identity["project_id"] or ""),
            ),
        ).fetchone()
        if currentness is None:
            return {}
        conn.execute(
            """
            UPDATE requirement_sets
            SET publication_state = publication_state
            WHERE id = ?
            """,
            (normalized_set_id,),
        )
        row = conn.execute(
            "SELECT * FROM requirement_sets WHERE id = ?",
            (normalized_set_id,),
        ).fetchone()
        if row is None:
            return {}
        publication_key = str(row["publication_key"] or "").strip()
        publication_state = str(row["publication_state"] or "").strip().upper()
        fenced_publication = bool(publication_key and publication_state == "PUBLISHING")
        if fenced_publication and (
            expected_publication_currentness_generation is None
            or int(currentness["publication_currentness_generation"] or 0)
            != int(expected_publication_currentness_generation)
        ):
            raise RequirementSetPublicationCurrentnessConflict(
                "Structure or table projection changed before publication "
                "finalization."
            )
        if fenced_publication and (
            not normalized_owner_token
            or str(row["publication_owner_token"] or "").strip()
            != normalized_owner_token
            or not str(row["publication_heartbeat_at"] or "").strip()
            or not str(row["publication_lease_expires_at"] or "").strip()
            or _publication_lease_is_expired(
                row["publication_lease_expires_at"],
                now_utc=current_time,
            )
        ):
            raise RequirementSetMutationBlocked(
                "Requirement Set publication lease is missing, expired, or owned "
                "by another publisher."
            )
        if normalized_owner_token and not fenced_publication:
            raise RequirementSetMutationBlocked(
                "Requirement Set publication owner no longer owns an incomplete draft."
            )
        existing_key = str(row["requirement_tree_object_key"] or "")
        if existing_key:
            mismatches = [
                field
                for field, value in candidate.items()
                if (
                    int(row[field] or 0) != value
                    if field == "structure_revision"
                    else str(row[field] or "") != str(value)
                )
            ]
            if mismatches:
                raise RequirementSetMutationBlocked(
                    "Requirement Set tree snapshot is immutable; "
                    f"conflicting fields: {', '.join(mismatches)}"
                )
            if (
                str(row["publication_key"] or "").strip()
                and str(row["publication_state"] or "").strip().upper()
                != "READY"
            ):
                updated = conn.execute(
                    """
                    UPDATE requirement_sets
                    SET publication_state = 'READY',
                        publication_owner_token = '',
                        publication_lease_expires_at = '',
                        publication_heartbeat_at = ''
                    WHERE id = ?
                      AND (? = '' OR publication_owner_token = ?)
                    """,
                    (
                        normalized_set_id,
                        normalized_owner_token,
                        normalized_owner_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise RequirementSetMutationBlocked(
                        "Requirement Set publication ownership changed before "
                        "idempotent finalization."
                    )
                row = conn.execute(
                    "SELECT * FROM requirement_sets WHERE id = ?",
                    (normalized_set_id,),
                ).fetchone()
            return _to_dict(row) or {}
        updated = conn.execute(
            """
            UPDATE requirement_sets
            SET requirement_tree_object_key = ?,
                requirement_tree_sha256 = ?,
                requirement_tree_schema_version = ?,
                structure_snapshot_id = ?,
                structure_tree_sha256 = ?,
                structure_revision = ?,
                publication_state = CASE
                    WHEN COALESCE(publication_key, '') <> '' THEN 'READY'
                    ELSE publication_state
                END,
                publication_owner_token = CASE
                    WHEN COALESCE(publication_key, '') <> '' THEN ''
                    ELSE publication_owner_token
                END,
                publication_lease_expires_at = CASE
                    WHEN COALESCE(publication_key, '') <> '' THEN ''
                    ELSE publication_lease_expires_at
                END,
                publication_heartbeat_at = CASE
                    WHEN COALESCE(publication_key, '') <> '' THEN ''
                    ELSE publication_heartbeat_at
                END
            WHERE id = ?
              AND (? = '' OR publication_owner_token = ?)
            """,
            (
                normalized_key,
                tree_sha256,
                normalized_schema,
                normalized_snapshot_id,
                structure_tree_sha256,
                int(structure_revision),
                normalized_set_id,
                normalized_owner_token,
                normalized_owner_token,
            ),
        )
        if updated.rowcount != 1:
            raise RequirementSetMutationBlocked(
                "Requirement Set publication ownership changed before finalization."
            )
        attached = conn.execute(
            "SELECT * FROM requirement_sets WHERE id = ?",
            (normalized_set_id,),
        ).fetchone()
    return _to_dict(attached) or {}


def activate_requirement_set(
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str,
    *,
    workflow_stage: str = "REVIEWING",
    table_assets: list[dict[str, Any]] | None = None,
    preview_generation_id: str = "",
    expected_active_requirement_set_id: str = "",
    expected_enrichment_publication_binding_id: str = "",
    expected_enrichment_publication_binding_sha256: str = "",
) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _activate_requirement_set_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=sor_document_id,
            requirement_set_id=requirement_set_id,
            workflow_stage=workflow_stage,
            table_assets=table_assets,
            preview_generation_id=preview_generation_id,
            expected_active_requirement_set_id=(
                expected_active_requirement_set_id
            ),
            expected_enrichment_publication_binding_id=(
                expected_enrichment_publication_binding_id
            ),
            expected_enrichment_publication_binding_sha256=(
                expected_enrichment_publication_binding_sha256
            ),
        )


def _lock_requirement_set_document_in_conn(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    sor_document_id: str,
) -> bool:
    """Serialize activation and failed-publication cleanup per document."""

    locked = conn.execute(
        """
        UPDATE sor_documents
        SET updated_at = updated_at
        WHERE id = ? AND project_id = ?
        """,
        (sor_document_id, project_id),
    )
    return locked.rowcount == 1


@contextmanager
def guard_unowned_requirement_tree_object_cleanup(
    *,
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str,
) -> Generator[bool, None, None]:
    """Serialize an orphan delete with Requirement Set creation and ownership.

    The yielded flag is true only while the deterministic Requirement Set ID
    has no row.  The transaction and document lock remain held for the entire
    caller block, so a replacement publication cannot claim the same ID between
    the owner check and the object-store delete.
    """

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        document_locked = _lock_requirement_set_document_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        owner = conn.execute(
            """
            SELECT id FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (requirement_set_id, project_id, sor_document_id),
        ).fetchone()
        # The no-op document update is the shared publication lifecycle lock
        # used by begin/finalize/abort.  Without that lock, fail closed and
        # leave the object for later reconciliation.
        yield document_locked and owner is None


def _activate_requirement_set_in_conn(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str,
    workflow_stage: str = "REVIEWING",
    table_assets: list[dict[str, Any]] | None = None,
    preview_generation_id: str = "",
    expected_active_requirement_set_id: str = "",
    expected_enrichment_publication_binding_id: str = "",
    expected_enrichment_publication_binding_sha256: str = "",
    enforce_ai_guard: bool = True,
    carried_ai_projection_source_requirement_set_id: str = "",
    carried_ai_projection_requires_analysis: bool = False,
) -> dict[str, Any]:
    """Activate and project a Requirement Set inside the caller's transaction."""

    if not _lock_requirement_set_document_in_conn(
        conn,
        project_id=project_id,
        sor_document_id=sor_document_id,
    ):
        return {}
    # Activation, impact projection, AI publication and human result writes
    # must all serialize on the same set-level boundary.  Take the document
    # lifecycle lock first everywhere, then acquire the sorted source/target
    # Matrix locks to avoid a document<->set lock-order inversion.
    from reqpilotx_server.adapters.persistence.requirement_matrix_states import (
        lock_requirement_matrix_sets_in_connection,
    )

    active_scope = conn.execute(
        """
        SELECT active_requirement_set_id
        FROM sor_documents
        WHERE id = ? AND project_id = ?
        """,
        (sor_document_id, project_id),
    ).fetchone()
    active_scope_id = str(
        active_scope["active_requirement_set_id"] or ""
    ).strip() if active_scope is not None else ""
    lock_requirement_matrix_sets_in_connection(
        conn,
        project_id=project_id,
        document_id=sor_document_id,
        requirement_set_ids=tuple(
            value
            for value in (active_scope_id, requirement_set_id)
            if value
        ),
    )
    expected_active_id = str(expected_active_requirement_set_id or "")
    expected_binding_id = str(
        expected_enrichment_publication_binding_id or ""
    )
    expected_binding_sha256 = str(
        expected_enrichment_publication_binding_sha256 or ""
    )
    carried_projection_source_id = str(
        carried_ai_projection_source_requirement_set_id or ""
    ).strip()
    strict_semantic_activation = bool(
        expected_binding_id or expected_binding_sha256
    )
    if (expected_active_id or strict_semantic_activation) and (
        expected_active_id != expected_active_id.strip()
    ):
        raise RequirementSetMutationBlocked(
            "Requirement Set activation precondition is invalid."
        )
    if strict_semantic_activation and (
        not expected_binding_id
        or not expected_binding_sha256
        or expected_binding_id != expected_binding_id.strip()
        or re.fullmatch(r"[0-9a-f]{64}", expected_binding_sha256) is None
    ):
        raise RequirementSetMutationBlocked(
            "Requirement Set semantic activation preconditions are incomplete "
            "or invalid."
        )
    timestamp = _now_iso()
    if enforce_ai_guard:
        active_job = _get_active_ai_analysis_job_in_conn(conn, sor_document_id)
        if active_job is not None:
            raise RequirementSetMutationBlocked(
                f"AI analysis job {active_job['id']} is still {active_job['status']}; "
                "stop or finish it before activating a Requirement Set."
            )
    target = conn.execute(
        """
        SELECT * FROM requirement_sets
        WHERE id = ? AND project_id = ? AND sor_document_id = ?
        """,
        (requirement_set_id, project_id, sor_document_id),
    ).fetchone()
    if target is None:
        return {}
    if carried_projection_source_id and (
        str(target["supersedes_id"] or "") != carried_projection_source_id
    ):
        raise RequirementSetMutationBlocked(
            "Carried AI projection must come from the direct source Requirement Set."
        )
    publication_key = str(target["publication_key"] or "").strip()
    publication_state = str(
        target["publication_state"] or ""
    ).strip().upper()
    enrichment_binding_id = str(
        target["enrichment_publication_binding_id"] or ""
    ).strip()
    enrichment_binding_sha256 = str(
        target["enrichment_publication_binding_sha256"] or ""
    ).strip()
    if bool(enrichment_binding_id) != bool(enrichment_binding_sha256):
        raise RequirementSetMutationBlocked(
            "Requirement Set enrichment publication binding is incomplete."
        )
    if enrichment_binding_id:
        enrichment_binding = conn.execute(
            """
            SELECT publication_state, payload_sha256, requirement_set_id,
                   project_id, document_id
            FROM requirement_enrichment_publication_bindings
            WHERE binding_id = ?
            """,
            (enrichment_binding_id,),
        ).fetchone()
        if (
            publication_key
            or publication_state != "READY"
            or enrichment_binding is None
            or str(enrichment_binding["publication_state"] or "").upper()
            != "READY"
            or str(enrichment_binding["payload_sha256"] or "")
            != enrichment_binding_sha256
            or str(enrichment_binding["requirement_set_id"] or "")
            != requirement_set_id
            or str(enrichment_binding["project_id"] or "") != project_id
            or str(enrichment_binding["document_id"] or "")
            != sor_document_id
        ):
            raise RequirementSetMutationBlocked(
                "Requirement Set enrichment publication is not READY and verifiable."
            )
    if (publication_key or publication_state) and publication_state != "READY":
        raise RequirementSetMutationBlocked(
            "Requirement Set publication is not READY and cannot be activated."
        )
    if publication_key or publication_state == "READY":
        required_text_fields = (
            "requirement_tree_object_key",
            "requirement_tree_schema_version",
            "structure_snapshot_id",
        )
        missing_fields = [
            field
            for field in required_text_fields
            if not str(target[field] or "").strip()
        ]
        invalid_hash_fields = [
            field
            for field in (
                "requirement_tree_sha256",
                "structure_tree_sha256",
            )
            if re.fullmatch(r"[0-9a-f]{64}", str(target[field] or ""))
            is None
        ]
        if missing_fields or invalid_hash_fields:
            raise RequirementSetMutationBlocked(
                "Requirement Set publication has no complete persisted tree "
                "identity and cannot be activated."
            )
    previous = conn.execute(
        """
        SELECT d.active_requirement_set_id, d.ai_status, d.ai_assessed_at,
               d.ai_requirement_set_version, d.ai_knowledge_base_version,
               p.knowledge_base_version
        FROM sor_documents d
        INNER JOIN projects p ON p.id = d.project_id
        WHERE d.id = ?
        """,
        (sor_document_id,),
    ).fetchone()
    if previous is None:
        return {}
    active_precondition_failed = bool(
        (expected_active_id or strict_semantic_activation)
        and str(previous["active_requirement_set_id"] or "")
        != expected_active_id
    )
    semantic_precondition_failed = bool(
        strict_semantic_activation
        and (
            enrichment_binding_id != expected_binding_id
            or enrichment_binding_sha256 != expected_binding_sha256
        )
    )
    if active_precondition_failed or semantic_precondition_failed:
        raise RequirementSetMutationBlocked(
            "Requirement Set semantic activation precondition failed; the "
            "active version or enrichment publication binding changed."
        )
    if table_assets is not None:
        _replace_document_table_assets_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=sor_document_id,
            requirement_set_id=requirement_set_id,
            assets=table_assets,
            preview_generation_id=preview_generation_id,
        )
    conn.execute("UPDATE requirement_sets SET is_active = 0 WHERE sor_document_id = ?", (sor_document_id,))
    conn.execute(
        "UPDATE requirement_sets SET is_active = 1, activated_at = ? WHERE id = ?",
        (timestamp, requirement_set_id),
    )
    locked_at = str(target["locked_at"] or "") if str(target["status"] or "").upper() == "LOCKED" else ""
    latest_completed = conn.execute(
        """
        SELECT *
        FROM ai_analysis_jobs
        WHERE requirement_set_id = ? AND status = 'COMPLETED'
        ORDER BY finished_at DESC, created_at DESC, rowid DESC
        LIMIT 1
        """,
        (requirement_set_id,),
    ).fetchone()
    latest_attempt = conn.execute(
        """
        SELECT * FROM ai_analysis_jobs
        WHERE requirement_set_id = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (requirement_set_id,),
    ).fetchone()
    stale_reference = conn.execute(
        """
        SELECT 1
        FROM assessments a
        INNER JOIN requirements r ON r.id = a.requirement_id
        WHERE r.requirement_set_id = ? AND a.reference_status = 'STALE_REFERENCE'
        LIMIT 1
        """,
        (requirement_set_id,),
    ).fetchone()
    ai_assessed_at = ""
    ai_requirement_set_version = 0
    ai_knowledge_base_version = 0
    current_projection_matches_target = (
        str(previous["active_requirement_set_id"] or "") == requirement_set_id
        and int(previous["ai_requirement_set_version"] or 0) == int(target["version"] or 0)
        and bool(str(previous["ai_assessed_at"] or "").strip())
    )
    carried_projection_matches_source = False
    if carried_projection_source_id:
        source_projection = conn.execute(
            """
            SELECT version
            FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (
                carried_projection_source_id,
                project_id,
                sor_document_id,
            ),
        ).fetchone()
        carried_projection_matches_source = bool(
            source_projection is not None
            and str(previous["active_requirement_set_id"] or "")
            == carried_projection_source_id
            and int(previous["ai_requirement_set_version"] or 0)
            == int(source_projection["version"] or 0)
            and str(previous["ai_assessed_at"] or "").strip()
            and str(previous["ai_status"] or "").strip().upper()
            in {"READY", "STALE"}
        )
    if latest_completed is not None:
        ai_assessed_at = str(latest_completed["finished_at"] or "")
        ai_requirement_set_version = int(target["version"] or 0)
        ai_knowledge_base_version = int(latest_completed["knowledge_base_version"] or 0)
        ai_status = (
            "READY"
            if ai_knowledge_base_version == int(previous["knowledge_base_version"] or 0)
            else "STALE"
        )
    elif carried_projection_matches_source:
        ai_assessed_at = str(previous["ai_assessed_at"] or "")
        ai_requirement_set_version = int(target["version"] or 0)
        ai_knowledge_base_version = int(
            previous["ai_knowledge_base_version"] or 0
        )
        ai_status = (
            "READY"
            if (
                not carried_ai_projection_requires_analysis
                and str(previous["ai_status"] or "").strip().upper()
                == "READY"
                and ai_knowledge_base_version
                == int(previous["knowledge_base_version"] or 0)
            )
            else "STALE"
        )
    elif current_projection_matches_target:
        ai_assessed_at = str(previous["ai_assessed_at"] or "")
        ai_requirement_set_version = int(previous["ai_requirement_set_version"] or 0)
        ai_knowledge_base_version = int(previous["ai_knowledge_base_version"] or 0)
        ai_status = (
            "READY"
            if ai_knowledge_base_version == int(previous["knowledge_base_version"] or 0)
            else "STALE"
        )
    elif stale_reference is not None:
        ai_status = "STALE"
    else:
        latest_status = str(latest_attempt["status"] or "").upper() if latest_attempt else ""
        ai_status = {
            "FAILED": "FAILED",
            "CANCELLED": "STOPPED",
        }.get(latest_status, "NOT_RUN")
    conn.execute(
        """
        UPDATE sor_documents
        SET active_requirement_set_id = ?, requirement_set_version = ?,
            workflow_stage = ?, extraction_locked_at = ?, ai_status = ?, ai_assessed_at = ?,
            ai_requirement_set_version = ?, ai_knowledge_base_version = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            requirement_set_id,
            int(target["version"] or 0),
            str(workflow_stage or "REVIEWING").strip().upper() or "REVIEWING",
            locked_at,
            ai_status,
            ai_assessed_at,
            ai_requirement_set_version,
            ai_knowledge_base_version,
            timestamp,
            sor_document_id,
        ),
    )
    row = conn.execute("SELECT * FROM requirement_sets WHERE id = ?", (requirement_set_id,)).fetchone()
    return _to_dict(row) or {}


def discard_requirement_set(requirement_set_id: str) -> bool:
    """Delete only an inactive draft created by a failed generation."""

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT project_id, sor_document_id
            FROM requirement_sets
            WHERE id = ?
            """,
            (requirement_set_id,),
        ).fetchone()
    if row is None:
        return False
    return discard_inactive_requirement_set(
        str(row["sor_document_id"] or ""),
        requirement_set_id,
        project_id=str(row["project_id"] or ""),
    )


def fork_requirement_set_for_edit(project_id: str, sor_document_id: str) -> dict[str, Any]:
    """Create and activate a structural draft without carrying final decisions forward."""
    created, _ = fork_requirement_set_for_edit_with_mapping(
        project_id,
        sor_document_id,
    )
    return created


def fork_requirement_set_for_edit_with_mapping(
    project_id: str,
    sor_document_id: str,
    *,
    expected_source_requirement_set_id: str = "",
    verified_source_publication_basis_key: str = "",
    expected_source_tree_sha256: str = "",
    expected_source_enrichment_binding_id: str = "",
    expected_source_enrichment_binding_sha256: str = "",
    derived_classification_snapshot_id: str = "",
    derived_classification_snapshot_sha256: str = "",
    activate: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Fork a set and return the exact old-to-new requirement identity mapping.

    A published source requires the semantic basis key recomputed from its
    already verified tree; without that proof this function fails before the
    INSERT so it cannot activate an unreadable classified draft.
    """

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        created, mapping = _fork_requirement_set_for_edit_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_requirement_set_id=(
                expected_source_requirement_set_id
            ),
            verified_source_publication_basis_key=(
                verified_source_publication_basis_key
            ),
            expected_source_tree_sha256=expected_source_tree_sha256,
            expected_source_enrichment_binding_id=(
                expected_source_enrichment_binding_id
            ),
            expected_source_enrichment_binding_sha256=(
                expected_source_enrichment_binding_sha256
            ),
            derived_classification_snapshot_id=(
                derived_classification_snapshot_id
            ),
            derived_classification_snapshot_sha256=(
                derived_classification_snapshot_sha256
            ),
            activate=activate,
        )
    return created, mapping


def rollback_requirement_set_fork(
    *,
    project_id: str,
    sor_document_id: str,
    source_requirement_set_id: str,
    draft_requirement_set_id: str,
    prior_document_state: dict[str, Any],
) -> bool:
    """Recover atomically when a derived tree object cannot be frozen.

    The target must still be the active DRAFT directly superseding the supplied
    source. This narrow check prevents rollback from deleting later user work.
    """

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute(
            """
            SELECT * FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (draft_requirement_set_id, project_id, sor_document_id),
        ).fetchone()
        source = conn.execute(
            """
            SELECT * FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (source_requirement_set_id, project_id, sor_document_id),
        ).fetchone()
        document = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ? AND project_id = ?",
            (sor_document_id, project_id),
        ).fetchone()
        if (
            draft is None
            or source is None
            or document is None
            or str(draft["status"] or "").upper() != "DRAFT"
            or not bool(int(draft["is_active"] or 0))
            or str(draft["supersedes_id"] or "") != source_requirement_set_id
            or str(document["active_requirement_set_id"] or "")
            != draft_requirement_set_id
        ):
            return False
        conn.execute(
            "DELETE FROM batch_decision_operations WHERE requirement_set_id = ?",
            (draft_requirement_set_id,),
        )
        conn.execute(
            "DELETE FROM requirement_sets WHERE id = ?",
            (draft_requirement_set_id,),
        )
        conn.execute(
            "UPDATE requirement_sets SET is_active = 1 WHERE id = ?",
            (source_requirement_set_id,),
        )
        conn.execute(
            """
            UPDATE sor_documents
            SET active_requirement_set_id = ?,
                requirement_set_version = ?,
                workflow_stage = ?,
                extraction_locked_at = ?,
                ai_status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                str(
                    prior_document_state.get("active_requirement_set_id")
                    or source_requirement_set_id
                ),
                int(
                    prior_document_state.get("requirement_set_version")
                    or source["version"]
                    or 0
                ),
                str(prior_document_state.get("workflow_stage") or "REVIEWING"),
                str(prior_document_state.get("extraction_locked_at") or ""),
                str(prior_document_state.get("ai_status") or "NOT_RUN"),
                str(prior_document_state.get("updated_at") or _now_iso()),
                sor_document_id,
            ),
        )
    return True


def _fork_requirement_set_for_edit_in_conn(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    sor_document_id: str,
    expected_source_requirement_set_id: str = "",
    verified_source_publication_basis_key: str = "",
    expected_source_tree_sha256: str = "",
    expected_source_enrichment_binding_id: str = "",
    expected_source_enrichment_binding_sha256: str = "",
    derived_classification_snapshot_id: str = "",
    derived_classification_snapshot_sha256: str = "",
    activate: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    timestamp = _now_iso()
    locked_document = conn.execute(
        """
        UPDATE sor_documents
        SET updated_at = updated_at
        WHERE id = ? AND project_id = ?
        """,
        (sor_document_id, project_id),
    )
    if locked_document.rowcount != 1:
        return {}, {}
    document = conn.execute(
        "SELECT * FROM sor_documents WHERE id = ? AND project_id = ?",
        (sor_document_id, project_id),
    ).fetchone()
    if document is None:
        return {}, {}
    active_job = _get_active_ai_analysis_job_in_conn(conn, sor_document_id)
    if active_job is not None:
        raise RequirementSetMutationBlocked(
            f"AI analysis job {active_job['id']} is still {active_job['status']}; "
            "stop or finish it before deriving a Requirement Set."
        )
    source = conn.execute(
        "SELECT * FROM requirement_sets WHERE id = ? AND sor_document_id = ?",
        (str(document["active_requirement_set_id"] or ""), sor_document_id),
    ).fetchone()
    if source is None:
        return {}, {}
    source_requirement_set_id = str(source["id"] or "").strip()
    expected_source_id = str(
        expected_source_requirement_set_id or ""
    ).strip()
    if expected_source_id and source_requirement_set_id != expected_source_id:
        raise RequirementSetMutationBlocked(
            "The active Requirement Set changed before the draft was derived."
        )
    expected_tree_sha256 = str(expected_source_tree_sha256 or "").strip().lower()
    source_tree_sha256 = str(source["requirement_tree_sha256"] or "").strip().lower()
    if expected_tree_sha256 and source_tree_sha256 != expected_tree_sha256:
        raise RequirementSetMutationBlocked(
            "The active Requirement Set tree changed before the draft was derived."
        )
    source_publication_key = str(
        source["publication_key"] or ""
    ).strip().lower()
    source_publication_state = str(
        source["publication_state"] or ""
    ).strip().upper()
    source_enrichment_binding_id = str(
        source["enrichment_publication_binding_id"] or ""
    ).strip()
    source_enrichment_binding_sha256 = str(
        source["enrichment_publication_binding_sha256"] or ""
    ).strip().lower()
    if bool(source_enrichment_binding_id) != bool(
        source_enrichment_binding_sha256
    ):
        raise RequirementSetMutationBlocked(
            "The active Requirement Set has incomplete enrichment lineage."
        )
    expected_binding_id = str(
        expected_source_enrichment_binding_id or ""
    ).strip()
    expected_binding_sha256 = str(
        expected_source_enrichment_binding_sha256 or ""
    ).strip().lower()
    if source_enrichment_binding_id and (
        expected_binding_id != source_enrichment_binding_id
        or expected_binding_sha256 != source_enrichment_binding_sha256
    ):
        raise RequirementSetMutationBlocked(
            "The active Requirement Set enrichment binding changed before the "
            "draft was derived."
        )
    classification_snapshot_id = str(
        source["entry_classification_snapshot_id"] or ""
    ).strip()
    classification_snapshot_sha256 = str(
        source["entry_classification_snapshot_sha256"] or ""
    ).strip().lower()
    supplied_classification_id = str(
        derived_classification_snapshot_id or ""
    ).strip()
    supplied_classification_sha256 = str(
        derived_classification_snapshot_sha256 or ""
    ).strip().lower()
    if supplied_classification_id or supplied_classification_sha256:
        if (
            not supplied_classification_id
            or re.fullmatch(r"[0-9a-f]{64}", supplied_classification_sha256)
            is None
        ):
            raise RequirementSetMutationBlocked(
                "The derived Requirement Set classification lineage is invalid."
            )
        if classification_snapshot_id and (
            classification_snapshot_id != supplied_classification_id
            or classification_snapshot_sha256 != supplied_classification_sha256
        ):
            raise RequirementSetMutationBlocked(
                "The derived Requirement Set classification lineage changed."
            )
        classification_snapshot_id = supplied_classification_id
        classification_snapshot_sha256 = supplied_classification_sha256
    if bool(classification_snapshot_id) != bool(classification_snapshot_sha256):
        raise RequirementSetMutationBlocked(
            "The active Requirement Set has incomplete classification lineage."
        )
    verified_base_key = str(
        verified_source_publication_basis_key or ""
    ).strip().lower()
    if source_enrichment_binding_id:
        if (
            source_publication_key
            or source_publication_state != "READY"
            or re.fullmatch(r"[0-9a-f]{64}", verified_base_key) is None
            or not classification_snapshot_id
            or re.fullmatch(r"[0-9a-f]{64}", classification_snapshot_sha256)
            is None
        ):
            raise RequirementSetMutationBlocked(
                "The active enrichment Requirement Set was not fully verified "
                "before deriving a draft."
            )
    elif source_publication_key:
        if (
            source_publication_state != "READY"
            or re.fullmatch(r"[0-9a-f]{64}", source_publication_key) is None
            or re.fullmatch(r"[0-9a-f]{64}", verified_base_key) is None
        ):
            raise RequirementSetMutationBlocked(
                "The active Requirement Set publication lineage was not verified "
                "before deriving a draft."
            )
        acceptable_source_publication_keys = {verified_base_key}
        source_supersedes_id = str(source["supersedes_id"] or "").strip()
        if source_supersedes_id:
            acceptable_source_publication_keys.add(
                derived_requirement_set_publication_key(
                    base_publication_key=verified_base_key,
                    source_requirement_set_id=source_supersedes_id,
                    target_requirement_set_id=source_requirement_set_id,
                )
            )
        if source_publication_key not in acceptable_source_publication_keys:
            raise RequirementSetMutationBlocked(
                "The active Requirement Set publication lineage does not match "
                "the verified source tree."
            )
    elif verified_base_key or classification_snapshot_id:
        raise RequirementSetMutationBlocked(
            "The active Requirement Set has incomplete publication lineage."
        )
    next_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM requirement_sets WHERE sor_document_id = ?",
        (sor_document_id,),
    ).fetchone()
    new_set_id = str(uuid.uuid4())
    next_version = int(next_row["version"] or 1)
    derived_publication_key = (
        derived_requirement_set_publication_key(
            base_publication_key=verified_base_key,
            source_requirement_set_id=source_requirement_set_id,
            target_requirement_set_id=new_set_id,
        )
        if source_publication_key or source_enrichment_binding_id
        else ""
    )
    conn.execute(
        """
        INSERT INTO requirement_sets
        (id, project_id, sor_document_id, version, status, is_active, source_hash,
         preview_generation_id, supersedes_id, created_at, activated_at, locked_at,
         publication_key, publication_state,
         entry_classification_snapshot_id,
         entry_classification_snapshot_sha256)
        VALUES (?, ?, ?, ?, 'DRAFT', 0, ?, ?, ?, ?, '', '', ?, ?, ?, ?)
        """,
        (
            new_set_id, project_id, sor_document_id, next_version,
            str(source["source_hash"] or ""), str(source["preview_generation_id"] or ""),
            source_requirement_set_id, timestamp, derived_publication_key,
            "READY" if derived_publication_key else "",
            classification_snapshot_id, classification_snapshot_sha256,
        ),
    )
    requirement_columns = [
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(requirements)").fetchall()
        if str(row["name"]) not in {
            "id", "requirement_set_id", "created_at", "updated_at",
            "prior_requirement_id", "decision_reference_json",
        }
    ]
    assessment_columns = [
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(assessments)").fetchall()
        if str(row["name"]) not in {
            "id", "requirement_id", "final_decision", "engineer_reason", "updated_at",
            "reference_status", "source_requirement_id",
        }
    ]
    evidence_columns = [
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(evidences)").fetchall()
        if str(row["name"]) not in {"id", "requirement_id"}
    ]
    insert_columns = [
        "id", "requirement_set_id", *requirement_columns, "created_at", "updated_at",
        "prior_requirement_id", "decision_reference_json",
    ]
    mapping: dict[str, str] = {}
    for old in conn.execute(
        "SELECT * FROM requirements WHERE requirement_set_id = ? ORDER BY req_code", (source["id"],)
    ).fetchall():
        old_id = str(old["id"])
        new_id = str(uuid.uuid4())
        mapping[old_id] = new_id
        assessment = conn.execute("SELECT * FROM assessments WHERE requirement_id = ?", (old_id,)).fetchone()
        reference = {
            "requirement_set_id": str(source["id"]),
            "requirement_set_version": int(source["version"] or 0),
            "requirement_id": old_id,
            "final_decision": assessment["final_decision"] if assessment else None,
            "engineer_reason": assessment["engineer_reason"] if assessment else None,
            "decided_at": assessment["updated_at"] if assessment else "",
            "confirmation_status": "PENDING" if assessment and assessment["final_decision"] else "NONE",
        }
        conn.execute(
            f"INSERT INTO requirements ({', '.join(insert_columns)}) VALUES ({', '.join('?' for _ in insert_columns)})",
            [
                new_id, new_set_id, *[old[column] for column in requirement_columns],
                timestamp, timestamp, old_id, json.dumps(reference, ensure_ascii=False),
            ],
        )
        if assessment is not None:
            columns = [
                "id", "requirement_id", *assessment_columns, "final_decision", "engineer_reason",
                "reference_status", "source_requirement_id", "updated_at",
            ]
            conn.execute(
                f"INSERT INTO assessments ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [
                    str(uuid.uuid4()), new_id, *[assessment[column] for column in assessment_columns],
                    None, None, "STALE_REFERENCE", old_id, timestamp,
                ],
            )
        for evidence in conn.execute(
            "SELECT * FROM evidences WHERE requirement_id = ?", (old_id,)
        ).fetchall():
            columns = ["id", "requirement_id", *evidence_columns]
            conn.execute(
                f"INSERT INTO evidences ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [str(uuid.uuid4()), new_id, *[evidence[column] for column in evidence_columns]],
            )
    for asset_row in conn.execute(
        """
        SELECT table_id, asset_json, preview_generation_id
        FROM requirement_set_table_assets
        WHERE requirement_set_id = ?
        """,
        (str(source["id"]),),
    ).fetchall():
        asset = _decode_json_payload(asset_row["asset_json"], default={})
        if not isinstance(asset, dict):
            continue
        asset["requirement_set_id"] = new_set_id
        conn.execute(
            """
            INSERT INTO requirement_set_table_assets
            (id, project_id, sor_document_id, requirement_set_id, table_id, asset_json,
             preview_generation_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), project_id, sor_document_id, new_set_id,
                str(asset_row["table_id"]), json.dumps(asset, ensure_ascii=False),
                str(asset_row["preview_generation_id"] or ""), timestamp, timestamp,
            ),
        )
    if activate:
        conn.execute(
            "UPDATE requirement_sets SET is_active = 0 WHERE sor_document_id = ?",
            (sor_document_id,),
        )
        conn.execute(
            "UPDATE requirement_sets SET is_active = 1, activated_at = ? WHERE id = ?",
            (timestamp, new_set_id),
        )
        current_ai_status = str(document["ai_status"] or "NOT_RUN").upper()
        has_ai = (
            bool(str(document["ai_assessed_at"] or "").strip())
            or current_ai_status == "READY"
        )
        conn.execute(
            """
            UPDATE sor_documents
            SET active_requirement_set_id = ?, requirement_set_version = ?,
                workflow_stage = 'REVIEWING', extraction_locked_at = '',
                ai_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_set_id,
                next_version,
                "STALE" if has_ai else "NOT_RUN",
                timestamp,
                sor_document_id,
            ),
        )
    created = conn.execute("SELECT * FROM requirement_sets WHERE id = ?", (new_set_id,)).fetchone()
    return _to_dict(created) or {}, mapping


def backfill_sor_document_runtime(document_id: str) -> dict[str, Any]:
    updated_at = _now_iso()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return {}
        requirement_rows = conn.execute(
            """
            SELECT COUNT(*) AS count FROM requirements
            WHERE sor_document_id = ? AND requirement_set_id = ?
            """,
            (document_id, str(row["active_requirement_set_id"] or "")),
        ).fetchone()
        requirement_count = int(requirement_rows["count"] or 0) if requirement_rows else 0
        if requirement_count <= 0:
            return _to_dict(row) or {}

        assessment_rows = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM assessments a
            INNER JOIN requirements r ON r.id = a.requirement_id
            WHERE r.sor_document_id = ? AND r.requirement_set_id = ?
            """,
            (document_id, str(row["active_requirement_set_id"] or "")),
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
    """Compatibility helper for legacy callers.

    Version numbers are now allocated by create/fork_requirement_set.  This method
    only synchronizes document runtime state with the active set and never mutates
    historical sets.
    """
    updated_at = _now_iso()
    stage = str(workflow_stage or "EXTRACTED").strip().upper() or "EXTRACTED"
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT d.*, rs.version AS active_version, rs.id AS active_set_id,
                   rs.status AS active_set_status
            FROM sor_documents d
            LEFT JOIN requirement_sets rs ON rs.id = d.active_requirement_set_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            return {}
        if str(row["active_set_status"] or "").strip().upper() == "LOCKED":
            raise PermissionError("Locked requirement sets are immutable; create a draft version before editing.")
        next_version = int(row["active_version"] or row["requirement_set_version"] or 0)
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


def lock_sor_document_extraction(
    document_id: str,
    *,
    waiver_reason: str = "",
    expected_requirement_set_id: str = "",
    expected_matrix_mutable_state_sha256: str = "",
) -> dict[str, Any]:
    from reqpilotx_server.adapters.persistence.requirement_matrix_states import (
        lock_requirement_matrix_sets_in_connection,
        requirement_matrix_mutable_state_sha256_in_connection,
    )

    timestamp = _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        scope = conn.execute(
            "SELECT project_id FROM sor_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if scope is None or not _lock_requirement_set_document_in_conn(
            conn,
            project_id=str(scope["project_id"] or ""),
            sor_document_id=document_id,
        ):
            return {}
        document = conn.execute(
            """
            SELECT project_id, active_requirement_set_id, preview_status,
                   preview_generation_id
            FROM sor_documents WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
        expected_set_id = str(expected_requirement_set_id or "").strip()
        actual_set_id = (
            str(document["active_requirement_set_id"] or "").strip()
            if document is not None
            else ""
        )
        if expected_set_id and actual_set_id != expected_set_id:
            raise RequirementSetMutationBlocked(
                "The selected Requirement Set changed before lock; reload and retry."
            )
        expected_mutable_sha256 = str(
            expected_matrix_mutable_state_sha256 or ""
        ).strip()
        if expected_mutable_sha256 and re.fullmatch(
            r"[0-9a-f]{64}", expected_mutable_sha256
        ) is None:
            raise RequirementSetMutationBlocked(
                "The Requirement Matrix lock precondition is invalid."
            )
        if document is not None and actual_set_id:
            lock_requirement_matrix_sets_in_connection(
                conn,
                project_id=str(document["project_id"] or ""),
                document_id=document_id,
                requirement_set_ids=(actual_set_id,),
            )
            if expected_mutable_sha256:
                actual_mutable_sha256 = (
                    requirement_matrix_mutable_state_sha256_in_connection(
                        conn,
                        project_id=str(document["project_id"] or ""),
                        document_id=document_id,
                        requirement_set_id=actual_set_id,
                    )
                )
                if actual_mutable_sha256 != expected_mutable_sha256:
                    raise RequirementSetMutationBlocked(
                        "The Requirement Matrix changed before lock; reload and retry."
                    )
            updated_set = conn.execute(
                """
                UPDATE requirement_sets
                SET status = 'LOCKED', locked_at = ?, lock_waiver_reason = ?,
                    preview_status_at_lock = ?,
                    preview_generation_id = CASE
                        WHEN COALESCE(preview_generation_id, '') = '' THEN ?
                        ELSE preview_generation_id
                    END
                WHERE id = ? AND project_id = ? AND sor_document_id = ?
                  AND status = 'DRAFT' AND is_active = 1
                """,
                (
                    timestamp,
                    str(waiver_reason or "").strip(),
                    str(document["preview_status"] or "").strip().upper(),
                    str(document["preview_generation_id"] or "").strip(),
                    actual_set_id,
                    str(document["project_id"] or ""),
                    document_id,
                ),
            )
            if updated_set.rowcount != 1:
                raise RequirementSetMutationBlocked(
                    "The Requirement Set is no longer an active DRAFT."
                )
        else:
            raise RequirementSetMutationBlocked(
                "No active Requirement Set is available to lock."
            )
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


def update_sor_document_ai_state_for_job(
    job_id: str,
    *,
    ai_status: str,
    ai_assessed_at: str = "",
    ai_requirement_set_version: int | None = None,
    ai_knowledge_base_version: int | None = None,
    workflow_stage: str = "",
    expected_requirement_set_version: int | None = None,
    expected_knowledge_base_version: int | None = None,
) -> dict[str, Any]:
    """Project a job state only while that job still owns the active set.

    Job rows remain valid audit history after a Requirement Set switch.  A
    late worker must still be allowed to mark its own job failed/cancelled, but
    it must never rewrite the document projection that belongs to another set
    or a newer job.
    """

    updated_at = _now_iso()
    status = str(ai_status or "NOT_RUN").strip().upper() or "NOT_RUN"
    stage = str(workflow_stage or "").strip().upper()
    with get_conn() as conn:
        job = conn.execute(
            "SELECT rowid AS job_rowid, * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            return {}
        current = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ?",
            (job["sor_document_id"],),
        ).fetchone()
        if current is None:
            return {}
        cursor = conn.execute(
            """
            UPDATE sor_documents
            SET ai_status = ?,
                ai_assessed_at = ?,
                ai_requirement_set_version = ?,
                ai_knowledge_base_version = ?,
                workflow_stage = ?,
                updated_at = ?
            WHERE id = ?
              AND COALESCE(active_requirement_set_id, '') = ?
              AND (? IS NULL OR requirement_set_version = ?)
              AND (
                    ? IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM projects current_project
                        WHERE current_project.id = sor_documents.project_id
                          AND current_project.knowledge_base_version = ?
                    )
              )
              AND NOT EXISTS (
                    SELECT 1 FROM ai_analysis_jobs newer
                    WHERE newer.sor_document_id = sor_documents.id
                      AND newer.rowid > ?
              )
            """,
            (
                status,
                ai_assessed_at if status == "READY" else str(current["ai_assessed_at"] or ai_assessed_at or ""),
                int(
                    ai_requirement_set_version
                    if ai_requirement_set_version is not None
                    else current["ai_requirement_set_version"] or 0
                ),
                int(
                    ai_knowledge_base_version
                    if ai_knowledge_base_version is not None
                    else current["ai_knowledge_base_version"] or 0
                ),
                stage or str(current["workflow_stage"] or ""),
                updated_at,
                job["sor_document_id"],
                str(job["requirement_set_id"] or ""),
                expected_requirement_set_version,
                expected_requirement_set_version,
                expected_knowledge_base_version,
                expected_knowledge_base_version,
                int(job["job_rowid"]),
            ),
        )
        if cursor.rowcount != 1:
            return {}
        row = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ?",
            (job["sor_document_id"],),
        ).fetchone()
    return _to_dict(row) or {}


def update_sor_document_preview_state(
    document_id: str,
    *,
    preview_status: str | None = None,
    preview_generated_at: str | None = None,
    preview_file_mtime: str | None = None,
    preview_error: str | None = None,
    preview_generation_id: str | None = None,
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
                preview_generation_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                str(preview_status if preview_status is not None else current["preview_status"] or "NOT_READY"),
                str(preview_generated_at if preview_generated_at is not None else current["preview_generated_at"] or ""),
                str(preview_file_mtime if preview_file_mtime is not None else current["preview_file_mtime"] or ""),
                str(preview_error if preview_error is not None else current["preview_error"] or ""),
                str(preview_generation_id if preview_generation_id is not None else current["preview_generation_id"] or ""),
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
    authority: str = "UNKNOWN",
    claim_role: str = "UNKNOWN",
    applicability: str = "UNKNOWN",
    source_sha256: str = "",
    source_size: int = 0,
    declared_extension: str = "",
    detected_format: str = "",
    preflight_schema_version: str = "",
    media_type: str = "application/octet-stream",
) -> dict[str, Any]:
    document_id = str(uuid.uuid4())
    created_at = _now_iso()
    with get_conn() as conn:
        if is_postgresql_runtime():
            _register_stored_object_in_conn(
                conn,
                storage_key=file_path,
                sha256=source_sha256,
                size_bytes=source_size,
                media_type=media_type,
                object_kind="knowledge-document",
            )
        conn.execute(
            """
            INSERT INTO knowledge_documents
            (id, project_id, file_name, file_type, file_path, extracted_text,
             source_sha256, source_size, declared_extension, detected_format, preflight_schema_version,
             doc_type, doc_version, status, effective_from, effective_to,
             authority, claim_role, applicability,
             retrieval_sync_status, retrieval_sync_error, retrieval_synced_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', '', '', ?)
            """,
            (
                document_id,
                project_id,
                file_name,
                file_type,
                file_path,
                extracted_text,
                str(source_sha256 or ""),
                max(int(source_size or 0), 0),
                str(declared_extension or "").lower(),
                str(detected_format or "").lower(),
                str(preflight_schema_version or ""),
                doc_type,
                doc_version,
                status,
                effective_from,
                effective_to,
                str(authority or "UNKNOWN").strip().upper(),
                str(claim_role or "UNKNOWN").strip().upper(),
                str(applicability or "UNKNOWN").strip(),
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


def get_knowledge_document(document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)
        ).fetchone()
    return _to_dict(row)


def update_knowledge_document_content(
    document_id: str,
    *,
    file_type: str,
    extracted_text: str,
) -> dict[str, Any]:
    with get_conn() as conn:
        current = conn.execute(
            "SELECT * FROM knowledge_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if current is None:
            return {}
        conn.execute(
            """
            UPDATE knowledge_documents
            SET file_type = ?,
                extracted_text = ?
            WHERE id = ?
            """,
            (
                str(file_type or "").strip().upper(),
                str(extracted_text or ""),
                document_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM knowledge_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    return _to_dict(row) or {}


def delete_knowledge_document(document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM knowledge_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
    return _to_dict(row)


def delete_sor_document(document_id: str) -> dict[str, Any] | None:
    """Delete a SOR document and all its cascading records (requirements, table assets, AI jobs)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sor_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        row_payload = _to_dict(row) or {}
        released_source_asset_keys: tuple[str, ...] = ()
        released_result_artifact_keys: tuple[str, ...] = ()
        released_source_asset_keys = tuple(
            sorted(
                {
                    str(item["storage_key"])
                    for item in conn.execute(
                        """
                        SELECT asset_ref.storage_key
                        FROM source_capture_asset_references AS asset_ref
                        JOIN source_capture_bindings AS binding
                          ON binding.capture_id = asset_ref.capture_id
                        WHERE binding.sor_document_id = ?
                        UNION
                        SELECT storage_key
                        FROM source_capture_asset_staging_references
                        WHERE sor_document_id = ?
                        """,
                        (document_id, document_id),
                    ).fetchall()
                }
            )
        )
        if is_postgresql_runtime():
            released_source_asset_keys = tuple(
                sorted(
                    set(released_source_asset_keys)
                    | {
                        str(item["storage_key"])
                        for item in conn.execute(
                            """
                            SELECT storage_key
                            FROM source_asset_references
                            WHERE document_id = ?
                            UNION
                            SELECT storage_key
                            FROM source_asset_staging_references
                            WHERE document_id = ?
                            UNION
                            SELECT storage_key
                            FROM document_extraction_page_staging_references
                            WHERE document_id = ?
                            """,
                            (document_id, document_id, document_id),
                        ).fetchall()
                    }
                )
            )
            released_result_artifact_keys = tuple(
                sorted(
                    {
                        str(item["storage_key"])
                        for item in conn.execute(
                            """
                            SELECT storage_key
                            FROM extraction_result_artifact_references
                            WHERE document_id = ?
                            UNION
                            SELECT storage_key
                            FROM extraction_result_artifact_staging_references
                            WHERE document_id = ?
                            UNION
                            SELECT new_storage_key AS storage_key
                            FROM extraction_result_artifact_cas_operations
                            WHERE document_id = ?
                            """,
                            (document_id, document_id, document_id),
                        ).fetchall()
                    }
                )
            )
        # SQLite backends own direct application files. PostgreSQL stores only
        # object identities; the application deletes bytes after commit.
        file_path = str(row_payload.get("file_path") or "")
        if not is_postgresql_runtime() and file_path:
            try:
                Path(file_path).unlink(missing_ok=True)
            except OSError:
                pass
        # Cascading deletes (foreign keys may not be enabled, so do manually)
        reading_capture_rows = conn.execute(
            """
            SELECT capture_id
            FROM source_capture_bindings
            WHERE sor_document_id = ?
            """,
            (document_id,),
        ).fetchall()
        reading_capture_ids = tuple(
            sorted({str(item["capture_id"]) for item in reading_capture_rows})
        )
        conn.execute(
            "DELETE FROM reading_artifacts WHERE sor_document_id = ?",
            (document_id,),
        )
        conn.execute(
            "DELETE FROM reading_notes WHERE sor_document_id = ?",
            (document_id,),
        )
        conn.execute(
            "DELETE FROM reading_sessions WHERE sor_document_id = ?",
            (document_id,),
        )
        visual_generation_rows = conn.execute(
            """
            SELECT generation_id
            FROM source_visual_capture_generations
            WHERE sor_document_id = ?
            """,
            (document_id,),
        ).fetchall()
        visual_generation_ids = tuple(
            sorted({str(item["generation_id"]) for item in visual_generation_rows})
        )
        for generation_id in visual_generation_ids:
            conn.execute(
                "DELETE FROM source_visual_page_attempts WHERE generation_id = ?",
                (generation_id,),
            )
            conn.execute(
                "DELETE FROM source_visual_page_snapshots WHERE generation_id = ?",
                (generation_id,),
            )
        conn.execute(
            "DELETE FROM source_visual_capture_bindings WHERE sor_document_id = ?",
            (document_id,),
        )
        conn.execute(
            "DELETE FROM source_visual_capture_generations WHERE sor_document_id = ?",
            (document_id,),
        )
        conn.execute(
            "DELETE FROM source_capture_bindings WHERE sor_document_id = ?",
            (document_id,),
        )
        for capture_id in reading_capture_ids:
            conn.execute(
                """
                DELETE FROM source_capture_snapshots
                WHERE capture_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM source_capture_bindings
                    WHERE source_capture_bindings.capture_id = ?
                  )
                """,
                (capture_id, capture_id),
            )
        conn.execute("DELETE FROM ai_analysis_jobs WHERE sor_document_id = ?", (document_id,))
        conn.execute("DELETE FROM ai_analysis_events WHERE sor_document_id = ?", (document_id,))
        conn.execute("DELETE FROM requirement_set_table_assets WHERE sor_document_id = ?", (document_id,))
        conn.execute("DELETE FROM document_table_assets WHERE sor_document_id = ?", (document_id,))
        conn.execute("DELETE FROM requirements WHERE sor_document_id = ?", (document_id,))
        conn.execute("DELETE FROM sor_documents WHERE id = ?", (document_id,))
        if released_source_asset_keys:
            row_payload["_released_source_asset_storage_keys"] = list(
                released_source_asset_keys
            )
        if released_result_artifact_keys:
            row_payload["_released_extraction_result_artifact_storage_keys"] = list(
                released_result_artifact_keys
            )
    return row_payload


def release_stored_object_if_unreferenced(storage_key: str) -> bool:
    if not is_postgresql_runtime():
        return False
    normalized = str(storage_key or "").strip()
    if not normalized:
        return False
    with get_conn() as conn:
        for table in ("sor_documents", "knowledge_documents", "export_jobs"):
            row = conn.execute(
                f"SELECT 1 AS present FROM {table} WHERE file_path = ? LIMIT 1",
                (normalized,),
            ).fetchone()
            if row is not None:
                return False
        sync_row = conn.execute(
            """
            SELECT 1 AS present
            FROM knowledge_document_syncs
            WHERE payload_path = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if sync_row is not None:
            return False
        cursor = conn.execute(
            "DELETE FROM stored_objects WHERE storage_key = ?",
            (normalized,),
        )
        return cursor.rowcount == 1

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
    payload_identity: str | None = str(payload_path or "").strip() or None
    if not is_postgresql_runtime():
        payload_identity = payload_identity or ""
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
                    payload_identity,
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
                    payload_identity,
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


def discard_inactive_requirement_set(
    sor_document_id: str,
    requirement_set_id: str,
    *,
    project_id: str = "",
    expected_publication_state: str = "",
    publication_owner_token: str = "",
    now_utc: datetime | None = None,
) -> bool:
    """Atomically remove one inactive draft; never clear active/history sets."""

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            document = conn.execute(
                "SELECT project_id FROM sor_documents WHERE id = ?",
                (sor_document_id,),
            ).fetchone()
            normalized_project_id = str(
                document["project_id"] if document is not None else ""
            ).strip()
        if not normalized_project_id:
            return False
        if not _lock_requirement_set_document_in_conn(
            conn,
            project_id=normalized_project_id,
            sor_document_id=sor_document_id,
        ):
            return False
        from reqpilotx_server.adapters.persistence.requirement_matrix_states import (
            lock_requirement_matrix_sets_in_connection,
        )

        lock_requirement_matrix_sets_in_connection(
            conn,
            project_id=normalized_project_id,
            document_id=sor_document_id,
            requirement_set_ids=(requirement_set_id,),
        )
        conn.execute(
            """
            UPDATE requirement_sets
            SET publication_state = publication_state
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (
                requirement_set_id,
                normalized_project_id,
                sor_document_id,
            ),
        )
        target = conn.execute(
            """
            SELECT status, is_active, activated_at, publication_key,
                   publication_state,
                   publication_owner_token, publication_lease_expires_at,
                   publication_heartbeat_at,
                   requirement_tree_object_key, requirement_tree_sha256,
                   enrichment_publication_binding_id,
                   enrichment_publication_binding_sha256
            FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
              AND status = 'DRAFT' AND is_active = 0
              AND COALESCE(activated_at, '') = ''
            """,
            (
                requirement_set_id,
                normalized_project_id,
                sor_document_id,
            ),
        ).fetchone()
        if target is None:
            return False
        normalized_owner_token = str(publication_owner_token or "").strip()
        if normalized_owner_token and not _REQUIREMENT_SET_PUBLICATION_OWNER_RE.fullmatch(
            normalized_owner_token
        ):
            raise ValueError("publication owner token is invalid")
        normalized_expected_state = str(
            expected_publication_state or ""
        ).strip().upper()
        if normalized_expected_state and (
            str(target["publication_state"] or "").strip().upper()
            != normalized_expected_state
            or str(target["enrichment_publication_binding_id"] or "").strip()
            or str(target["enrichment_publication_binding_sha256"] or "").strip()
        ):
            return False
        fenced_publication = bool(
            str(target["publication_key"] or "").strip()
            and str(target["publication_state"] or "").strip().upper()
            == "PUBLISHING"
        )
        if fenced_publication:
            current_time = _requirement_set_publication_now_utc(now_utc)
            if (
                not normalized_owner_token
                or str(target["publication_owner_token"] or "").strip()
                != normalized_owner_token
                or not str(target["publication_heartbeat_at"] or "").strip()
                or not str(
                    target["publication_lease_expires_at"] or ""
                ).strip()
                or _publication_lease_is_expired(
                    target["publication_lease_expires_at"],
                    now_utc=current_time,
                )
                or str(target["requirement_tree_object_key"] or "").strip()
                or str(target["requirement_tree_sha256"] or "").strip()
            ):
                return False
        elif normalized_owner_token:
            return False
        _delete_requirement_set_requirement_rows_in_conn(
            conn,
            sor_document_id=sor_document_id,
            requirement_set_id=requirement_set_id,
        )
        deleted = conn.execute(
            """
            DELETE FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
              AND status = 'DRAFT' AND is_active = 0
              AND COALESCE(activated_at, '') = ''
              AND (? = '' OR publication_owner_token = ?)
            """,
            (
                requirement_set_id,
                normalized_project_id,
                sor_document_id,
                normalized_owner_token,
                normalized_owner_token,
            ),
        )
    return deleted.rowcount == 1


def _delete_requirement_set_requirement_rows_in_conn(
    conn: sqlite3.Connection,
    *,
    sor_document_id: str,
    requirement_set_id: str,
) -> None:
    req_rows = conn.execute(
        """
        SELECT id FROM requirements
        WHERE sor_document_id = ? AND requirement_set_id = ?
        """,
        (sor_document_id, requirement_set_id),
    ).fetchall()
    req_ids = [row["id"] for row in req_rows]
    if req_ids:
        placeholders = ", ".join("?" for _ in req_ids)
        conn.execute(
            f"DELETE FROM review_logs WHERE requirement_id IN ({placeholders})",
            req_ids,
        )
        conn.execute(
            f"DELETE FROM evidences WHERE requirement_id IN ({placeholders})",
            req_ids,
        )
        conn.execute(
            f"DELETE FROM assessments WHERE requirement_id IN ({placeholders})",
            req_ids,
        )
        conn.execute(
            f"DELETE FROM assessment_runs WHERE requirement_id IN ({placeholders})",
            req_ids,
        )
    conn.execute(
        """
        DELETE FROM requirements
        WHERE sor_document_id = ? AND requirement_set_id = ?
        """,
        (sor_document_id, requirement_set_id),
    )


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
    requirement_set_id: str = "",
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
    preview_generation_id: str = "",
    requirement_id: str = "",
    publication_owner_token: str = "",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    normalized_requirement_id = str(requirement_id or uuid.uuid4()).strip()
    if not normalized_requirement_id:
        raise ValueError("requirement_id must not be blank")
    normalized_owner_token = str(publication_owner_token or "").strip()
    if normalized_owner_token and not _REQUIREMENT_SET_PUBLICATION_OWNER_RE.fullmatch(
        normalized_owner_token
    ):
        raise ValueError("publication owner token is invalid")
    timestamp = _now_iso()
    with get_conn() as conn:
        set_id = str(requirement_set_id or "").strip()
        if not set_id:
            active = conn.execute(
                """
                SELECT rs.id FROM requirement_sets rs
                INNER JOIN sor_documents d ON d.id = rs.sor_document_id
                WHERE rs.sor_document_id = ?
                  AND (rs.id = d.active_requirement_set_id OR rs.is_active = 1)
                ORDER BY (rs.id = d.active_requirement_set_id) DESC, rs.version DESC LIMIT 1
                """,
                (sor_document_id,),
            ).fetchone()
            if active is not None:
                set_id = str(active["id"])
            else:
                document = conn.execute(
                    "SELECT * FROM sor_documents WHERE id = ? AND project_id = ?",
                    (sor_document_id, project_id),
                ).fetchone()
                if document is None:
                    return {}
                set_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO requirement_sets
                    (id, project_id, sor_document_id, version, status, is_active, source_hash,
                     preview_generation_id, supersedes_id, created_at, activated_at, locked_at)
                    VALUES (?, ?, ?, 1, 'DRAFT', 1, ?, ?, '', ?, ?, '')
                    """,
                    (
                        set_id,
                        project_id,
                        sor_document_id,
                        _document_source_hash(document),
                        str(document["preview_generation_id"] or ""),
                        timestamp,
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    UPDATE sor_documents
                    SET active_requirement_set_id = ?, requirement_set_version = 1,
                        workflow_stage = 'REVIEWING', updated_at = ?
                    WHERE id = ?
                    """,
                    (set_id, timestamp, sor_document_id),
                )
        if normalized_owner_token:
            locked = conn.execute(
                """
                UPDATE requirement_sets
                SET publication_state = publication_state
                WHERE id = ? AND publication_owner_token = ?
                """,
                (set_id, normalized_owner_token),
            )
            if locked.rowcount != 1:
                raise RequirementSetMutationBlocked(
                    "Requirement Set publication ownership changed before row write."
                )
        set_row = conn.execute(
            """
            SELECT status, publication_key, publication_state,
                   publication_owner_token, publication_heartbeat_at,
                   publication_lease_expires_at
            FROM requirement_sets WHERE id = ?
            """,
            (set_id,),
        ).fetchone()
        if set_row is None:
            raise LookupError("requirement_set_not_found")
        if str(set_row["status"] or "").strip().upper() == "LOCKED":
            raise PermissionError("Locked requirement sets are immutable; create a draft version before editing.")
        fenced_publication = bool(
            str(set_row["publication_key"] or "").strip()
            and str(set_row["publication_state"] or "").strip().upper()
            == "PUBLISHING"
        )
        if fenced_publication:
            current_time = _requirement_set_publication_now_utc(now_utc)
            if (
                not normalized_owner_token
                or str(
                    set_row["publication_owner_token"] or ""
                ).strip()
                != normalized_owner_token
                or not str(
                    set_row["publication_heartbeat_at"] or ""
                ).strip()
                or not str(
                    set_row["publication_lease_expires_at"] or ""
                ).strip()
                or _publication_lease_is_expired(
                    set_row["publication_lease_expires_at"],
                    now_utc=current_time,
                )
            ):
                raise RequirementSetMutationBlocked(
                    "Requirement Set publication lease is missing, expired, or "
                    "owned by another publisher."
                )
        elif normalized_owner_token:
            raise RequirementSetMutationBlocked(
                "Requirement Set publication owner no longer owns an incomplete draft."
            )
        conn.execute(
            """
            INSERT INTO requirements
            (id, project_id, sor_document_id, requirement_set_id, req_code, source_page, source_anchor,
             requirement_text, requirement_type, priority, status,
             entry_kind, text_relation, is_deduped, parent_section_code, parent_section_title, parent_section_label,
             heading_text, raw_text, display_text,
             merged_from_req_codes, merged_from_entry_ids, dedupe_version,
             table_refs, content_blocks, has_table_asset, table_render_mode, preview_generation_id,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_requirement_id,
                project_id,
                sor_document_id,
                set_id,
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
                preview_generation_id,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM requirements WHERE id = ?",
            (normalized_requirement_id,),
        ).fetchone()
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
    preview_generation_id: str | None = None,
) -> dict[str, Any]:
    updated_at = _now_iso()
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM requirements WHERE id = ?", (requirement_id,)).fetchone()
        if current is None:
            return {}
        _require_requirement_mutable(conn, requirement_id)
        conn.execute(
            """
            UPDATE requirements
            SET source_page = ?, source_anchor = ?, requirement_text = ?, requirement_type = ?, priority = ?,
                entry_kind = ?, text_relation = ?, is_deduped = ?, parent_section_code = ?, parent_section_title = ?,
                parent_section_label = ?, heading_text = ?, raw_text = ?, display_text = ?,
                merged_from_req_codes = ?, merged_from_entry_ids = ?, dedupe_version = ?,
                table_refs = ?, content_blocks = ?, has_table_asset = ?, table_render_mode = ?, preview_generation_id = ?, updated_at = ?
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
                str(preview_generation_id if preview_generation_id is not None else current["preview_generation_id"] or ""),
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
        if row is None:
            return
        _require_requirement_mutable(conn, requirement_id)
        conn.execute("DELETE FROM review_logs WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM evidences WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM assessments WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM assessment_runs WHERE requirement_id = ?", (requirement_id,))
        conn.execute("DELETE FROM requirements WHERE id = ?", (requirement_id,))


def list_requirements(
    project_id: str,
    sor_document_id: str | None = None,
    requirement_set_id: str | None = None,
) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if requirement_set_id:
            rows = conn.execute(
                """
                  SELECT r.*, a.profile_id, a.provider, a.retrieval_backend, a.ai_decision, a.compliance_status, a.ai_reason, a.ai_confidence, a.assessment_mode, a.analysis_label,
                       a.analysis_basis,
                      a.normalized_requirement, a.risk_level, a.clarification_question, a.model_name, a.prompt_version,
                       a.evidence_findings_json, a.structured_information_gap_json, a.sub_judgments_json,
                       a.final_decision, a.engineer_reason, a.reference_status, a.source_requirement_id,
                       a.result_origin, a.source_assessment_id, a.impact_receipt_id,
                       a.analysis_input_schema_version, a.analysis_input_sha256,
                       a.updated_at AS assessment_updated_at
                FROM requirements r
                LEFT JOIN assessments a ON a.requirement_id = r.id
                WHERE r.project_id = ? AND r.requirement_set_id = ?
                ORDER BY r.req_code
                """,
                (project_id, requirement_set_id),
            ).fetchall()
        elif sor_document_id:
            rows = conn.execute(
                """
                  SELECT r.*, a.profile_id, a.provider, a.retrieval_backend, a.ai_decision, a.compliance_status, a.ai_reason, a.ai_confidence, a.assessment_mode, a.analysis_label,
                       a.analysis_basis,
                      a.normalized_requirement, a.risk_level, a.clarification_question, a.model_name, a.prompt_version,
                       a.evidence_findings_json, a.structured_information_gap_json, a.sub_judgments_json,
                       a.final_decision, a.engineer_reason, a.reference_status, a.source_requirement_id,
                       a.result_origin, a.source_assessment_id, a.impact_receipt_id,
                       a.analysis_input_schema_version, a.analysis_input_sha256,
                       a.updated_at AS assessment_updated_at
                FROM requirements r
                LEFT JOIN assessments a ON a.requirement_id = r.id
                INNER JOIN sor_documents d ON d.id = r.sor_document_id
                WHERE r.project_id = ? AND r.sor_document_id = ?
                  AND r.requirement_set_id = d.active_requirement_set_id
                ORDER BY r.req_code
                """,
                (project_id, sor_document_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                  SELECT r.*, a.profile_id, a.provider, a.retrieval_backend, a.ai_decision, a.compliance_status, a.ai_reason, a.ai_confidence, a.assessment_mode, a.analysis_label,
                       a.analysis_basis,
                      a.normalized_requirement, a.risk_level, a.clarification_question, a.model_name, a.prompt_version,
                       a.evidence_findings_json, a.structured_information_gap_json, a.sub_judgments_json,
                       a.final_decision, a.engineer_reason, a.reference_status, a.source_requirement_id,
                       a.result_origin, a.source_assessment_id, a.impact_receipt_id,
                       a.analysis_input_schema_version, a.analysis_input_sha256,
                       a.updated_at AS assessment_updated_at
                FROM requirements r
                LEFT JOIN assessments a ON a.requirement_id = r.id
                INNER JOIN sor_documents d ON d.id = r.sor_document_id
                WHERE r.project_id = ? AND r.requirement_set_id = d.active_requirement_set_id
                ORDER BY r.req_code
                """,
                (project_id,),
            ).fetchall()

        output: list[dict[str, Any]] = []
        for row in rows:
            item = _normalize_requirement_record(_to_dict(row) or {})
            evidence_rows = conn.execute(
                """
                SELECT e.id, e.source_doc_id, e.source_doc_name, e.source_doc_version,
                       e.retrieval_evidence_id, e.content_hash, e.evidence_contract_version,
                       e.content_kind, e.evidence_metadata_json,
                       e.source_location, e.excerpt, e.relevance,
                       COALESCE(k.status, 'UNKNOWN') AS source_doc_status,
                       COALESCE(k.effective_from, '') AS source_effective_from,
                       COALESCE(k.effective_to, '') AS source_effective_to,
                       COALESCE(k.effective_from, '') AS effective_from,
                       COALESCE(k.effective_to, '') AS effective_to
                FROM evidences e
                LEFT JOIN knowledge_documents k ON k.id = e.source_doc_id
                WHERE e.requirement_id = ?
                ORDER BY e.relevance DESC
                """,
                (row["id"],),
            ).fetchall()
            item["evidences"] = [
                _normalize_evidence_record(_to_dict(evidence) or {})
                for evidence in evidence_rows
            ]
            if item.get("assessment_reference_status") == "STALE_REFERENCE":
                for evidence in item["evidences"]:
                    evidence["reference_status"] = "STALE_REFERENCE"
                    evidence["source_requirement_id"] = item.get("assessment_source_requirement_id")
            output.append(item)
    return output


def get_requirement(requirement_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
                 SELECT r.*, a.profile_id, a.provider, a.retrieval_backend, a.ai_decision, a.compliance_status, a.ai_reason, a.ai_confidence, a.assessment_mode, a.analysis_label,
                   a.analysis_basis,
                     a.normalized_requirement, a.risk_level, a.clarification_question, a.model_name, a.prompt_version,
                   a.evidence_findings_json, a.structured_information_gap_json, a.sub_judgments_json,
                   a.final_decision, a.engineer_reason, a.reference_status, a.source_requirement_id,
                   a.result_origin, a.source_assessment_id, a.impact_receipt_id,
                   a.analysis_input_schema_version, a.analysis_input_sha256,
                   a.updated_at AS assessment_updated_at
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
            SELECT e.id, e.source_doc_id, e.source_doc_name, e.source_doc_version,
                   e.retrieval_evidence_id, e.content_hash, e.evidence_contract_version,
                   e.content_kind, e.evidence_metadata_json,
                   e.source_location, e.excerpt, e.relevance,
                   COALESCE(k.status, 'UNKNOWN') AS source_doc_status,
                   COALESCE(k.effective_from, '') AS source_effective_from,
                   COALESCE(k.effective_to, '') AS source_effective_to,
                   COALESCE(k.effective_from, '') AS effective_from,
                   COALESCE(k.effective_to, '') AS effective_to
            FROM evidences e
            LEFT JOIN knowledge_documents k ON k.id = e.source_doc_id
            WHERE e.requirement_id = ?
            ORDER BY e.relevance DESC
            """,
            (requirement_id,),
        ).fetchall()
        item["evidences"] = [
            _normalize_evidence_record(_to_dict(evidence) or {})
            for evidence in evidence_rows
        ]
        if item.get("assessment_reference_status") == "STALE_REFERENCE":
            for evidence in item["evidences"]:
                evidence["reference_status"] = "STALE_REFERENCE"
                evidence["source_requirement_id"] = item.get("assessment_source_requirement_id")
    return item


def get_evidence(evidence_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT e.*, r.project_id, r.sor_document_id, r.requirement_set_id,
                   a.reference_status, a.source_requirement_id
            FROM evidences e
            INNER JOIN requirements r ON r.id = e.requirement_id
            LEFT JOIN assessments a ON a.requirement_id = r.id
            WHERE e.id = ?
            """,
            (evidence_id,),
        ).fetchone()
    item = _to_dict(row)
    return _normalize_evidence_record(item) if item is not None else None


def get_requirement_in_set_by_prior(
    requirement_set_id: str, prior_requirement_id: str
) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id FROM requirements
            WHERE requirement_set_id = ? AND prior_requirement_id = ?
            LIMIT 1
            """,
            (requirement_set_id, prior_requirement_id),
        ).fetchone()
    return get_requirement(str(row["id"])) if row is not None else None


def get_assessment(requirement_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM assessments WHERE requirement_id = ?", (requirement_id,)
        ).fetchone()
    item = _to_dict(row)
    return _normalize_assessment_record(item) if item is not None else None


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
    compliance_status: str = "",
    evidence_findings: Any = None,
    structured_information_gap: Any = None,
    sub_judgments: Any = None,
    analysis_input_schema_version: str = "",
    analysis_input_sha256: str = "",
) -> dict[str, Any]:
    assessment_id = str(uuid.uuid4())
    updated_at = _now_iso()
    evidence_findings_json = _canonical_json_text(
        evidence_findings, default=[]
    )
    structured_information_gap_json = _canonical_json_text(
        structured_information_gap, default={}
    )
    sub_judgments_json = _canonical_json_text(sub_judgments, default=[])
    input_schema = str(analysis_input_schema_version or "").strip()
    input_sha256 = str(analysis_input_sha256 or "").strip()
    if bool(input_schema) != bool(input_sha256) or (
        input_sha256 and re.fullmatch(r"[0-9a-f]{64}", input_sha256) is None
    ):
        raise ValueError("assessment analysis input provenance is invalid")
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
                    ai_decision = ?, compliance_status = ?, ai_reason = ?, ai_confidence = ?, assessment_mode = ?, analysis_label = ?,
                    analysis_basis = ?, normalized_requirement = ?, risk_level = ?, clarification_question = ?,
                    model_name = ?, prompt_version = ?, evidence_findings_json = ?,
                    structured_information_gap_json = ?, sub_judgments_json = ?,
                    reference_status = 'CURRENT',
                    source_requirement_id = '', result_origin = 'NATIVE',
                    source_assessment_id = '', impact_receipt_id = '',
                    analysis_input_schema_version = ?, analysis_input_sha256 = ?,
                    updated_at = ?
                WHERE requirement_id = ?
                """,
                (
                    profile_id,
                    provider,
                    retrieval_backend,
                    ai_decision,
                    str(compliance_status or ""),
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
                    evidence_findings_json,
                    structured_information_gap_json,
                    sub_judgments_json,
                    input_schema,
                    input_sha256,
                    updated_at,
                    requirement_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO assessments
                (id, requirement_id, profile_id, provider, retrieval_backend, ai_decision, compliance_status, ai_reason, ai_confidence,
                 assessment_mode, analysis_label, analysis_basis, normalized_requirement, risk_level, clarification_question,
                 final_decision, engineer_reason, model_name, prompt_version, evidence_findings_json,
                 structured_information_gap_json, sub_judgments_json,
                 analysis_input_schema_version, analysis_input_sha256, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id,
                    requirement_id,
                    profile_id,
                    provider,
                    retrieval_backend,
                    ai_decision,
                    str(compliance_status or ""),
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
                    evidence_findings_json,
                    structured_information_gap_json,
                    sub_judgments_json,
                    input_schema,
                    input_sha256,
                    updated_at,
                ),
            )
        row = conn.execute(
            "SELECT * FROM assessments WHERE requirement_id = ?", (requirement_id,)
        ).fetchone()
    item = _to_dict(row) or {}
    return _normalize_assessment_record(item)


def replace_evidences(requirement_id: str, evidences: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM evidences WHERE requirement_id = ?", (requirement_id,))
        for evidence in evidences:
            metadata = dict(evidence.get("evidence_metadata") or {})
            for key in (
                "chunk_id",
                "score_source",
                "doc_type",
                "status",
                "effective_from",
                "effective_to",
                "section_code",
                "section_title",
                "authority",
                "claim_role",
                "applicability",
                "chunk_content_hash",
                "excerpt_hash",
                "retrieval_parent_evidence_id",
                "retrieval_parent_relevance",
                "table_id",
                "native_locator",
                "native_format",
                "native_page",
                "native_table_index",
                "native_body_element_index",
                "native_workbook",
                "native_sheet",
                "native_range",
            ):
                value = evidence.get(key)
                if value not in (None, "", [], {}):
                    metadata[key] = value
            conn.execute(
                """
                INSERT INTO evidences
                (id, requirement_id, source_doc_id, source_doc_name, source_doc_version,
                 retrieval_evidence_id, content_hash, evidence_contract_version, content_kind,
                 evidence_metadata_json, source_location, excerpt, relevance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    requirement_id,
                    evidence["source_doc_id"],
                    evidence["source_doc_name"],
                    str(evidence.get("source_doc_version") or ""),
                    str(
                        evidence.get("retrieval_evidence_id")
                        or evidence.get("evidence_id")
                        or ""
                    ),
                    str(evidence.get("content_hash") or ""),
                    str(evidence.get("evidence_contract_version") or ""),
                    str(evidence.get("content_kind") or ""),
                    _canonical_json_text(metadata, default={}),
                    evidence["source_location"],
                    evidence["excerpt"],
                    float(evidence["relevance"]),
                ),
            )


def add_assessment_run(
    requirement_id: str,
    *,
    analysis_node_attempt_id: str = "",
    run_index: int = -1,
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
            (id, requirement_id, analysis_node_attempt_id, run_index,
             profile_id, provider, mode, retrieval_backend, model_name, prompt_version,
             input_payload_json, output_payload_json, validation_status, fallback_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                requirement_id,
                str(analysis_node_attempt_id or "").strip(),
                int(run_index),
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
    requirement_set_id: str = "",
    requirement_set_version: int | None = None,
    requirement_tree_sha256: str | None = None,
    knowledge_base_version: int | None = None,
    command_id: str = "",
    command_sha256: str = "",
    scope_schema_version: str = "",
    scope_kind: str = "",
    scope_payload_json: str = "{}",
    scope_sha256: str = "",
    publication_mode: str = "ATOMIC_BATCH",
    total_requirements: int,
    published_requirements: int = 0,
    status: str = "PENDING",
    current_stage: str = "queued",
    last_message: str = "AI analysis queued.",
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    timestamp = _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        document = conn.execute(
            """
            SELECT d.active_requirement_set_id, p.knowledge_base_version
            FROM sor_documents d
            INNER JOIN projects p ON p.id = d.project_id
            WHERE d.id = ? AND d.project_id = ?
            """,
            (sor_document_id, project_id),
        ).fetchone()
        if document is None:
            return {}
        set_id = str(requirement_set_id or "").strip()
        if not set_id:
            set_id = str(document["active_requirement_set_id"] or "")
        set_lineage = conn.execute(
            """
            SELECT version, requirement_tree_sha256
            FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (set_id, project_id, sor_document_id),
        ).fetchone()
        set_version_snapshot = max(
            int(
                set_lineage["version"]
                if requirement_set_version is None and set_lineage is not None
                else requirement_set_version or 0
            ),
            0,
        )
        tree_sha256_snapshot = str(
            set_lineage["requirement_tree_sha256"]
            if requirement_tree_sha256 is None and set_lineage is not None
            else requirement_tree_sha256 or ""
        ).strip().lower()
        normalized_status = str(status or "PENDING").strip().upper() or "PENDING"
        if normalized_status in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}:
            active_job = _get_active_ai_analysis_job_in_conn(conn, sor_document_id)
            if active_job is not None:
                raise RuntimeError("active_ai_analysis_job_exists")
        knowledge_snapshot = max(
            int(
                document["knowledge_base_version"]
                if knowledge_base_version is None
                else knowledge_base_version
            ),
            0,
        )
        conn.execute(
            """
            INSERT INTO ai_analysis_jobs
            (id, project_id, sor_document_id, requirement_set_id,
             requirement_set_version, requirement_tree_sha256, knowledge_base_version,
             command_id, command_sha256, scope_schema_version, scope_kind,
             scope_payload_json, scope_sha256, publication_mode,
             profile_id, provider, assessment_mode, retrieval_backend, chat_model, embedding_model, status, total_requirements,
             completed_requirements, published_requirements, analyzed_requirements, info_requirements, screened_requirements,
             current_requirement_id, current_requirement_code, current_requirement_index,
             current_stage, last_message, error_message,
             created_at, started_at, finished_at, updated_at)
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                0, ?, 0, 0, 0, '', '', 0,
                ?, ?, '', ?, '', '', ?
            )
            """,
            (
                job_id,
                project_id,
                sor_document_id,
                set_id,
                set_version_snapshot,
                tree_sha256_snapshot,
                knowledge_snapshot,
                str(command_id or "").strip(),
                str(command_sha256 or "").strip().lower(),
                str(scope_schema_version or "").strip(),
                str(scope_kind or "").strip().upper(),
                str(scope_payload_json or "{}"),
                str(scope_sha256 or "").strip().lower(),
                str(publication_mode or "ATOMIC_BATCH").strip().upper(),
                str(profile_id or "").strip(),
                str(provider or "").strip(),
                str(assessment_mode or "").strip(),
                str(retrieval_backend or "").strip(),
                str(chat_model or "").strip(),
                str(embedding_model or "").strip(),
                normalized_status,
                max(int(total_requirements or 0), 0),
                max(int(published_requirements or 0), 0),
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


def bind_ai_analysis_runtime_job(job_id: str, runtime_job_id: str) -> dict[str, Any]:
    if not is_postgresql_runtime():
        raise RuntimeError("Durable runtime job binding requires PostgreSQL.")
    normalized_runtime_id = str(runtime_job_id or "").strip()
    if not normalized_runtime_id:
        raise ValueError("runtime_job_id must not be blank.")
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET runtime_job_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (normalized_runtime_id, _now_iso(), job_id),
        )
        row = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return _to_dict(row) or {}


def get_latest_ai_analysis_job(sor_document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT j.*
            FROM ai_analysis_jobs j
            INNER JOIN sor_documents d ON d.id = j.sor_document_id
            WHERE j.sor_document_id = ? AND j.requirement_set_id = d.active_requirement_set_id
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sor_document_id,),
        ).fetchone()
    return _to_dict(row)


def get_running_ai_analysis_job(sor_document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = _get_active_ai_analysis_job_in_conn(conn, sor_document_id)
    return _to_dict(row)


def get_latest_ai_analysis_job_for_set(requirement_set_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM ai_analysis_jobs
            WHERE requirement_set_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (requirement_set_id,),
        ).fetchone()
    return _to_dict(row)


def get_latest_completed_ai_analysis_job_for_set(requirement_set_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM ai_analysis_jobs
            WHERE requirement_set_id = ? AND status = 'COMPLETED'
            ORDER BY finished_at DESC, created_at DESC, rowid DESC
            LIMIT 1
            """,
            (requirement_set_id,),
        ).fetchone()
    return _to_dict(row)


def _get_active_ai_analysis_job_in_conn(
    conn: sqlite3.Connection,
    sor_document_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM ai_analysis_jobs
        WHERE sor_document_id = ?
          AND status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (sor_document_id,),
    ).fetchone()


def get_active_ai_analysis_job_for_document(sor_document_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = _get_active_ai_analysis_job_in_conn(conn, sor_document_id)
    return _to_dict(row)


def update_ai_analysis_job(
    job_id: str,
    *,
    status: str | None = None,
    total_requirements: int | None = None,
    completed_requirements: int | None = None,
    published_requirements: int | None = None,
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
    if published_requirements is not None:
        assignments.append("published_requirements = ?")
        values.append(max(int(published_requirements or 0), 0))
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


def request_ai_analysis_job_cancel(
    job_id: str,
    *,
    last_message: str,
) -> tuple[dict[str, Any], bool]:
    """Move only a still-active job into cancellation.

    The compare-and-set prevents a late stop request from overwriting a job
    that completed between the status read and the user's confirmation.
    """

    updated_at = _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET status = 'CANCEL_REQUESTED', current_stage = 'cancelling',
                last_message = ?, error_message = '', updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'RUNNING')
            """,
            (str(last_message or ""), updated_at, str(job_id or "").strip()),
        )
        row = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (str(job_id or "").strip(),),
        ).fetchone()
        changed = cursor.rowcount == 1
    return _to_dict(row) or {}, changed


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


def list_ai_analysis_job_targets(job_id: str) -> list[dict[str, Any]]:
    """Return the durable node targets in immutable Matrix source order."""

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM ai_analysis_job_targets
            WHERE job_id = ?
            ORDER BY source_order, id
            """,
            (str(job_id or "").strip(),),
        ).fetchall()
    items = _to_dict_list(rows)
    for item in items:
        item["retryable"] = bool(int(item.get("retryable") or 0))
    return items


def get_ai_analysis_job_target(target_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ?",
            (str(target_id or "").strip(),),
        ).fetchone()
    item = _to_dict(row)
    if item is not None:
        item["retryable"] = bool(int(item.get("retryable") or 0))
    return item


def list_ai_analysis_node_attempts(
    target_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 100), 500))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM ai_analysis_node_attempts
            WHERE target_id = ?
            ORDER BY attempt_no, id
            LIMIT ?
            """,
            (str(target_id or "").strip(), safe_limit),
        ).fetchall()
    items = _to_dict_list(rows)
    for item in items:
        item["result_payload"] = _decode_json_payload(
            item.get("result_payload_json"), default={}
        )
    return items


def get_ai_analysis_node_attempt(attempt_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ai_analysis_node_attempts WHERE id = ?",
            (str(attempt_id or "").strip(),),
        ).fetchone()
    item = _to_dict(row)
    if item is not None:
        item["result_payload"] = _decode_json_payload(
            item.get("result_payload_json"), default={}
        )
    return item


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
            WHERE id = ? AND requirement_set_id IN (
                SELECT id FROM requirement_sets WHERE status != 'LOCKED'
            )
            """,
            payload,
        )


def update_requirement_status(requirement_id: str, status: str) -> None:
    timestamp = _now_iso()
    with get_conn() as conn:
        _require_requirement_mutable(conn, requirement_id)
        conn.execute(
            "UPDATE requirements SET status = ?, updated_at = ? WHERE id = ?",
            (status, timestamp, requirement_id),
        )


def _require_requirement_mutable(conn: sqlite3.Connection, requirement_id: str) -> None:
    row = conn.execute(
        """
        SELECT rs.status, rs.is_active, d.active_requirement_set_id, r.requirement_set_id
        FROM requirements r
        INNER JOIN requirement_sets rs ON rs.id = r.requirement_set_id
        INNER JOIN sor_documents d ON d.id = r.sor_document_id
        WHERE r.id = ?
        """,
        (requirement_id,),
    ).fetchone()
    if row is not None and (
        str(row["status"] or "").strip().upper() == "LOCKED"
        or not int(row["is_active"] or 0)
        or str(row["active_requirement_set_id"] or "") != str(row["requirement_set_id"] or "")
    ):
        raise PermissionError("Locked requirement sets are immutable; create a draft version before editing.")


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
    sor_document_id: str = "",
    requirement_set_id: str = "",
    export_mode: str = "DRAFT",
    source_hash: str = "",
    knowledge_base_version: int = 0,
    ai_run_id: str = "",
    human_coverage: float = 0.0,
    object_sha256: str = "",
    object_size: int = 0,
    media_type: str = "application/octet-stream",
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    created_at = _now_iso()
    attempts = 4
    for attempt in range(attempts):
        try:
            with get_conn() as conn:
                if is_postgresql_runtime():
                    _register_stored_object_in_conn(
                        conn,
                        storage_key=file_path,
                        sha256=object_sha256,
                        size_bytes=object_size,
                        media_type=media_type,
                        object_kind="export",
                    )
                conn.execute(
                    """
                    INSERT INTO export_jobs
                    (id, project_id, file_name, file_path, version_label, compare_export_id, snapshot_json,
                     sor_document_id, requirement_set_id, export_mode, source_hash,
                     knowledge_base_version, ai_run_id, human_coverage, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        project_id,
                        file_name,
                        file_path,
                        version_label.strip(),
                        compare_export_id.strip(),
                        snapshot_json,
                        str(sor_document_id or ""),
                        str(requirement_set_id or ""),
                        str(export_mode or "DRAFT").strip().upper(),
                        str(source_hash or ""),
                        max(int(knowledge_base_version or 0), 0),
                        str(ai_run_id or ""),
                        max(0.0, min(float(human_coverage or 0.0), 1.0)),
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
    *,
    requirement_set_id: str = "",
    preview_generation_id: str = "",
) -> None:
    with get_conn() as conn:
        _replace_document_table_assets_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=sor_document_id,
            requirement_set_id=requirement_set_id,
            assets=assets,
            preview_generation_id=preview_generation_id,
        )


def _replace_document_table_assets_in_conn(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str = "",
    assets: list[dict[str, Any]],
    preview_generation_id: str = "",
) -> None:
    timestamp = _now_iso()
    set_id = str(requirement_set_id or "").strip()
    if set_id:
        target = conn.execute(
            """
            SELECT id, status FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (set_id, project_id, sor_document_id),
        ).fetchone()
        if target is None:
            raise LookupError("requirement_set_not_found")
        if str(target["status"] or "").upper() == "LOCKED":
            raise PermissionError("Locked requirement-set table assets are immutable.")
    deduped_assets: list[dict[str, Any]] = []
    seen_table_ids: set[str] = set()
    for asset in assets:
        normalized_asset = dict(asset)
        if preview_generation_id:
            normalized_asset["preview_generation_id"] = preview_generation_id
        else:
            normalized_asset["preview_generation_id"] = str(normalized_asset.get("preview_generation_id") or "")
        if set_id:
            normalized_asset["requirement_set_id"] = set_id
        asset = normalized_asset
        table_id = str(asset.get("table_id") or "").strip()
        if not table_id or table_id in seen_table_ids:
            continue
        seen_table_ids.add(table_id)
        deduped_assets.append(asset)
    table_name = "requirement_set_table_assets" if set_id else "document_table_assets"
    scope_column = "requirement_set_id" if set_id else "sor_document_id"
    scope_value = set_id or sor_document_id
    conn.execute(f"DELETE FROM {table_name} WHERE {scope_column} = ?", (scope_value,))
    for asset in deduped_assets:
        table_id = str(asset.get("table_id") or "").strip()
        if not table_id:
            continue
        if set_id:
            conn.execute(
                """
                INSERT INTO requirement_set_table_assets
                (id, project_id, sor_document_id, requirement_set_id, table_id, asset_json,
                 preview_generation_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), project_id, sor_document_id, set_id, table_id,
                    json.dumps(asset, ensure_ascii=False),
                    str(asset.get("preview_generation_id") or ""), timestamp, timestamp,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO document_table_assets
                (id, project_id, sor_document_id, table_id, asset_json, preview_generation_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), project_id, sor_document_id, table_id,
                    json.dumps(asset, ensure_ascii=False),
                    str(asset.get("preview_generation_id") or ""), timestamp, timestamp,
                ),
            )


def list_document_table_assets(
    project_id: str,
    sor_document_id: str,
    requirement_set_id: str = "",
) -> list[dict[str, Any]]:
    with get_conn() as conn:
        explicit_set_id = str(requirement_set_id or "").strip()
        set_id = explicit_set_id
        if not set_id:
            document = conn.execute(
                "SELECT active_requirement_set_id FROM sor_documents WHERE id = ? AND project_id = ?",
                (sor_document_id, project_id),
            ).fetchone()
            set_id = str(document["active_requirement_set_id"] or "") if document else ""
        rows = []
        using_set_assets = False
        if set_id:
            rows = conn.execute(
                """
                SELECT asset_json
                FROM requirement_set_table_assets
                WHERE project_id = ? AND sor_document_id = ? AND requirement_set_id = ?
                ORDER BY table_id
                """,
                (project_id, sor_document_id, set_id),
            ).fetchall()
            using_set_assets = bool(rows)
        if not rows and not explicit_set_id:
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
            if using_set_assets:
                payload["requirement_set_id"] = set_id
            assets.append(payload)
    return assets


def get_document_table_asset(
    project_id: str,
    sor_document_id: str,
    table_id: str,
    requirement_set_id: str = "",
) -> dict[str, Any] | None:
    with get_conn() as conn:
        explicit_set_id = str(requirement_set_id or "").strip()
        set_id = explicit_set_id
        if not set_id:
            document = conn.execute(
                "SELECT active_requirement_set_id FROM sor_documents WHERE id = ? AND project_id = ?",
                (sor_document_id, project_id),
            ).fetchone()
            set_id = str(document["active_requirement_set_id"] or "") if document else ""
        row = None
        using_set_asset = False
        if set_id:
            row = conn.execute(
                """
                SELECT asset_json
                FROM requirement_set_table_assets
                WHERE project_id = ? AND sor_document_id = ? AND requirement_set_id = ? AND table_id = ?
                """,
                (project_id, sor_document_id, set_id, table_id),
            ).fetchone()
            using_set_asset = row is not None
        if row is None and not explicit_set_id:
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
    if isinstance(payload, dict) and using_set_asset:
        payload["requirement_set_id"] = set_id
    return payload if isinstance(payload, dict) else None


def list_export_jobs(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, project_id, file_name, file_path, version_label, compare_export_id,
                   sor_document_id, requirement_set_id, export_mode, source_hash,
                   knowledge_base_version, ai_run_id, human_coverage, created_at
            FROM export_jobs
            WHERE project_id = ?
            ORDER BY created_at DESC
            """,
            (project_id,),
        ).fetchall()
    output = _to_dict_list(rows)
    for item in output:
        item["document_id"] = str(item.get("sor_document_id") or "")
    return output


def get_export_job(export_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM export_jobs WHERE id = ?", (export_id,)).fetchone()
    return _to_dict(row)


def get_export_job_by_file_name(file_name: str) -> dict[str, Any] | None:
    normalized = str(file_name or "").strip()
    if not normalized:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM export_jobs
            WHERE file_name = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
    return _to_dict(row)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {str(row[1]) for row in rows}
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_ai_analysis_job_scope_schema(
    conn: sqlite3.Connection,
    *,
    backfill_set_version: bool,
    backfill_tree_sha256: bool,
    backfill_published_count: bool,
) -> None:
    """Add immutable W6-A scope identity without inventing legacy scope payloads.

    Historical analysis jobs always used the full eligible Requirement Set, but
    they did not persist the exact ordered Matrix targets.  Their scope and
    command fields therefore remain empty.  Set lineage can be recovered from
    the immutable Requirement Set row, while completed legacy jobs can safely be
    marked fully published because the old serving transaction was all-or-none.
    """

    if backfill_set_version:
        conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET requirement_set_version = COALESCE(
                (
                    SELECT rs.version
                    FROM requirement_sets rs
                    WHERE rs.id = ai_analysis_jobs.requirement_set_id
                ),
                0
            )
            """
        )
    if backfill_tree_sha256:
        conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET requirement_tree_sha256 = COALESCE(
                (
                    SELECT rs.requirement_tree_sha256
                    FROM requirement_sets rs
                    WHERE rs.id = ai_analysis_jobs.requirement_set_id
                ),
                ''
            )
            """
        )
    if backfill_published_count:
        conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET published_requirements = CASE
                WHEN UPPER(COALESCE(status, '')) = 'COMPLETED'
                THEN MAX(COALESCE(total_requirements, 0), 0)
                ELSE 0
            END
            """
        )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_analysis_jobs_document_command
        ON ai_analysis_jobs(sor_document_id, command_id)
        WHERE command_id <> ''
        """
    )


def _ensure_ai_analysis_job_lineage_schema(
    conn: sqlite3.Connection,
    *,
    backfill_knowledge_snapshot: bool,
) -> None:
    """Backfill immutable job inputs and enforce one active worker per SOR.

    Existing databases did not capture a job-level knowledge snapshot.  The
    best available migration value is the document's published snapshot for a
    matching completed set, otherwise the project's current knowledge version.
    The backfill runs only when the column is first introduced so a legitimate
    historical version ``0`` is never rewritten on a later startup.
    """

    if backfill_knowledge_snapshot:
        conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET knowledge_base_version = COALESCE(
                (
                    SELECT CASE
                        WHEN UPPER(COALESCE(ai_analysis_jobs.status, '')) = 'COMPLETED'
                         AND ai_analysis_jobs.requirement_set_id = d.active_requirement_set_id
                        THEN d.ai_knowledge_base_version
                        ELSE p.knowledge_base_version
                    END
                    FROM sor_documents d
                    INNER JOIN projects p ON p.id = d.project_id
                    WHERE d.id = ai_analysis_jobs.sor_document_id
                ),
                0
            )
            """
        )

    # A pre-lineage database could contain multiple non-terminal rows for one
    # document. Keep the newest row claimable and close older rows before the
    # partial unique index is installed.
    duplicates = conn.execute(
        """
        SELECT sor_document_id
        FROM ai_analysis_jobs
        WHERE status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
        GROUP BY sor_document_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    migrated_at = _now_iso()
    for duplicate in duplicates:
        rows = conn.execute(
            """
            SELECT rowid, id
            FROM ai_analysis_jobs
            WHERE sor_document_id = ?
              AND status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
            ORDER BY created_at DESC, rowid DESC
            """,
            (duplicate["sor_document_id"],),
        ).fetchall()
        for stale in rows[1:]:
            conn.execute(
                """
                UPDATE ai_analysis_jobs
                SET status = 'FAILED', current_stage = 'failed',
                    last_message = 'Superseded duplicate worker found during lineage migration.',
                    error_message = 'Duplicate active AI job was closed during schema migration.',
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (migrated_at, migrated_at, stale["id"]),
            )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_analysis_jobs_one_active_document
        ON ai_analysis_jobs(sor_document_id)
        WHERE status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_set_completion
        ON ai_analysis_jobs(requirement_set_id, status, finished_at DESC, created_at DESC)
        """
    )


def _ensure_requirement_sets_schema(conn: sqlite3.Connection) -> None:
    """Migrate legacy document-scoped requirements to immutable version sets.

    The migration is intentionally additive first: an active set is created for every
    document that already owns requirements, those rows are linked, and only then is
    the legacy UNIQUE(document, code) table rebuilt as UNIQUE(set, code).  Child rows
    keep their requirement IDs, so assessments, evidence and review history survive.
    """

    _ensure_column(conn, "requirements", "requirement_set_id", "TEXT DEFAULT ''")
    _ensure_column(conn, "requirements", "prior_requirement_id", "TEXT DEFAULT ''")
    _ensure_column(conn, "requirements", "decision_reference_json", "TEXT DEFAULT '{}'")

    document_rows = conn.execute(
        """
        SELECT d.*, COUNT(r.id) AS requirement_count
        FROM sor_documents d
        LEFT JOIN requirements r ON r.sor_document_id = d.id
        GROUP BY d.id
        """
    ).fetchall()
    for document in document_rows:
        active_set_id = str(document["active_requirement_set_id"] or "").strip()
        active_set = None
        if active_set_id:
            active_set = conn.execute(
                "SELECT * FROM requirement_sets WHERE id = ? AND sor_document_id = ?",
                (active_set_id, document["id"]),
            ).fetchone()
        if active_set is None:
            active_set = conn.execute(
                """
                SELECT * FROM requirement_sets
                WHERE sor_document_id = ? AND is_active = 1
                ORDER BY version DESC LIMIT 1
                """,
                (document["id"],),
            ).fetchone()

        requirement_count = int(document["requirement_count"] or 0)
        if active_set is None and requirement_count > 0:
            set_id = str(uuid.uuid4())
            version = max(int(document["requirement_set_version"] or 0), 1)
            locked_at = str(document["extraction_locked_at"] or "").strip()
            status = "LOCKED" if locked_at else "DRAFT"
            activated_at = str(document["updated_at"] or document["created_at"] or _now_iso())
            source_hash = hashlib.sha256(
                str(document["extracted_text"] or "").encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO requirement_sets
                (id, project_id, sor_document_id, version, status, is_active,
                 source_hash, preview_generation_id, supersedes_id,
                 created_at, activated_at, locked_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, '', ?, ?, ?)
                """,
                (
                    set_id,
                    document["project_id"],
                    document["id"],
                    version,
                    status,
                    source_hash,
                    str(document["preview_generation_id"] or ""),
                    activated_at,
                    activated_at,
                    locked_at,
                ),
            )
            active_set = conn.execute(
                "SELECT * FROM requirement_sets WHERE id = ?", (set_id,)
            ).fetchone()

        if active_set is None:
            continue
        set_id = str(active_set["id"])
        conn.execute(
            "UPDATE sor_documents SET active_requirement_set_id = ? WHERE id = ?",
            (set_id, document["id"]),
        )
        conn.execute(
            """
            UPDATE requirements SET requirement_set_id = ?
            WHERE sor_document_id = ? AND COALESCE(requirement_set_id, '') = ''
            """,
            (set_id, document["id"]),
        )
        conn.execute(
            """
            UPDATE ai_analysis_jobs SET requirement_set_id = ?
            WHERE sor_document_id = ? AND COALESCE(requirement_set_id, '') = ''
            """,
            (set_id, document["id"]),
        )

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'requirements'"
    ).fetchone()
    schema_sql = str(row[0] or "") if row else ""
    compact_schema = "".join(schema_sql.split()).lower()
    if "unique(requirement_set_id,req_code)" in compact_schema:
        return

    child_backups: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    for table in ("assessments", "evidences", "assessment_runs", "review_logs"):
        columns = [str(item["name"]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
        child_backups[table] = (columns, [tuple(row[column] for column in columns) for row in rows])

    conn.execute(
        """
        CREATE TABLE requirements_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            sor_document_id TEXT NOT NULL,
            requirement_set_id TEXT NOT NULL DEFAULT '',
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
            preview_generation_id TEXT DEFAULT '',
            prior_requirement_id TEXT DEFAULT '',
            decision_reference_json TEXT DEFAULT '{}',
            UNIQUE(requirement_set_id, req_code),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
            FOREIGN KEY(requirement_set_id) REFERENCES requirement_sets(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO requirements_new (
            id, project_id, sor_document_id, requirement_set_id, req_code, source_page, source_anchor,
            requirement_text, requirement_type, priority, status, created_at, updated_at,
            entry_kind, text_relation, is_deduped, heading_text, raw_text, display_text,
            merged_from_req_codes, merged_from_entry_ids, dedupe_version,
            parent_section_code, parent_section_title, parent_section_label,
            table_refs, content_blocks, has_table_asset, table_render_mode, preview_generation_id,
            prior_requirement_id, decision_reference_json
        )
        SELECT
            id, project_id, sor_document_id, requirement_set_id, req_code, source_page, source_anchor,
            requirement_text, requirement_type, priority, status, created_at, updated_at,
            entry_kind, text_relation, is_deduped, heading_text, raw_text, display_text,
            merged_from_req_codes, merged_from_entry_ids, dedupe_version,
            parent_section_code, parent_section_title, parent_section_label,
            COALESCE(table_refs, '[]'), COALESCE(content_blocks, '[]'),
            COALESCE(has_table_asset, 0), COALESCE(table_render_mode, ''),
            COALESCE(preview_generation_id, ''), COALESCE(prior_requirement_id, ''),
            COALESCE(decision_reference_json, '{}')
        FROM requirements
        """
    )
    conn.execute("DROP TABLE requirements")
    conn.execute("ALTER TABLE requirements_new RENAME TO requirements")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requirements_project_id ON requirements(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requirements_document_id ON requirements(sor_document_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requirements_set_id ON requirements(requirement_set_id)")
    for table, (columns, rows) in child_backups.items():
        if not rows:
            continue
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            rows,
        )


def _backfill_export_job_lineage(conn: sqlite3.Connection) -> None:
    """Scope legacy exports when the owning SOR can be proven deterministically.

    Old records predate export mode and requirement-set lineage.  We never infer
    whether such a workbook was draft or formal, so every migrated record is
    labelled LEGACY.  Ambiguous multi-SOR records remain intentionally unscoped.
    """

    jobs = conn.execute(
        """
        SELECT * FROM export_jobs
        WHERE COALESCE(sor_document_id, '') = ''
           OR COALESCE(requirement_set_id, '') = ''
        """
    ).fetchall()
    for job in jobs:
        project_id = str(job["project_id"] or "")
        document_id = str(job["sor_document_id"] or "").strip()
        requirement_set_id = str(job["requirement_set_id"] or "").strip()
        snapshot = _decode_json_payload(job["snapshot_json"], default=[])
        requirement_ids = [
            str(item.get("id") or item.get("requirement_id") or "").strip()
            for item in snapshot
            if isinstance(item, dict)
        ]
        requirement_ids = list(dict.fromkeys(item for item in requirement_ids if item))
        lineage_rows: list[sqlite3.Row] = []
        for offset in range(0, len(requirement_ids), 400):
            chunk = requirement_ids[offset : offset + 400]
            placeholders = ", ".join("?" for _ in chunk)
            lineage_rows.extend(
                conn.execute(
                    f"""
                    SELECT sor_document_id, requirement_set_id
                    FROM requirements
                    WHERE project_id = ? AND id IN ({placeholders})
                    """,
                    (project_id, *chunk),
                ).fetchall()
            )
        snapshot_documents = {
            str(row["sor_document_id"] or "").strip() for row in lineage_rows
            if str(row["sor_document_id"] or "").strip()
        }
        snapshot_sets = {
            str(row["requirement_set_id"] or "").strip() for row in lineage_rows
            if str(row["requirement_set_id"] or "").strip()
        }
        if not document_id and len(snapshot_documents) == 1:
            document_id = next(iter(snapshot_documents))
        if not requirement_set_id and len(snapshot_sets) == 1:
            requirement_set_id = next(iter(snapshot_sets))

        if not document_id:
            project_documents = conn.execute(
                "SELECT id, active_requirement_set_id FROM sor_documents WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            if len(project_documents) == 1:
                document_id = str(project_documents[0]["id"] or "")
                if not requirement_set_id:
                    requirement_set_id = str(
                        project_documents[0]["active_requirement_set_id"] or ""
                    )
        if document_id and not requirement_set_id:
            document = conn.execute(
                "SELECT active_requirement_set_id FROM sor_documents WHERE id = ? AND project_id = ?",
                (document_id, project_id),
            ).fetchone()
            requirement_set_id = str(
                document["active_requirement_set_id"] if document is not None else ""
            ).strip()

        if requirement_set_id:
            owning_set = conn.execute(
                """
                SELECT sor_document_id FROM requirement_sets
                WHERE id = ? AND project_id = ?
                """,
                (requirement_set_id, project_id),
            ).fetchone()
            if owning_set is None:
                requirement_set_id = ""
            elif document_id and str(owning_set["sor_document_id"] or "") != document_id:
                # Contradictory evidence is ambiguous; retain neither guessed set
                # nor a document/set combination that never existed.
                requirement_set_id = ""

        conn.execute(
            """
            UPDATE export_jobs
            SET sor_document_id = ?, requirement_set_id = ?, export_mode = 'LEGACY'
            WHERE id = ?
            """,
            (document_id, requirement_set_id, job["id"]),
        )


def _backfill_requirement_set_table_assets(conn: sqlite3.Connection) -> None:
    """Attach legacy document-scoped assets to the current immutable set once."""
    timestamp = _now_iso()
    rows = conn.execute(
        """
        SELECT dta.id, dta.project_id, dta.sor_document_id, dta.table_id,
               dta.asset_json, dta.preview_generation_id,
               dta.created_at, d.active_requirement_set_id
        FROM document_table_assets dta
        INNER JOIN sor_documents d ON d.id = dta.sor_document_id
        WHERE NULLIF(TRIM(d.active_requirement_set_id), '') IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM requirement_set_table_assets rsta
              WHERE rsta.requirement_set_id = d.active_requirement_set_id
                AND rsta.table_id = dta.table_id
          )
        """
    ).fetchall()
    for row in rows:
        set_id = str(row["active_requirement_set_id"] or "").strip()
        asset = _decode_json_payload(row["asset_json"], default={})
        if not set_id or not isinstance(asset, dict):
            continue
        table_id = str(row["table_id"] or "").strip()
        referenced = False
        for requirement in conn.execute(
            "SELECT table_refs FROM requirements WHERE requirement_set_id = ?",
            (set_id,),
        ).fetchall():
            refs = _decode_json_payload(requirement["table_refs"], default=[])
            if any(
                isinstance(ref, dict) and str(ref.get("table_id") or "").strip() == table_id
                for ref in refs
            ):
                referenced = True
                break
        if not referenced:
            continue
        asset["requirement_set_id"] = set_id
        conn.execute(
            """
            INSERT OR IGNORE INTO requirement_set_table_assets
            (id, project_id, sor_document_id, requirement_set_id, table_id, asset_json,
             preview_generation_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), str(row["project_id"]), str(row["sor_document_id"]),
                set_id, table_id, json.dumps(asset, ensure_ascii=False),
                str(row["preview_generation_id"] or ""),
                str(row["created_at"] or timestamp), timestamp,
            ),
        )


def _cleanup_orphan_records(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM knowledge_documents
        WHERE project_id NOT IN (SELECT id FROM projects);

        DELETE FROM sor_documents
        WHERE project_id NOT IN (SELECT id FROM projects);

        DELETE FROM export_jobs
        WHERE project_id NOT IN (SELECT id FROM projects);

        DELETE FROM requirement_sets
        WHERE project_id NOT IN (SELECT id FROM projects)
           OR sor_document_id NOT IN (SELECT id FROM sor_documents);

        DELETE FROM batch_decision_operations
        WHERE project_id NOT IN (SELECT id FROM projects);

        DELETE FROM document_table_assets
        WHERE project_id NOT IN (SELECT id FROM projects)
           OR sor_document_id NOT IN (SELECT id FROM sor_documents);

        DELETE FROM requirement_set_table_assets
        WHERE project_id NOT IN (SELECT id FROM projects)
           OR sor_document_id NOT IN (SELECT id FROM sor_documents)
           OR requirement_set_id NOT IN (SELECT id FROM requirement_sets);

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
