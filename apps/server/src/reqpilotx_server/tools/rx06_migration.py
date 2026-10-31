"""RX06 explicit PostgreSQL-to-SQLite migration with export receipts."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Iterator

from reqpilotx_server.resource_paths import postgres_migration_dir
from reqpilotx_server.tools.rx06_common import (
    Rx06Error,
    canonical_json,
    is_within,
    json_safe,
    primary_key_digest,
    read_json,
    records_digest,
    rename_no_replace,
    require_absolute,
    require_sha256,
    safe_relative_file,
    safe_relative_key,
    secure_directory,
    secure_file,
    sha256_bytes,
    sha256_file,
    value_digest,
    verify_runtime_references,
    write_json,
)


EXPORT_FORMAT = "reqpilotx-postgres-sqlite-export"
EXPORT_FORMAT_VERSION = 1
RECEIPT_SCHEMA_VERSION = "reqpilotx-rx06-receipt/v1"
KNOWLEDGE_MAPPING_SCHEMA_VERSION = "reqpilotx-knowledge-mapping/v1"
MANIFEST_NAME = "manifest.json"
TABLES_DIR = "tables"
OBJECTS_DIR = "objects"
KNOWLEDGE_MAPPING_FILE = "knowledge-mapping.json"
RECEIPT_DIR = "rx06"
RECEIPT_FILE = "receipt.json"
SUPPORTED_STATUSES = {"RESOLVED", "UNRESOLVED", "UNAVAILABLE"}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SECRET_COLUMN_NAMES = {
    "password",
    "password_digest",
    "passwd",
    "secret",
    "client_secret",
    "api_key",
    "private_key",
    "access_token",
    "refresh_token",
    "session_token",
    "token_sha256",
    "csrf_token",
    "authorization",
}
_SECRET_COLUMN_SUFFIXES = ("_password", "_secret", "_api_key", "_private_key", "_credential")
_OBJECT_KEYS = {
    "storage_key",
    "object_key",
    "export_key",
    "requirement_tree_object_key",
    "payload_storage_key",
}
_JSON_COLUMNS = {
    ("source_item_delivery_versions", "artifact_json"),
    ("source_item_manual_decision_versions", "artifact_json"),
    ("source_capture_snapshots", "payload_json"),
    ("source_visual_capture_generations", "payload_json"),
    ("source_visual_page_snapshots", "payload_json"),
    ("source_ir_snapshots", "payload_json"),
    ("requirement_atomization_checkpoints", "payload_json"),
    ("requirement_atomization_runs", "payload_json"),
    ("requirement_atomization_scope_receipts", "payload_json"),
    ("requirement_atomization_scope_attempts", "payload_json"),
    ("requirement_refinement_suggestions", "payload_json"),
    ("requirement_refinement_events", "payload_json"),
    ("requirement_enrichment_publication_previews", "payload_json"),
    ("requirement_enrichment_publications", "result_json"),
    ("requirement_enrichment_publication_binding_previews", "payload_json"),
    ("requirement_enrichment_publication_bindings", "payload_json"),
    ("requirement_enrichment_publication_row_bindings", "payload_json"),
    ("entry_classification_snapshots", "payload_json"),
    ("requirement_set_impact_receipts", "payload_json"),
    ("ai_analysis_node_attempts", "result_payload_json"),
    ("agent_structure_proposals", "proposal_payload"),
    ("agent_structure_proposals", "runtime_receipt"),
    ("agent_table_materializations", "logical_table_scope_payload"),
    ("agent_table_materializations", "materialization_payload"),
    ("agent_table_materializations", "evidence_bindings"),
    ("agent_structure_handoff_attempts", "request_payload"),
    ("agent_structure_handoff_attempts", "receipt_payload"),
    ("agent_structure_activity_events", "event_payload"),
}
_FILE_COLUMN_ALIASES = {
    "sor_documents": ("storage_key", "file_path"),
    "knowledge_documents": ("storage_key", "file_path"),
    "export_jobs": ("storage_key", "file_path"),
    "knowledge_document_syncs": ("payload_storage_key", "payload_path"),
}
_DROPPED_SOURCE_COLUMNS = {
    "ai_analysis_jobs": {
        "runtime_job_id": "PostgreSQL runtime job pointer is not local business data.",
        "_row_order": "PostgreSQL identity ordering is not portable business data.",
    },
    "requirement_enrichment_publication_previews": {
        "_row_order": "PostgreSQL identity ordering is not portable business data.",
    },
}


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    primary_key: tuple[str, ...]
    filter_sql: str | None
    disposition: str
    note: str = ""


def _spec(name: str, pk: tuple[str, ...], filter_sql: str | None, disposition: str = "imported", note: str = "") -> TableSpec:
    return TableSpec(name=name, primary_key=pk, filter_sql=filter_sql, disposition=disposition, note=note)


_SPECS = (
    _spec("schema_migrations", ("version",), None, "skipped", "Applied migration ledger is validated separately."),
    _spec("projects", ("id",), "id = ANY(%s)"),
    _spec("stored_objects", ("storage_key",), None),
    _spec("source_asset_managed_objects", ("storage_key",), None),
    _spec("sor_documents", ("id",), "project_id = ANY(%s)"),
    _spec("table_projection_revisions", ("id",), "project_id = ANY(%s)"),
    _spec("knowledge_documents", ("id",), "project_id = ANY(%s)"),
    _spec("knowledge_document_syncs", ("id",), "knowledge_document_id IN (SELECT id FROM knowledge_documents WHERE project_id = ANY(%s))"),
    _spec("requirement_sets", ("id",), "project_id = ANY(%s)"),
    _spec("requirement_set_impact_receipts", ("receipt_id",), "project_id = ANY(%s)"),
    _spec("requirements", ("id",), "project_id = ANY(%s)"),
    _spec("requirement_matrix_node_states", ("id",), "project_id = ANY(%s)"),
    _spec("assessments", ("id",), "requirement_id IN (SELECT id FROM requirements WHERE project_id = ANY(%s))"),
    _spec("evidences", ("id",), "requirement_id IN (SELECT id FROM requirements WHERE project_id = ANY(%s))"),
    _spec("assessment_runs", ("id",), "requirement_id IN (SELECT id FROM requirements WHERE project_id = ANY(%s))"),
    _spec("ai_analysis_jobs", ("id",), "project_id = ANY(%s)"),
    _spec("ai_analysis_job_targets", ("id",), "sor_document_id IN (SELECT id FROM sor_documents WHERE project_id = ANY(%s))"),
    _spec("ai_analysis_node_attempts", ("id",), "job_id IN (SELECT id FROM ai_analysis_jobs WHERE project_id = ANY(%s))"),
    _spec("ai_analysis_events", ("id",), "sor_document_id IN (SELECT id FROM sor_documents WHERE project_id = ANY(%s))"),
    _spec("review_logs", ("id",), "requirement_id IN (SELECT id FROM requirements WHERE project_id = ANY(%s))"),
    _spec("export_jobs", ("id",), "project_id = ANY(%s)"),
    _spec("batch_decision_operations", ("id",), "project_id = ANY(%s)"),
    _spec("parsing_risk_acknowledgements", ("id",), "project_id = ANY(%s)"),
    _spec("document_table_assets", ("id",), "project_id = ANY(%s)"),
    _spec("requirement_set_table_assets", ("id",), "project_id = ANY(%s)"),
    _spec("entry_classification_snapshots", ("id",), "project_id = ANY(%s)"),
    _spec("entry_classification_command_receipts", ("id",), "project_id = ANY(%s)"),
    _spec("entry_command_receipts", ("kind", "scope_id", "command_id"), "(kind = 'project.create' AND resource_id = ANY(%s)) OR (kind = 'document.import' AND scope_id = ANY(%s))"),
    _spec("source_ir_snapshots", ("source_ir_id",), "document_id IN (SELECT id FROM sor_documents WHERE project_id = ANY(%s))"),
    _spec("source_capture_snapshots", ("capture_id",), "source_ir_document_id IN (SELECT id FROM sor_documents WHERE project_id = ANY(%s))"),
    _spec("source_capture_bindings", ("project_id", "sor_document_id", "source_sha256"), "project_id = ANY(%s)"),
    _spec("source_capture_asset_references", ("capture_id", "storage_key"), "capture_id IN (SELECT capture_id FROM source_capture_snapshots WHERE source_ir_document_id IN (SELECT id FROM sor_documents WHERE project_id = ANY(%s)))"),
    _spec("source_visual_capture_generations", ("generation_id",), "project_id = ANY(%s)"),
    _spec("source_visual_capture_bindings", ("project_id", "sor_document_id", "source_sha256"), "project_id = ANY(%s)"),
    _spec("source_visual_page_snapshots", ("generation_id", "page_number"), "generation_id IN (SELECT generation_id FROM source_visual_capture_generations WHERE project_id = ANY(%s))"),
    _spec("source_visual_page_attempts", ("attempt_id",), "project_id = ANY(%s)"),
    _spec("reading_sessions", ("reading_session_id",), "project_id = ANY(%s)"),
    _spec("reading_artifacts", ("artifact_id",), "reading_session_id IN (SELECT reading_session_id FROM reading_sessions WHERE project_id = ANY(%s))"),
    _spec("reading_notes", ("note_id",), "reading_session_id IN (SELECT reading_session_id FROM reading_sessions WHERE project_id = ANY(%s))"),
    _spec("reading_conversations", ("conversation_id",), "project_id = ANY(%s)"),
    _spec("reading_chat_messages", ("message_id",), "conversation_id IN (SELECT conversation_id FROM reading_conversations WHERE project_id = ANY(%s))"),
    _spec("source_item_manual_decision_versions", ("project_id", "sor_document_id", "revision"), "project_id = ANY(%s)"),
    _spec("source_item_delivery_versions", ("project_id", "sor_document_id", "revision"), "project_id = ANY(%s)"),
    _spec("llm_requirement_review_sessions", ("session_id",), "project_id = ANY(%s)"),
    _spec("llm_requirement_review_events", ("session_id", "revision"), "session_id IN (SELECT session_id FROM llm_requirement_review_sessions WHERE project_id = ANY(%s))"),
    _spec("llm_requirement_review_confirmation_events", ("session_id", "revision"), "session_id IN (SELECT session_id FROM llm_requirement_review_sessions WHERE project_id = ANY(%s))"),
    _spec("requirement_atomization_checkpoints", ("lineage_sha256", "review_node_id"), "project_id = ANY(%s)"),
    _spec("requirement_atomization_runs", ("atomization_run_id",), "project_id = ANY(%s)"),
    _spec("requirement_atomization_scope_receipts", ("receipt_id",), "project_id = ANY(%s)"),
    _spec("requirement_atomization_scope_attempts", ("attempt_id",), "project_id = ANY(%s)"),
    _spec("requirement_refinement_suggestions", ("suggestion_id",), "project_id = ANY(%s)"),
    _spec("requirement_refinement_events", ("suggestion_id", "revision"), "suggestion_id IN (SELECT suggestion_id FROM requirement_refinement_suggestions WHERE project_id = ANY(%s))"),
    _spec("requirement_enrichment_publication_previews", ("preview_id",), "project_id = ANY(%s)"),
    _spec("requirement_enrichment_publications", ("publication_key",), "preview_id IN (SELECT preview_id FROM requirement_enrichment_publication_previews WHERE project_id = ANY(%s))"),
    _spec("requirement_enrichment_publication_binding_previews", ("binding_preview_id",), "project_id = ANY(%s)"),
    _spec("requirement_enrichment_publication_bindings", ("binding_id",), "project_id = ANY(%s)"),
    _spec("requirement_enrichment_publication_row_bindings", ("binding_id", "publication_order"), "binding_id IN (SELECT binding_id FROM requirement_enrichment_publication_bindings WHERE project_id = ANY(%s))"),
    _spec("source_asset_references", ("document_id", "source_sha256", "storage_key"), "project_id = ANY(%s)", "export_only", "Local SQLite owns source objects through capture references; this PostgreSQL GC manifest is exported for recovery but not imported."),
    _spec("agent_structure_proposals", ("id",), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent Structure proposal store in RX06 scope."),
    _spec("agent_structure_proposal_events", ("id",), "proposal_id IN (SELECT id FROM agent_structure_proposals WHERE project_id = ANY(%s))", "export_only", "Local SQLite has no Agent Structure proposal store in RX06 scope."),
    _spec("agent_structure_steer_commands", ("id",), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent Structure steer store in RX06 scope."),
    _spec("agent_table_materializations", ("id",), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent table materialization store in RX06 scope."),
    _spec("agent_table_materialization_commands", ("project_id", "sor_document_id", "source_sha256", "effective_structure_snapshot_id", "materializer_logical_table_id", "command_id"), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent table materialization store in RX06 scope."),
    _spec("agent_structure_handoff_attempts", ("attempt_id",), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent Structure handoff store in RX06 scope."),
    _spec("agent_structure_activity_events", ("event_id",), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent Activity store in RX06 scope."),
    _spec("agent_structure_working_revisions", ("revision_id",), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent Structure working-revision store in RX06 scope."),
    _spec("agent_structure_working_revision_heads", ("project_id", "sor_document_id", "source_sha256", "base_artifact_id", "base_projection_sha256"), "project_id = ANY(%s)", "export_only", "Local SQLite has no Agent Structure working-revision store in RX06 scope."),
    _spec("runtime_jobs", ("id",), None, "skipped", "Durable PostgreSQL runtime/worker state is never replayed or copied."),
    _spec("runtime_worker_fleet", ("worker_id",), None, "skipped", "Ephemeral worker identities are never copied."),
    _spec("source_asset_staging_references", ("job_id", "attempt_generation", "storage_key"), None, "skipped", "Transient runtime staging references are not business data."),
    _spec("source_capture_asset_publications", ("publication_id",), None, "skipped", "Transient STAGING/ABORTED publication attempts are not business data."),
    _spec("source_capture_asset_staging_references", ("publication_id", "storage_key"), None, "skipped", "Transient runtime staging references are not business data."),
    _spec("document_extraction_page_staging_references", ("job_id", "attempt_generation", "page_number"), None, "skipped", "Job-generation page staging is not durable business data."),
    _spec("extraction_result_artifact_managed_objects", ("storage_key",), None, "skipped", "Runtime extraction artifact lifecycle is not local business data."),
    _spec("extraction_result_artifact_references", ("job_id",), None, "skipped", "Runtime extraction artifact lifecycle is not local business data."),
    _spec("extraction_result_artifact_staging_references", ("job_id", "attempt_generation"), None, "skipped", "Runtime extraction artifact lifecycle is not local business data."),
    _spec("extraction_result_artifact_cas_operations", ("operation_id",), None, "skipped", "Runtime extraction artifact lifecycle is not local business data."),
    _spec("semantic_provider_runs", ("provider_run_id",), None, "skipped", "Global model-provider audit rows are not bound to selected projects and remain in PostgreSQL."),
    _spec("cutover_migration_runs", ("id",), None, "skipped", "Historical cutover receipts are not business records for the local target."),
    _spec("saas_accounts", ("id",), None, "skipped", "Credential-bearing SaaS identity rows are never exported."),
    _spec("saas_sessions", ("token_sha256",), None, "skipped", "Credential-bearing SaaS session rows are never exported."),
    _spec("saas_auth_attempts", ("bucket",), None, "skipped", "Credential-bearing SaaS authentication rows are never exported."),
    _spec("saas_invitations", ("token_sha256",), None, "skipped", "Credential-bearing SaaS invitation rows are never exported."),
    _spec("saas_project_owners", ("project_id",), None, "skipped", "SaaS identity binding is not part of the local business root."),
)
_TABLE_SPECS = {spec.name: spec for spec in _SPECS}
_IMPORT_ORDER = tuple(spec.name for spec in _SPECS if spec.disposition == "imported")


def _packaged_migrations() -> list[dict[str, str]]:
    migrations: list[dict[str, str]] = []
    for path in sorted(postgres_migration_dir().glob("*.sql")):
        stem = path.stem
        version, separator, name = stem.partition("_")
        if not separator or not version.isdigit() or not name:
            raise Rx06Error(f"PostgreSQL migration filename is not recognized: {path.name}")
        raw = path.read_text(encoding="utf-8").encode("utf-8")
        migrations.append({"version": version, "name": name, "sha256": sha256_bytes(raw)})
    if not migrations:
        raise Rx06Error("No packaged PostgreSQL migrations are installed.")
    return migrations


def _known_pg_tables() -> set[str]:
    names: set[str] = set()
    for path in sorted(postgres_migration_dir().glob("*.sql")):
        for match in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-z][a-z0-9_]*)", path.read_text(encoding="utf-8")):
            names.add(match.group(1))
    names.add("schema_migrations")
    return names


def _assert_specs_cover_schema(known: set[str]) -> None:
    missing = sorted(known - set(_TABLE_SPECS))
    extra = sorted(set(_TABLE_SPECS) - known)
    if missing or extra:
        raise Rx06Error(
            "RX06 table classification changed: missing="
            + ",".join(missing)
            + " extra="
            + ",".join(extra)
        )


def _verify_source_migrations(source: Any) -> list[dict[str, str]]:
    applied = source.applied_migrations()
    packaged = _packaged_migrations()
    expected = {(item["version"], item["name"], item["sha256"]) for item in packaged}
    observed = {
        (str(item["version"]), str(item["name"]), str(item["sha256"])) for item in applied
    }
    if observed != expected:
        unknown = sorted(observed - expected)
        missing = sorted(expected - observed)
        raise Rx06Error(
            "Source PostgreSQL schema is not the exact packaged version: unknown="
            + ",".join(item[0] for item in unknown)
            + " missing="
            + ",".join(item[0] for item in missing)
        )
    return packaged


def _assert_known_source_tables(source: Any) -> set[str]:
    known = _known_pg_tables()
    _assert_specs_cover_schema(known)
    observed = {str(name) for name in source.base_tables()}
    unknown = sorted(observed - known)
    if unknown:
        raise Rx06Error("Source PostgreSQL has unknown tables: " + ", ".join(unknown))
    return known


def _secret_columns(table: str, columns: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for column in columns:
        normalized = str(column).lower()
        if normalized in _SECRET_COLUMN_NAMES or normalized.endswith(_SECRET_COLUMN_SUFFIXES):
            hits.append(f"{table}.{column}")
    return hits


def _collect_keys(value: Any, *, out: dict[str, set[str]], expected: dict[str, set[str]]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_keys(item, out=out, expected=expected)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        normalized = str(key)
        if isinstance(item, str) and (
            normalized in _OBJECT_KEYS
            or normalized.endswith("_storage_key")
            or normalized.endswith("_object_key")
        ):
            storage_key = safe_relative_key(item)
            out.setdefault(storage_key, set())
            for hash_key in (
                "sha256",
                "source_sha256",
                normalized.replace("_key", "_sha256"),
            ):
                candidate = value.get(hash_key)
                if isinstance(candidate, str) and _SHA256_RE.fullmatch(candidate.lower()):
                    expected.setdefault(storage_key, set()).add(candidate.lower())
        _collect_keys(item, out=out, expected=expected)


def _row_object_references(table: str, row: dict[str, Any]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    keys: dict[str, set[str]] = {}
    expected: dict[str, set[str]] = {}
    for key, value in row.items():
        if isinstance(value, str) and (key in _OBJECT_KEYS or key.endswith("_storage_key") or key.endswith("_object_key")):
            storage_key = safe_relative_key(value)
            keys.setdefault(storage_key, set())
            for hash_key in ("sha256", "source_sha256", key.replace("_key", "_sha256")):
                candidate = row.get(hash_key)
                if isinstance(candidate, str) and _SHA256_RE.fullmatch(candidate.lower()):
                    expected.setdefault(storage_key, set()).add(candidate.lower())
        if (table, key) in _JSON_COLUMNS and isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            _collect_keys(parsed, out=keys, expected=expected)
    return keys, expected


def _object_media_type(key: str) -> str:
    suffix = Path(key).suffix.lower()
    return {
        ".json": "application/json",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "application/octet-stream")


def _validate_object_file(root: Path, key: str, expected: set[str], stored: dict[str, Any] | None) -> dict[str, Any]:
    safe_key = safe_relative_key(key)
    path = root / safe_key
    if path.is_symlink() or not path.is_file():
        raise Rx06Error(f"Referenced object is missing or unsafe: {safe_key}")
    digest = sha256_file(path)
    size = path.stat().st_size
    if expected and digest not in expected:
        raise Rx06Error(f"Referenced object hash mismatch: {safe_key}")
    if stored is not None:
        if str(stored.get("sha256")) != digest or int(stored.get("size_bytes")) != size:
            raise Rx06Error(f"Stored object metadata mismatch: {safe_key}")
    return {
        "storage_key": safe_key,
        "path": f"{OBJECTS_DIR}/{safe_key}",
        "sha256": digest,
        "size_bytes": size,
        "media_type": str((stored or {}).get("media_type") or _object_media_type(safe_key)),
        "object_kind": str((stored or {}).get("object_kind") or "referenced"),
    }


def _write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    retained: list[dict[str, Any]] = []
    columns: list[str] = []
    seen: set[str] = set()
    digest = sha256()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        for row in rows:
            normalized = {str(key): json_safe(value) for key, value in row.items()}
            for key in normalized:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
            encoded = (canonical_json(normalized) + "\n").encode("utf-8")
            stream.write(encoded)
            digest.update(encoded)
            retained.append(normalized)
    return {
        "columns": columns,
        "rows": retained,
        "data_size": path.stat().st_size,
        "data_sha256": digest.hexdigest(),
    }


def _load_knowledge_mapping(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"provided": False, "unresolved": [], "mappings": [], "report": {"status": "MISSING", "blockers": ["KNOWLEDGE_MAPPING_MISSING"], "unresolved": [], "unmatched": []}}
    source = require_absolute(path, field="knowledge map")
    if not source.is_file() or source.is_symlink():
        raise Rx06Error("Knowledge mapping input is missing or unsafe.")
    payload = read_json(source)
    if not isinstance(payload, dict) or payload.get("schema_version") != KNOWLEDGE_MAPPING_SCHEMA_VERSION:
        raise Rx06Error("Knowledge mapping schema is unsupported.")
    mappings = payload.get("mappings")
    if not isinstance(mappings, list):
        raise Rx06Error("Knowledge mapping must contain a mappings list.")
    seen: set[tuple[str, str]] = set()
    unresolved: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    for item in mappings:
        if not isinstance(item, dict):
            raise Rx06Error("Knowledge mapping contains an invalid entry.")
        source_kind = str(item.get("source_kind") or "").strip()
        source_id = str(item.get("source_id") or "").strip()
        status = str(item.get("status") or "").strip().upper()
        if not source_kind or not source_id or status not in SUPPORTED_STATUSES:
            raise Rx06Error("Knowledge mapping entry identity or status is invalid.")
        identity = (source_kind, source_id)
        if identity in seen:
            raise Rx06Error("Knowledge mapping contains duplicate source identities.")
        seen.add(identity)
        target_kind = str(item.get("target_kind") or "").strip()
        target_id = str(item.get("target_id") or "").strip()
        if status == "RESOLVED" and (not target_kind or not target_id):
            raise Rx06Error("Resolved knowledge mappings require a target identity.")
        if status != "RESOLVED" and target_id:
            raise Rx06Error("Unresolved knowledge mappings must not claim a target identity.")
        entry = {
            "source_kind": source_kind,
            "source_id": source_id,
            "status": status,
            "target_kind": target_kind,
            "target_id": target_id,
            "target_revision": str(item.get("target_revision") or ""),
            "note": str(item.get("note") or ""),
        }
        normalized.append(entry)
        if status != "RESOLVED":
            unresolved.append(entry)
    return {
        "provided": True,
        "schema_version": KNOWLEDGE_MAPPING_SCHEMA_VERSION,
        "sha256": sha256_file(source),
        "payload": payload,
        "mappings": normalized,
        "unresolved": unresolved,
        "report": {"status": "PROVIDED", "blockers": [], "unresolved": unresolved, "unmatched": []},
    }


def _knowledge_report(mapping: dict[str, Any], exported_documents: list[dict[str, Any]]) -> dict[str, Any]:
    report = dict(mapping.get("report") or {})
    unresolved = list(report.get("unresolved") or [])
    unmatched: list[dict[str, Any]] = []
    blockers: list[str] = list(report.get("blockers") or [])
    required_sources = {
        ("knowledge_document", str(row.get("id"))) for row in exported_documents
    }
    mapped_sources = {
        (str(item.get("source_kind")), str(item.get("source_id")))
        for item in mapping.get("mappings") or []
    }
    missing = sorted(required_sources - mapped_sources)
    if mapping.get("provided"):
        for item in mapping.get("mappings") or []:
            identity = (str(item.get("source_kind")), str(item.get("source_id")))
            if identity[0] == "knowledge_document" and identity not in required_sources:
                unmatched.append(item)
        if missing:
            blockers.append("KNOWLEDGE_MAPPING_INCOMPLETE")
        if unmatched:
            blockers.append("KNOWLEDGE_MAPPING_UNMATCHED_SOURCE")
        if unresolved:
            blockers.append("KNOWLEDGE_MAPPING_UNRESOLVED")
        if not exported_documents and not mapping.get("mappings"):
            blockers = []
            report["status"] = "NOT_REQUIRED"
    elif not exported_documents:
        blockers = []
        report["status"] = "NOT_REQUIRED"
    report["blockers"] = sorted(set(blockers))
    report["unresolved"] = unresolved
    report["missing_source_ids"] = [item[1] for item in missing]
    report["unmatched"] = unmatched
    return report


def _load_export_tables(export_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = read_json(export_dir / MANIFEST_NAME)
    if not isinstance(manifest, dict) or manifest.get("format") != EXPORT_FORMAT or manifest.get("format_version") != EXPORT_FORMAT_VERSION:
        raise Rx06Error("Export manifest format is unsupported.")
    if manifest.get("source", {}).get("schema_migrations") != _packaged_migrations():
        raise Rx06Error("Export was created for a different packaged PostgreSQL schema version.")
    if not isinstance(manifest.get("tables"), list):
        raise Rx06Error("Export manifest tables are invalid.")
    table_entries: dict[str, dict[str, Any]] = {}
    for entry in manifest["tables"]:
        if not isinstance(entry, dict):
            raise Rx06Error("Export manifest contains an invalid table entry.")
        name = str(entry.get("name") or "")
        if name in table_entries:
            raise Rx06Error(f"Export contains duplicate table entries: {name}")
        if name not in _TABLE_SPECS:
            raise Rx06Error(f"Export contains an unsupported table: {name}")
        spec = _TABLE_SPECS[name]
        if entry.get("primary_key") != list(spec.primary_key):
            raise Rx06Error(f"Export primary key is unsupported for {name}.")
        if entry.get("disposition") != spec.disposition:
            raise Rx06Error(f"Export table disposition is invalid for {name}.")
        columns = entry.get("columns")
        if not isinstance(columns, list) or len({str(column) for column in columns}) != len(columns):
            raise Rx06Error(f"Export table columns are invalid for {name}.")
        if any(_SAFE_IDENTIFIER_RE.fullmatch(str(column)) is None for column in columns):
            raise Rx06Error(f"Export table column identifiers are unsafe for {name}.")
        hits = _secret_columns(name, columns)
        if hits:
            raise Rx06Error("Export contains credential-like columns: " + ", ".join(hits))
        data_path = safe_relative_file(entry.get("data_path"))
        path = export_dir / data_path
        if not path.is_file() or path.is_symlink():
            raise Rx06Error(f"Export table file is missing: {name}")
        actual_size = path.stat().st_size
        actual_sha = sha256_file(path)
        if actual_size != int(entry.get("data_size", -1)) or actual_sha != entry.get("data_sha256"):
            raise Rx06Error(f"Export table file was changed: {name}")
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    raise Rx06Error(f"Export table {name} has a blank row at line {line_number}.")
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise Rx06Error(f"Export table {name} is not valid JSONL.") from exc
                if not isinstance(row, dict):
                    raise Rx06Error(f"Export table {name} row is not an object.")
                rows.append(row)
        if len(rows) != int(entry.get("row_count", -1)):
            raise Rx06Error(f"Export table row count changed: {name}")
        if records_digest(rows) != entry.get("records_sha256"):
            raise Rx06Error(f"Export table row digest changed: {name}")
        if primary_key_digest(rows, spec.primary_key) != entry.get("primary_key_sha256"):
            raise Rx06Error(f"Export table primary key digest changed: {name}")
        if any(str(key) not in {str(column) for column in columns} for row in rows for key in row):
            raise Rx06Error(f"Export table {name} contains undeclared columns.")
        table_entries[name] = entry
    missing_imported = [name for name in _IMPORT_ORDER if name not in table_entries]
    if missing_imported:
        raise Rx06Error("Export is missing imported tables: " + ", ".join(missing_imported))
    selection = manifest.get("source", {}).get("selection") or {}
    project_ids = [str(item).strip() for item in (selection.get("project_ids") or [])]
    if (
        selection.get("mode") != "explicit-projects"
        or not project_ids
        or any(not item for item in project_ids)
        or sorted(set(project_ids)) != project_ids
    ):
        raise Rx06Error("Export selection is invalid.")
    return manifest, table_entries


def _iter_export_rows(export_dir: Path, entry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    path = export_dir / safe_relative_file(entry.get("data_path"))
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def validate_export(export_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    directory = require_absolute(export_dir, field="export directory")
    if not directory.is_dir() or directory.is_symlink():
        raise Rx06Error("Export directory is missing or unsafe.")
    manifest, table_entries = _load_export_tables(directory)
    objects = manifest.get("objects")
    if not isinstance(objects, list):
        raise Rx06Error("Export object list is invalid.")
    seen: set[str] = set()
    for entry in objects:
        if not isinstance(entry, dict):
            raise Rx06Error("Export contains an invalid object entry.")
        key = safe_relative_key(entry.get("storage_key"))
        if key in seen:
            raise Rx06Error("Export contains duplicate object keys.")
        seen.add(key)
        path = directory / safe_relative_file(entry.get("path"))
        if not path.is_file() or path.is_symlink():
            raise Rx06Error(f"Export object file is missing: {key}")
        if path.stat().st_size != int(entry.get("size_bytes", -1)) or sha256_file(path) != entry.get("sha256"):
            raise Rx06Error(f"Export object hash mismatch: {key}")
        if require_sha256(entry.get("sha256"), field=f"object {key}") != sha256_file(path):
            raise Rx06Error(f"Export object identity mismatch: {key}")
    mapping_entry = manifest.get("knowledge_mapping") or {}
    if mapping_entry.get("provided"):
        mapping_path = directory / safe_relative_file(mapping_entry.get("path"))
        if not mapping_path.is_file() or mapping_path.is_symlink() or sha256_file(mapping_path) != mapping_entry.get("sha256"):
            raise Rx06Error("Export knowledge mapping was changed.")
    return manifest, table_entries


class _PsycopgSource:
    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg import IsolationLevel
        except ImportError as exc:
            raise Rx06Error("PostgreSQL export requires the optional psycopg package.") from exc
        self._psycopg = psycopg
        try:
            self._connection = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
        except Exception as exc:
            raise Rx06Error("PostgreSQL source connection failed; no URL or credential was logged.") from exc
        self._connection.isolation_level = IsolationLevel.REPEATABLE_READ
        self._connection.read_only = True
        identity = self._connection.execute(
            "SELECT current_database() AS database_name, current_setting('server_version') AS server_version"
        ).fetchone()
        self.database_name = str((identity or {}).get("database_name") or "")
        self.server_version = str((identity or {}).get("server_version") or "")
        transaction = self._connection.execute(
            "SELECT current_setting('transaction_read_only') AS read_only, "
            "current_setting('transaction_isolation') AS isolation"
        ).fetchone()
        if (
            str((transaction or {}).get("read_only") or "").lower() not in {"on", "true"}
            or str((transaction or {}).get("isolation") or "").lower() != "repeatable read"
        ):
            raise Rx06Error("PostgreSQL export transaction is not repeatable-read read-only.")

    def close(self) -> None:
        self._connection.rollback()
        self._connection.close()

    def applied_migrations(self) -> list[dict[str, Any]]:
        try:
            rows = self._connection.execute(
                "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
            ).fetchall()
        except Exception as exc:
            raise Rx06Error("Source PostgreSQL has no readable schema_migrations ledger.") from exc
        return [dict(row) for row in rows]

    def base_tables(self) -> set[str]:
        try:
            rows = self._connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
                """
            ).fetchall()
        except Exception as exc:
            raise Rx06Error("Source PostgreSQL table inventory failed.") from exc
        return {str(row["table_name"]) for row in rows}

    def fetch_rows(
        self,
        table: str,
        filter_sql: str | None,
        params: tuple[Any, ...],
        order_by: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if _SAFE_IDENTIFIER_RE.fullmatch(table) is None:
            raise Rx06Error("Unsafe source table name.")
        order = ", ".join(f'"{column}"' for column in order_by)
        where = f" WHERE {filter_sql}" if filter_sql else ""
        sql = f'SELECT * FROM "{table}"{where} ORDER BY {order}'
        try:
            rows = self._connection.execute(sql, params).fetchall()
        except Exception as exc:
            raise Rx06Error(f"Source query failed for table {table}.") from exc
        return [dict(row) for row in rows]

    def fetch_by_keys(self, table: str, key_column: str, keys: list[str]) -> list[dict[str, Any]]:
        if not keys:
            return []
        if _SAFE_IDENTIFIER_RE.fullmatch(table) is None or _SAFE_IDENTIFIER_RE.fullmatch(key_column) is None:
            raise Rx06Error("Unsafe source table or key column name.")
        try:
            rows = self._connection.execute(
                f'SELECT * FROM "{table}" WHERE "{key_column}" = ANY(%s) ORDER BY "{key_column}"',
                (keys,),
            ).fetchall()
        except Exception as exc:
            raise Rx06Error(f"Source query failed for table {table}.") from exc
        return [dict(row) for row in rows]


def _export_selected(
    source: Any,
    *,
    project_ids: list[str],
    object_root: Path,
    output_dir: Path,
    knowledge_map_path: Path | None,
) -> dict[str, Any]:
    projects = sorted({str(item).strip() for item in project_ids if str(item).strip()})
    if not projects:
        raise Rx06Error("At least one explicit source project id is required.")
    packaged = _verify_source_migrations(source)
    _assert_known_source_tables(source)
    selected_rows = source.fetch_rows("projects", "id = ANY(%s)", (projects,), ("id",))
    selected_ids = sorted(str(row.get("id")) for row in selected_rows)
    if selected_ids != projects:
        missing = sorted(set(projects) - set(selected_ids))
        raise Rx06Error("Selected source projects are missing: " + ", ".join(missing))
    source_root = require_absolute(object_root, field="object root")
    if not source_root.is_dir() or source_root.is_symlink():
        raise Rx06Error("Source object root is missing or unsafe.")
    output = require_absolute(output_dir, field="export output")
    if output.exists():
        raise Rx06Error("Export output already exists; existing exports are never overwritten.")
    if (output.parent / "app.db").is_file() and (output.parent / "objects").is_dir():
        raise Rx06Error("Export output must not be created inside an application root.")
    if is_within(output, source_root) or is_within(source_root, output):
        raise Rx06Error("Export output and source object root must not contain one another.")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid.uuid4().hex}.tmp"
    mapping = _load_knowledge_mapping(knowledge_map_path)
    try:
        staging.mkdir(mode=0o700)
        secure_directory(staging)
        (staging / TABLES_DIR).mkdir(mode=0o700)
        (staging / OBJECTS_DIR).mkdir(mode=0o700)
        table_payloads: dict[str, list[dict[str, Any]]] = {}
        object_keys: dict[str, set[str]] = {}
        object_expected: dict[str, set[str]] = {}
        for spec in _SPECS:
            if spec.disposition == "skipped" or spec.name in {"stored_objects", "source_asset_managed_objects"}:
                continue
            params = (projects, projects) if spec.name == "entry_command_receipts" else (projects,)
            rows = source.fetch_rows(spec.name, spec.filter_sql, params, spec.primary_key)
            table_payloads[spec.name] = rows
            for row in rows:
                keys, expected = _row_object_references(spec.name, row)
                for key, hashes in keys.items():
                    object_keys.setdefault(key, set()).update(hashes)
                for key, hashes in expected.items():
                    object_expected.setdefault(key, set()).update(hashes)
                hits = _secret_columns(spec.name, row.keys())
                if hits:
                    raise Rx06Error("Source contains credential-like columns: " + ", ".join(hits))
        all_keys = sorted(object_keys)
        stored_rows = source.fetch_by_keys("stored_objects", "storage_key", all_keys)
        stored_by_key = {str(row["storage_key"]): row for row in stored_rows}
        managed_rows = source.fetch_by_keys("source_asset_managed_objects", "storage_key", all_keys)
        table_payloads["stored_objects"] = stored_rows
        table_payloads["source_asset_managed_objects"] = managed_rows
        object_entries: list[dict[str, Any]] = []
        for key in all_keys:
            expected = set(object_expected.get(key, set()))
            stored = stored_by_key.get(key)
            if stored is not None:
                expected.add(str(stored.get("sha256")))
            entry = _validate_object_file(source_root, key, expected, stored)
            source_path = source_root / key
            target_path = staging / entry["path"]
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with source_path.open("rb") as reader, target_path.open("wb") as writer:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            secure_file(target_path)
            if sha256_file(target_path) != entry["sha256"]:
                raise Rx06Error(f"Object changed while it was exported: {key}")
            object_entries.append(entry)
        table_entries: list[dict[str, Any]] = []
        for spec in _SPECS:
            if spec.disposition == "skipped":
                continue
            rows = table_payloads.get(spec.name, [])
            path = staging / TABLES_DIR / f"{spec.name}.jsonl"
            written = _write_rows(path, rows)
            columns = written["columns"]
            hits = _secret_columns(spec.name, columns)
            if hits:
                raise Rx06Error("Source contains credential-like columns: " + ", ".join(hits))
            table_entries.append(
                {
                    "name": spec.name,
                    "disposition": spec.disposition,
                    "primary_key": list(spec.primary_key),
                    "columns": columns,
                    "row_count": len(written["rows"]),
                    "data_path": f"{TABLES_DIR}/{spec.name}.jsonl",
                    "data_size": written["data_size"],
                    "data_sha256": written["data_sha256"],
                    "records_sha256": records_digest(written["rows"]),
                    "primary_key_sha256": primary_key_digest(written["rows"], spec.primary_key),
                    "note": spec.note,
                }
            )
        knowledge_entry: dict[str, Any] = {"provided": bool(mapping.get("provided"))}
        if mapping.get("provided"):
            target = staging / KNOWLEDGE_MAPPING_FILE
            target.write_text(json.dumps(mapping["payload"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            secure_file(target)
            knowledge_entry.update({"path": KNOWLEDGE_MAPPING_FILE, "sha256": sha256_file(target)})
        knowledge_entry["report"] = _knowledge_report(mapping, table_payloads.get("knowledge_documents", []))
        manifest = {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "export_id": f"rx06-export-{uuid.uuid4().hex}",
            "source": {
                "database_name": str(getattr(source, "database_name", "") or ""),
                "server_version": str(getattr(source, "server_version", "") or ""),
                "selection": {"mode": "explicit-projects", "project_ids": projects},
                "consistency": "REPEATABLE_READ_READ_ONLY",
                "schema_migrations": packaged,
                "schema_fingerprint_sha256": value_digest(
                    canonical_json(item) for item in packaged
                ),
            },
            "tables": table_entries,
            "objects": object_entries,
            "knowledge_mapping": knowledge_entry,
            "excluded_tables": [
                {"name": spec.name, "reason": spec.note}
                for spec in _SPECS
                if spec.disposition == "skipped" and spec.name != "schema_migrations"
            ],
        }
        write_json(staging / MANIFEST_NAME, manifest)
        validate_export(staging)
        rename_no_replace(staging, output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _inventory(source: Any, *, project_ids: list[str], object_root: Path) -> dict[str, Any]:
    projects = sorted({str(item).strip() for item in project_ids if str(item).strip()})
    if not projects:
        raise Rx06Error("At least one explicit source project id is required.")
    packaged = _verify_source_migrations(source)
    _assert_known_source_tables(source)
    source_root = require_absolute(object_root, field="object root")
    if not source_root.is_dir() or source_root.is_symlink():
        raise Rx06Error("Source object root is missing or unsafe.")
    selected = source.fetch_rows("projects", "id = ANY(%s)", (projects,), ("id",))
    selected_ids = sorted(str(row.get("id")) for row in selected)
    if selected_ids != projects:
        raise Rx06Error("Selected source projects are missing.")
    counts: dict[str, int] = {}
    object_keys: set[str] = set()
    active_jobs = 0
    knowledge_documents = 0
    for spec in _SPECS:
        if spec.disposition == "skipped" or spec.name in {"stored_objects", "source_asset_managed_objects"}:
            continue
        params = (projects, projects) if spec.name == "entry_command_receipts" else (projects,)
        rows = source.fetch_rows(spec.name, spec.filter_sql, params, spec.primary_key)
        counts[spec.name] = len(rows)
        if spec.name == "knowledge_documents":
            knowledge_documents = len(rows)
        if spec.name == "ai_analysis_jobs":
            active_jobs = sum(
                str(row.get("status") or "").upper() in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}
                for row in rows
            )
        for row in rows:
            keys, _expected = _row_object_references(spec.name, row)
            object_keys.update(keys)
    object_bytes = 0
    missing_objects: list[str] = []
    for key in sorted(object_keys):
        path = source_root / safe_relative_key(key)
        if path.is_file() and not path.is_symlink():
            object_bytes += path.stat().st_size
        else:
            missing_objects.append(key)
    return {
        "format": "reqpilotx-rx06-inventory/v1",
        "selection": {"mode": "explicit-projects", "project_ids": projects},
        "schema": {"migrations": packaged, "fingerprint_sha256": value_digest(canonical_json(item) for item in packaged)},
        "tables": counts,
        "objects": {"count": len(object_keys), "bytes": object_bytes, "missing": missing_objects, "storage_keys": sorted(object_keys)},
        "active_analysis_jobs": active_jobs,
        "knowledge_documents": knowledge_documents,
        "writes_source": False,
    }


def _bootstrap_target(root: Path) -> None:
    import reqpilotx_server

    package_parent = str(Path(reqpilotx_server.__file__).resolve().parents[1])
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("REQPILOT_", "REQPILOTX_"))
    }
    environment.update(
        {
            "REQPILOT_ENV": "local",
            "REQPILOT_DATABASE_BACKEND": "sqlite-local",
            "REQPILOT_STORAGE_DIR": str(root),
            "REQPILOT_OBJECT_ROOT": str(root),
            "REQPILOT_SCRATCH_ROOT": str(root.parent / f".{root.name}.scratch"),
            "REQPILOT_ALLOWED_HOSTS": "127.0.0.1,localhost",
            "PYTHONPATH": os.pathsep.join(
                [package_parent, *[part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]]
            ),
        }
    )
    command = [sys.executable, "-m", "reqpilotx_server.tools.rx06_migration", "_bootstrap-target", "--root", str(root)]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise Rx06Error("Target schema bootstrap failed: " + (result.stderr.strip() or result.stdout.strip()))


def _bootstrap_target_in_process(root: Path) -> None:
    from reqpilotx_server import db
    from reqpilotx_server.adapters.persistence.source_item_delivery_versions import ensure_delivery_versions_schema
    from reqpilotx_server.adapters.persistence.source_item_manual_decisions import ensure_manual_schema
    from reqpilotx_server.adapters.persistence import sql_llm_requirement_reviews as llm
    from reqpilotx_server.adapters.persistence import sql_requirement_enrichment as enrichment
    from reqpilotx_server.adapters.persistence.requirement_matrix_states import _ensure_schema as ensure_matrix
    from reqpilotx_server.adapters.persistence.requirement_set_impacts import _ensure_schema as ensure_impacts
    from reqpilotx_server.adapters.persistence.sql_reading import _ensure_sqlite_chat_schema
    from reqpilotx_server.adapters.persistence.sqlite_extraction import SQLiteSourceIRRepository
    from reqpilotx_server.infrastructure.runtime_settings import RuntimeSettings

    settings = RuntimeSettings.from_environment(
        environment={
            "REQPILOT_ENV": "local",
            "REQPILOT_DATABASE_BACKEND": "sqlite-local",
            "REQPILOT_STORAGE_DIR": str(root),
            "REQPILOT_OBJECT_ROOT": str(root),
            "REQPILOT_SCRATCH_ROOT": str(root.parent / f".{root.name}.scratch"),
            "REQPILOT_ALLOWED_HOSTS": "127.0.0.1,localhost",
        },
        prototype_dir=None,
    )
    saved = (db._RUNTIME_SETTINGS, db._POSTGRES_DATABASE, db.STORAGE_DIR, db.DB_PATH)
    try:
        db.configure_runtime(settings)
        db.STORAGE_DIR = root
        db.DB_PATH = root / "app.db"
        db.init_db()
        _ensure_sqlite_chat_schema()
        with db.get_conn() as conn:
            ensure_manual_schema(conn)
            ensure_delivery_versions_schema(conn)
            for statement in (
                llm._SESSION_SCHEMA,
                llm._EVENT_SCHEMA,
                llm._CONFIRMATION_EVENT_SCHEMA,
                llm._INDEX_SCHEMA,
                llm._CONFIRMATION_EVENT_INDEX_SCHEMA,
                enrichment._ATOMIZATION_CHECKPOINT_SCHEMA,
                enrichment._ATOMIZATION_SCHEMA,
                enrichment._ATOMIZATION_SCOPE_SCHEMA,
                enrichment._ATOMIZATION_SCOPE_ATTEMPT_SCHEMA,
                enrichment._REFINEMENT_SCHEMA,
                enrichment._REFINEMENT_EVENT_SCHEMA,
                enrichment._PUBLICATION_PREVIEW_SCHEMA,
                enrichment._PUBLICATION_SCHEMA,
                enrichment._PUBLICATION_BINDING_PREVIEW_V2_SCHEMA,
                enrichment._PUBLICATION_BINDING_V2_SCHEMA,
                enrichment._PUBLICATION_ROW_BINDING_V2_SCHEMA,
                enrichment._RUN_INDEX,
                enrichment._SCOPE_INDEX,
                enrichment._SCOPE_ATTEMPT_INDEX,
                enrichment._SCOPE_ATTEMPT_COMMAND_INDEX,
                enrichment._SCOPE_ATTEMPT_RUNNING_INDEX,
                enrichment._REFINEMENT_INDEX,
                enrichment._PREVIEW_INDEX,
                enrichment._PUBLICATION_BINDING_PREVIEW_V2_INDEX,
            ):
                conn.executescript(statement)
            ensure_matrix(conn)
            ensure_impacts(conn)
            SQLiteSourceIRRepository(conn)
    finally:
        db.configure_runtime(saved[0], postgres_database=saved[1])
        db.STORAGE_DIR = saved[2]
        db.DB_PATH = saved[3]


def _target_schema_fingerprint(database_path: Path) -> str:
    conn = sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        objects = [
            {"type": row["type"], "name": row["name"], "sql": row["sql"]}
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ]
        ledger = [
            dict(row)
            for row in conn.execute(
                "SELECT version, name, sha256 FROM reqpilotx_local_schema_migrations ORDER BY version"
            ).fetchall()
        ]
        return value_digest([canonical_json({"objects": objects, "ledger": ledger})])
    finally:
        conn.close()


def _target_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not rows:
        raise Rx06Error(f"Target table is missing: {table}")
    return {str(row[1]): str(row[2] or "").upper() for row in rows}


def _object_entry_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["storage_key"]): item for item in manifest.get("objects", [])}


def _target_object_path(target_root: Path, key: str) -> str:
    return str((target_root / OBJECTS_DIR / safe_relative_key(key)).resolve(strict=False))


def _normalize_value(value: Any, target_type: str, *, target_column: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value) if target_type.startswith("INT") else value
    if isinstance(value, (dict, list)):
        if "TEXT" not in target_type and target_type:
            raise Rx06Error(f"Target column {target_column} cannot store structured JSON.")
        return canonical_json(value)
    if hasattr(value, "as_integer_ratio"):
        return int(value) if target_type.startswith("INT") else float(value)
    return value


def _quarantine_active_rows(table: str, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    now = datetime.now(timezone.utc).isoformat()
    if table == "ai_analysis_jobs" and str(result.get("status") or "").upper() in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}:
        result["status"] = "FAILED"
        result["error_message"] = "Interrupted by RX06 PostgreSQL-to-SQLite migration; explicitly rerun this job."
        result["finished_at"] = str(result.get("finished_at") or now)
        result["updated_at"] = result["finished_at"]
    elif table == "ai_analysis_job_targets" and str(result.get("status") or "").upper() in {"WAITING", "RUNNING", "PUBLISHING"}:
        result["status"] = "FAILED"
        result["retryable"] = 1
        result["error_code"] = "rx06_migration_interrupted"
        result["error_message"] = "Interrupted by RX06 migration; retry this node explicitly."
        result["updated_at"] = now
    elif table == "ai_analysis_node_attempts" and str(result.get("status") or "").upper() == "RUNNING":
        result["status"] = "FAILED"
        result["error_code"] = "rx06_migration_interrupted"
        result["error_message"] = "Interrupted by RX06 migration before publication."
        result["finished_at"] = str(result.get("finished_at") or now)
        result["updated_at"] = result["finished_at"]
    return result


def _validate_manual_rows_real(rows: list[dict[str, Any]], document_sources: dict[tuple[str, str], str]) -> None:
    from reqpilotx.application.agent_workflow.source_item_manual_decisions import validate_manual_decisions

    by_document: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_document.setdefault((str(row["project_id"]), str(row["sor_document_id"])), []).append(row)
    for (project_id, document_id), items in by_document.items():
        items.sort(key=lambda item: int(item["revision"]))
        previous_sha = ""
        for index, item in enumerate(items, 1):
            if int(item["revision"]) != index:
                raise Rx06Error(f"Manual decision revisions are not contiguous for {project_id}/{document_id}.")
            try:
                artifact = json.loads(str(item["artifact_json"]))
            except json.JSONDecodeError as exc:
                raise Rx06Error("Manual decision artifact is not valid JSON.") from exc
            validate_manual_decisions(artifact)
            if artifact.get("artifact_sha256") != item.get("artifact_sha256"):
                raise Rx06Error("Manual decision artifact hash does not match its row.")
            if str(artifact.get("predecessor_sha256") or "") != previous_sha:
                raise Rx06Error("Manual decision predecessor chain is broken.")
            require_sha256(artifact.get("source_sha256"), field="manual decision source_sha256")
            if str(item.get("command_id") or "") == "":
                raise Rx06Error("Manual decision command id is blank.")
            previous_sha = str(item["artifact_sha256"])


def _validate_delivery_rows(rows: list[dict[str, Any]], objects: dict[str, dict[str, Any]]) -> None:
    from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

    by_document: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_document.setdefault((str(row["project_id"]), str(row["sor_document_id"])), []).append(row)
    for (project_id, document_id), items in by_document.items():
        items.sort(key=lambda item: int(item["revision"]))
        for index, item in enumerate(items, 1):
            if int(item["revision"]) != index:
                raise Rx06Error(f"Delivery revisions are not contiguous for {project_id}/{document_id}.")
            try:
                artifact = json.loads(str(item["artifact_json"]))
            except json.JSONDecodeError as exc:
                raise Rx06Error("Delivery artifact is not valid JSON.") from exc
            artifact_body = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
            if canonical_json_sha256(artifact_body) != item.get("artifact_sha256"):
                raise Rx06Error("Delivery artifact hash does not match its row.")
            if artifact.get("artifact_sha256") not in {None, item.get("artifact_sha256")}:
                raise Rx06Error("Delivery embedded artifact hash does not match its row.")
            snapshot = artifact.get("snapshot") or {}
            if canonical_json_sha256({key: value for key, value in snapshot.items() if key != "delivery_sha256"}) != snapshot.get("delivery_sha256"):
                raise Rx06Error("Delivery snapshot hash does not match its artifact.")
            source = snapshot.get("source_item_baseline") or {}
            require_sha256(source.get("source_sha256"), field="delivery source_sha256")
            key = safe_relative_key(artifact.get("export_key"))
            entry = objects.get(key)
            if entry is None:
                raise Rx06Error("Delivery workbook object is missing from the export.")
            if require_sha256(artifact.get("export_sha256"), field="delivery export_sha256") != entry.get("sha256"):
                raise Rx06Error("Delivery workbook hash does not match the exported object.")
            workbook_path = entry.get("_absolute_path")
            if not isinstance(workbook_path, Path) or not workbook_path.is_file():
                raise Rx06Error("Delivery workbook bytes are missing.")
            with zipfile.ZipFile(workbook_path) as archive:
                if "xl/workbook.xml" not in archive.namelist():
                    raise Rx06Error("Delivery workbook is not an XLSX workbook.")


def _materialize_objects(export_dir: Path, target_root: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest.get("objects", []):
        key = safe_relative_key(entry.get("storage_key"))
        source = export_dir / safe_relative_file(entry.get("path"))
        target = target_root / OBJECTS_DIR / key
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, target.open("wb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        secure_file(target)
        if target.stat().st_size != int(entry.get("size_bytes", -1)) or sha256_file(target) != entry.get("sha256"):
            raise Rx06Error(f"Object changed while it was materialized: {key}")
        entry["_absolute_path"] = target


def _load_documents(export_dir: Path, table_entries: dict[str, dict[str, Any]]) -> dict[tuple[str, str], str]:
    entry = table_entries.get("sor_documents")
    if entry is None:
        return {}
    return {
        (str(row["project_id"]), str(row["id"])): str(row["source_sha256"] or "")
        for row in _iter_export_rows(export_dir, entry)
    }


def _populate_target(
    *,
    export_dir: Path,
    target_root: Path,
    staging: Path,
    manifest: dict[str, Any],
    table_entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    database_path = staging / "app.db"
    objects = _object_entry_map(manifest)
    document_sources = _load_documents(export_dir, table_entries)
    manual_rows = list(_iter_export_rows(export_dir, table_entries["source_item_manual_decision_versions"])) if "source_item_manual_decision_versions" in table_entries else []
    delivery_rows = list(_iter_export_rows(export_dir, table_entries["source_item_delivery_versions"])) if "source_item_delivery_versions" in table_entries else []
    if manual_rows:
        _validate_manual_rows_real(manual_rows, document_sources)
    if delivery_rows:
        _validate_delivery_rows(delivery_rows, objects)
    target_table_digests: dict[str, dict[str, Any]] = {}
    quarantined: list[dict[str, str]] = []
    conn = sqlite3.connect(database_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in _IMPORT_ORDER:
            entry = table_entries.get(table)
            spec = _TABLE_SPECS[table]
            if entry is None or spec.disposition != "imported":
                continue
            target_columns = _target_columns(conn, table)
            aliases = _FILE_COLUMN_ALIASES.get(table, ("", ""))
            dropped = _DROPPED_SOURCE_COLUMNS.get(table, {})
            source_columns = [str(column) for column in entry["columns"]]
            for column in source_columns:
                if column in target_columns or column == aliases[0] or column in dropped:
                    continue
                raise Rx06Error(f"Target schema would drop {table}.{column}.")
            rows = manual_rows if table == "source_item_manual_decision_versions" else delivery_rows if table == "source_item_delivery_versions" else _iter_export_rows(export_dir, entry)
            normalized_rows: list[dict[str, Any]] = []
            for raw in rows:
                row = _quarantine_active_rows(table, raw)
                if row != raw:
                    quarantined.append({"table": table, "id": str(row.get("id")), "from_status": str(raw.get("status")), "to_status": str(row.get("status"))})
                payload: dict[str, Any] = {}
                for source_column, value in row.items():
                    if source_column in dropped:
                        continue
                    target_column = aliases[1] if source_column == aliases[0] else source_column
                    if target_column not in target_columns:
                        continue
                    if source_column == aliases[0] and aliases[0]:
                        raw_key = str(value or "").strip()
                        if not raw_key:
                            if table == "knowledge_document_syncs":
                                payload[target_column] = ""
                                continue
                            raise Rx06Error(f"{table}.{source_column} must not be blank.")
                        key = safe_relative_key(raw_key)
                        object_entry = objects.get(key)
                        if object_entry is None:
                            raise Rx06Error(f"Missing object for {table}.{source_column}: {key}")
                        if target_column == "payload_path":
                            payload[target_column] = _target_object_path(target_root, key)
                        else:
                            payload[target_column] = _target_object_path(target_root, key)
                        if table in {"sor_documents", "knowledge_documents"}:
                            expected_hash = str(row.get("source_sha256") or "").lower()
                            if expected_hash and expected_hash != object_entry.get("sha256"):
                                raise Rx06Error(f"Source hash does not match object bytes for {table}.{row.get('id')}.")
                            payload["source_sha256"] = object_entry["sha256"]
                            payload["source_size"] = int(object_entry["size_bytes"])
                        continue
                    payload[target_column] = _normalize_value(value, target_columns[target_column], target_column=target_column)
                normalized_rows.append(payload)
            if normalized_rows:
                columns = [str(column) for column in entry["columns"] if column not in dropped and (column in target_columns or column == aliases[0])]
                target_names = [aliases[1] if column == aliases[0] else column for column in columns]
                for name in ("source_sha256", "source_size"):
                    if table in {"sor_documents", "knowledge_documents"} and name not in target_names and name in target_columns:
                        target_names.append(name)
                        columns.append(name)
                placeholders = ", ".join("?" for _ in target_names)
                column_sql = ", ".join(f'"{column}"' for column in target_names)
                conn.executemany(
                    f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
                    [tuple(row.get(column) for column in target_names) for row in normalized_rows],
                )
            target_rows = [
                {key: row[key] for key in row.keys()}
                for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()
            ]
            target_table_digests[table] = {
                "row_count": len(target_rows),
                "records_sha256": records_digest(target_rows),
                "primary_key_sha256": primary_key_digest(target_rows, spec.primary_key),
                "primary_key": list(spec.primary_key),
            }
            if len(target_rows) != len(normalized_rows):
                raise Rx06Error(f"Target row count does not match imported rows for {table}.")
            if len(target_rows) != int(entry.get("row_count") or 0):
                raise Rx06Error(f"Target row count does not match export for {table}.")
            if target_table_digests[table]["primary_key_sha256"] != entry.get("primary_key_sha256"):
                raise Rx06Error(f"Target primary key digest does not match export for {table}.")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "target_table_digests": target_table_digests,
        "active_jobs_quarantined": quarantined,
    }


def _knowledge_target_report(manifest: dict[str, Any]) -> dict[str, Any]:
    return dict((manifest.get("knowledge_mapping") or {}).get("report") or {})


def _activation_blockers(manifest: dict[str, Any], table_entries: dict[str, dict[str, Any]]) -> list[str]:
    blockers = list(_knowledge_target_report(manifest).get("blockers") or [])
    export_only = [
        name
        for name, entry in table_entries.items()
        if entry.get("disposition") == "export_only" and int(entry.get("row_count") or 0) > 0
    ]
    if export_only:
        blockers.append("UNSUPPORTED_BUSINESS_TABLES_RETAINED_EXPORT_ONLY")
    return sorted(set(blockers))


def _verify_domain_records(conn: sqlite3.Connection, root: Path, receipt: dict[str, Any]) -> dict[str, int]:
    document_sources = {
        (str(row["project_id"]), str(row["id"])): str(row["source_sha256"] or "")
        for row in conn.execute("SELECT id, project_id, source_sha256 FROM sor_documents").fetchall()
    }
    manual = [
        {key: row[key] for key in row.keys()}
        for row in conn.execute("SELECT * FROM source_item_manual_decision_versions").fetchall()
    ]
    if manual:
        _validate_manual_rows_real(manual, document_sources)
    delivery = [
        {key: row[key] for key in row.keys()}
        for row in conn.execute("SELECT * FROM source_item_delivery_versions").fetchall()
    ]
    object_index = receipt.get("object_index") or {}
    if delivery:
        objects: dict[str, dict[str, Any]] = {}
        for key, entry in object_index.items():
            objects[key] = {**entry, "_absolute_path": root / OBJECTS_DIR / safe_relative_key(key)}
        _validate_delivery_rows(delivery, objects)
    return {"manual_decision_versions": len(manual), "delivery_versions": len(delivery)}


def _verify_target_root(
    root: Path,
    *,
    receipt: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    stored_root: Path | None = None,
) -> dict[str, Any]:
    database_path = Path(root) / "app.db"
    if not database_path.is_file() or database_path.is_symlink():
        raise Rx06Error("Target root does not contain a safe app.db.")
    conn = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise Rx06Error("Target SQLite integrity_check failed.")
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise Rx06Error("Target SQLite foreign_key_check found violations.")
        active_jobs = conn.execute(
            "SELECT count(*) FROM ai_analysis_jobs WHERE status IN ('PENDING','RUNNING','CANCEL_REQUESTED')"
        ).fetchone()[0]
        if int(active_jobs):
            raise Rx06Error("Target still contains active analysis jobs; RX06 never replays them.")
        if receipt is not None:
            if _target_schema_fingerprint(database_path) != receipt.get("target_schema_fingerprint_sha256"):
                raise Rx06Error("Target SQLite schema fingerprint does not match the receipt.")
            for name, expected in receipt.get("tables", {}).items():
                if name not in _TABLE_SPECS:
                    raise Rx06Error(f"Receipt contains an unsupported table: {name}")
                spec = _TABLE_SPECS[name]
                primary_key = tuple(expected.get("primary_key") or spec.primary_key)
                rows = [{key: row[key] for key in row.keys()} for row in conn.execute(f'SELECT * FROM "{name}"').fetchall()]
                if len(rows) != int(expected["row_count"]):
                    raise Rx06Error(f"Target row count changed: {name}")
                if records_digest(rows) != expected["records_sha256"]:
                    raise Rx06Error(f"Target row digest changed: {name}")
                if primary_key_digest(rows, primary_key) != expected["primary_key_sha256"]:
                    raise Rx06Error(f"Target primary key digest changed: {name}")
        reference_counts = verify_runtime_references(database_path, root, stored_root=stored_root)
        domain_counts = _verify_domain_records(conn, root, receipt) if receipt is not None else {}
    finally:
        conn.close()
    return {
        "ok": True,
        "integrity": "ok",
        "foreign_key_violations": 0,
        "active_analysis_jobs": 0,
        "references": reference_counts,
        "domain_records": domain_counts,
    }


def import_export(export_dir: Path, target_root: Path) -> dict[str, Any]:
    source = require_absolute(export_dir, field="export directory")
    target = require_absolute(target_root, field="target root")
    if not source.is_dir() or source.is_symlink():
        raise Rx06Error("Export directory is missing or unsafe.")
    if target.exists():
        raise Rx06Error("Target root already exists; RX06 never overwrites an existing root.")
    if (target.parent / "app.db").is_file() and (target.parent / "objects").is_dir():
        raise Rx06Error("Target root must not be created inside an existing application root.")
    if is_within(target, source) or is_within(source, target):
        raise Rx06Error("Target root and export directory must not contain one another.")
    manifest, table_entries = validate_export(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.rx06-{uuid.uuid4().hex}.tmp"
    incomplete = staging / ".RX06-INCOMPLETE"
    try:
        staging.mkdir(mode=0o700)
        incomplete.write_text("RX06 target is incomplete and must not be activated.\n", encoding="utf-8")
        _bootstrap_target(staging)
        _materialize_objects(source, staging, manifest)
        populated = _populate_target(
            export_dir=source,
            target_root=target,
            staging=staging,
            manifest=manifest,
            table_entries=table_entries,
        )
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "COMPLETE",
            "migration_id": "rx06-" + uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target_root_name": target.name,
            "source_export_id": manifest.get("export_id"),
            "source_manifest_sha256": sha256_file(source / MANIFEST_NAME),
            "source_schema_migrations": manifest["source"]["schema_migrations"],
            "source_schema_fingerprint_sha256": manifest["source"]["schema_fingerprint_sha256"],
            "target_schema_fingerprint_sha256": _target_schema_fingerprint(staging / "app.db"),
            "selection": manifest["source"]["selection"],
            "tables": populated["target_table_digests"],
            "objects": {"count": len(manifest.get("objects", [])), "sha256_digest": value_digest(item["sha256"] for item in manifest.get("objects", []))},
            "object_index": {
                str(item["storage_key"]): {"sha256": str(item["sha256"]), "size_bytes": int(item["size_bytes"])}
                for item in manifest.get("objects", [])
            },
            "active_jobs_quarantined": populated["active_jobs_quarantined"],
            "knowledge": _knowledge_target_report(manifest),
            "activation_ready": not _activation_blockers(manifest, table_entries),
            "activation_blockers": _activation_blockers(manifest, table_entries),
            "support_limits": [
                "The target is a new local SQLite root; PostgreSQL remains unchanged and authoritative.",
                "Agent Structure and PostgreSQL source-asset GC tables are retained in the export only.",
                "Active PostgreSQL analysis jobs are marked FAILED and are never resumed.",
                "Knowledge activation requires an explicit resolved old-to-new mapping.",
            ],
        }
        receipt["verification"] = _verify_target_root(staging, receipt=receipt, manifest=manifest, stored_root=target)
        (staging / RECEIPT_DIR).mkdir(mode=0o700, exist_ok=True)
        write_json(staging / RECEIPT_DIR / RECEIPT_FILE, receipt)
        incomplete.unlink(missing_ok=True)
        rename_no_replace(staging, target)
        final_verification = _verify_target_root(target, receipt=receipt, manifest=manifest)
        return {**receipt, "verification": final_verification}
    except BaseException as exc:
        if staging.exists():
            try:
                incomplete.write_text("RX06 target is incomplete and must not be activated.\n", encoding="utf-8")
            except OSError:
                pass
            raise Rx06Error(f"RX06 import failed; incomplete staging retained at {staging}: {exc}") from exc
        if target.exists():
            try:
                (target / ".RX06-VERIFY-FAILED").write_text(
                    "RX06 target verification failed after publication; do not activate.\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        raise


def verify_target(target_root: Path) -> dict[str, Any]:
    target = require_absolute(target_root, field="target root")
    receipt_path = target / RECEIPT_DIR / RECEIPT_FILE
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise Rx06Error("Target root has no RX06 completion receipt.")
    receipt = read_json(receipt_path)
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise Rx06Error("Target receipt schema is unsupported.")
    if receipt.get("status") != "COMPLETE":
        raise Rx06Error("Target receipt is not complete.")
    verification = _verify_target_root(target, receipt=receipt)
    return {"ok": True, "receipt": receipt, "verification": verification}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RX06 explicit PostgreSQL-to-SQLite migration.")
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", help="Read-only source inventory.")
    inventory.add_argument("--database-url-env", default="REQPILOT_DATABASE_URL")
    inventory.add_argument("--project-id", action="append", required=True)
    inventory.add_argument("--object-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path)

    export = commands.add_parser("export", help="Consistent read-only export of selected projects.")
    export.add_argument("--database-url-env", default="REQPILOT_DATABASE_URL")
    export.add_argument("--project-id", action="append", required=True)
    export.add_argument("--object-root", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--knowledge-map", type=Path)

    validate = commands.add_parser("validate-export", help="Validate an export without writing a target.")
    validate.add_argument("--export", type=Path, required=True)

    importer = commands.add_parser("import", help="Create a new SQLite root from a validated export.")
    importer.add_argument("--export", type=Path, required=True)
    importer.add_argument("--target-root", type=Path, required=True)

    verify = commands.add_parser("verify-target", help="Verify a completed SQLite root and receipt.")
    verify.add_argument("--target-root", type=Path, required=True)

    bootstrap = commands.add_parser("_bootstrap-target", help=argparse.SUPPRESS)
    bootstrap.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "_bootstrap-target":
            _bootstrap_target_in_process(args.root)
            return 0
        if args.command == "validate-export":
            manifest, table_entries = validate_export(args.export)
            print(json.dumps({"ok": True, "export_id": manifest.get("export_id"), "tables": {name: entry["row_count"] for name, entry in table_entries.items()}, "objects": len(manifest.get("objects", [])), "knowledge": _knowledge_target_report(manifest), "activation_ready": not _activation_blockers(manifest, table_entries), "activation_blockers": _activation_blockers(manifest, table_entries)}, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "verify-target":
            print(json.dumps(verify_target(args.target_root), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "import":
            print(json.dumps(import_export(args.export, args.target_root), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        database_url = str(os.environ.get(args.database_url_env) or "").strip()
        if not database_url:
            raise Rx06Error(f"Set {args.database_url_env} for PostgreSQL export access.")
        source = _PsycopgSource(database_url)
        try:
            if args.command == "inventory":
                report = _inventory(source, project_ids=args.project_id, object_root=args.object_root)
                encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                if args.output:
                    write_json(require_absolute(args.output, field="inventory output"), report)
                print(encoded)
                return 0
            manifest = _export_selected(
                source,
                project_ids=args.project_id,
                object_root=args.object_root,
                output_dir=args.output,
                knowledge_map_path=args.knowledge_map,
            )
            exported_entries = {str(item["name"]): item for item in manifest["tables"]}
            blockers = _activation_blockers(manifest, exported_entries)
            print(json.dumps({"ok": True, "export_id": manifest.get("export_id"), "output": str(args.output), "tables": len(manifest["tables"]), "objects": len(manifest["objects"]), "activation_ready": not blockers, "activation_blockers": blockers}, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        finally:
            source.close()
    except (Rx06Error, OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"RX06 failed: {type(exc).__name__}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
