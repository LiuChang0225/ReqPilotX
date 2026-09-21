from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.sqlite_cutover_inventory import build_inventory

from reqpilotx_server.adapters.persistence.requirement_matrix_states import (
    REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION,
)
from reqpilotx_server.application.assessment_scope import (
    AssessmentScopeError,
    assessment_command_sha256,
    parse_assessment_scope,
)
from reqpilotx_server.application.requirement_set_impact_service import (
    REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
)
from reqpilotx_server.domain.atomization_scope import (
    AtomizationScopeAttemptV1,
    AtomizationScopeReceiptV1,
)
from reqpilotx_server.domain.extraction.requirement_enrichment import (
    RequirementAtomizationRun,
    RequirementPublicationPreview,
    canonical_model_sha256,
)
from reqpilotx_server.domain.requirement_enrichment_publication_v2 import (
    RequirementEnrichmentAtomizationStateV2,
    RequirementEnrichmentPublicationBindingPreviewV2,
    RequirementEnrichmentPublicationBindingV2,
    RequirementEnrichmentPublicationRowBindingV2,
    canonical_publication_decision_trace_v2_json,
)
from reqpilotx_server.domain.requirement_set_impact import RequirementSetImpactReceiptV1
from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.ports.object_store import ObjectStore


class CutoverRejected(RuntimeError):
    """Raised before publication when source or target invariants do not hold."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSESSMENT_COMMAND_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
)
_CARRIED_ASSESSMENT_RESULT_COLUMNS = (
    "profile_id",
    "provider",
    "retrieval_backend",
    "ai_decision",
    "compliance_status",
    "ai_reason",
    "ai_confidence",
    "assessment_mode",
    "analysis_label",
    "analysis_basis",
    "normalized_requirement",
    "risk_level",
    "clarification_question",
    "model_name",
    "prompt_version",
    "evidence_findings_json",
    "structured_information_gap_json",
    "sub_judgments_json",
)
_CARRIED_EVIDENCE_COLUMNS = (
    "source_doc_id",
    "source_doc_name",
    "source_doc_version",
    "retrieval_evidence_id",
    "content_hash",
    "evidence_contract_version",
    "content_kind",
    "evidence_metadata_json",
    "source_location",
    "excerpt",
    "relevance",
)
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ENTRY_CLASSIFICATION_ENVELOPE_VERSION = (
    "entry-classification-persistence-envelope/v1"
)
_ENTRY_CLASSIFICATION_ENVELOPE_FIELDS = (
    "id",
    "project_id",
    "sor_document_id",
    "source_sha256",
    "working_tree_id",
    "working_tree_sha256",
    "effective_structure_id",
    "semantic_snapshot_id",
    "policy_version",
    "revision",
    "status",
    "supersedes_id",
    "command_id",
    "command_sha256",
    "payload_sha256",
    "created_at",
    "accepted_at",
    "accepted_by",
)
_ENTRY_CLASSIFICATION_RECEIPT_ENVELOPE_VERSION = (
    "entry-classification-command-receipt-envelope/v1"
)
_ENTRY_CLASSIFICATION_RECEIPT_ENVELOPE_FIELDS = (
    "id",
    "project_id",
    "sor_document_id",
    "source_sha256",
    "working_tree_id",
    "working_tree_sha256",
    "effective_structure_id",
    "semantic_snapshot_id",
    "policy_version",
    "command_id",
    "command_sha256",
    "result_snapshot_id",
    "result_snapshot_sha256",
    "created_at",
)

_TABLE_ORDER = (
    "projects",
    "sor_documents",
    "requirement_sets",
    "requirement_set_impact_receipts",
    "knowledge_documents",
    "knowledge_document_syncs",
    "requirements",
    "requirement_matrix_node_states",
    "assessments",
    "evidences",
    "assessment_runs",
    "ai_analysis_jobs",
    "ai_analysis_job_targets",
    "ai_analysis_node_attempts",
    "ai_analysis_events",
    "review_logs",
    "export_jobs",
    "batch_decision_operations",
    "parsing_risk_acknowledgements",
    "entry_classification_snapshots",
    "entry_classification_command_receipts",
    "document_table_assets",
    "requirement_set_table_assets",
    "source_ir_snapshots",
    "semantic_provider_runs",
    "llm_requirement_review_sessions",
    "llm_requirement_review_events",
    "llm_requirement_review_confirmation_events",
    "requirement_atomization_checkpoints",
    "requirement_atomization_runs",
    "requirement_atomization_scope_attempts",
    "requirement_atomization_scope_receipts",
    "requirement_refinement_suggestions",
    "requirement_refinement_events",
    "requirement_enrichment_publication_previews",
    "requirement_enrichment_publications",
    "requirement_enrichment_publication_binding_previews",
    "requirement_enrichment_publication_bindings",
    "requirement_enrichment_publication_row_bindings",
)

_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "projects": ("id",),
    "sor_documents": ("id",),
    "requirement_sets": ("id",),
    "requirement_set_impact_receipts": ("receipt_id",),
    "knowledge_documents": ("id",),
    "knowledge_document_syncs": ("id",),
    "requirements": ("id",),
    "requirement_matrix_node_states": ("id",),
    "assessments": ("id",),
    "evidences": ("id",),
    "assessment_runs": ("id",),
    "ai_analysis_jobs": ("id",),
    "ai_analysis_job_targets": ("id",),
    "ai_analysis_node_attempts": ("id",),
    "ai_analysis_events": ("id",),
    "review_logs": ("id",),
    "export_jobs": ("id",),
    "batch_decision_operations": ("id",),
    "parsing_risk_acknowledgements": ("id",),
    "entry_classification_snapshots": ("id",),
    "entry_classification_command_receipts": ("id",),
    "document_table_assets": ("id",),
    "requirement_set_table_assets": ("id",),
    "source_ir_snapshots": ("source_ir_id",),
    "semantic_provider_runs": ("provider_run_id",),
    "llm_requirement_review_sessions": ("session_id",),
    "llm_requirement_review_events": ("session_id", "revision"),
    "llm_requirement_review_confirmation_events": ("session_id", "revision"),
    "requirement_atomization_checkpoints": ("lineage_sha256", "review_node_id"),
    "requirement_atomization_runs": ("atomization_run_id",),
    "requirement_atomization_scope_attempts": ("attempt_id",),
    "requirement_atomization_scope_receipts": ("receipt_id",),
    "requirement_refinement_suggestions": ("suggestion_id",),
    "requirement_refinement_events": ("suggestion_id", "revision"),
    "requirement_enrichment_publication_previews": ("preview_id",),
    "requirement_enrichment_publications": ("publication_key",),
    "requirement_enrichment_publication_binding_previews": (
        "binding_preview_id",
    ),
    "requirement_enrichment_publication_bindings": ("binding_id",),
    "requirement_enrichment_publication_row_bindings": (
        "binding_id",
        "publication_order",
    ),
}

_FILE_COLUMNS: dict[str, tuple[str, str]] = {
    "sor_documents": ("file_path", "storage_key"),
    "knowledge_documents": ("file_path", "storage_key"),
    "export_jobs": ("file_path", "storage_key"),
    "knowledge_document_syncs": ("payload_path", "payload_storage_key"),
}

_MUTABLE_JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "requirements": (
        "table_refs",
        "content_blocks",
        "decision_reference_json",
    ),
    "document_table_assets": ("asset_json",),
    "requirement_set_table_assets": ("asset_json",),
    "export_jobs": ("snapshot_json",),
}

_HASHED_JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "source_ir_snapshots": ("payload_json",),
    "semantic_provider_runs": ("payload_json",),
    "llm_requirement_review_sessions": (
        "model_prediction_json",
        "reviewed_result_json",
        "delta_json",
    ),
    "llm_requirement_review_events": ("reviewed_result_json", "delta_json"),
    "llm_requirement_review_confirmation_events": (),
    "requirement_atomization_checkpoints": ("payload_json",),
    "requirement_atomization_runs": ("payload_json",),
    "requirement_atomization_scope_attempts": ("payload_json",),
    "requirement_atomization_scope_receipts": ("payload_json",),
    "requirement_refinement_suggestions": ("payload_json",),
    "requirement_refinement_events": ("payload_json",),
    "requirement_enrichment_publication_previews": ("payload_json",),
    "requirement_enrichment_publications": ("result_json",),
    "requirement_enrichment_publication_binding_previews": ("payload_json",),
    "requirement_enrichment_publication_bindings": ("payload_json",),
    "requirement_enrichment_publication_row_bindings": ("payload_json",),
    "entry_classification_snapshots": ("payload_json",),
    "requirement_set_impact_receipts": ("payload_json",),
    "ai_analysis_node_attempts": ("result_payload_json",),
}


@dataclass(frozen=True, slots=True)
class ObjectCandidate:
    source_path: Path
    storage_key: str
    sha256: str
    size: int
    media_type: str
    object_kind: str


@dataclass(frozen=True, slots=True)
class CutoverPlan:
    source_database: Path
    source_database_sha256: str
    inventory_sha256: str
    table_counts: dict[str, int]
    primary_key_digests: dict[str, str]
    row_digests: dict[str, str]
    object_candidates: tuple[ObjectCandidate, ...]
    rewritten_rows: dict[str, tuple[dict[str, Any], ...]]

    def public_summary(self) -> dict[str, Any]:
        object_bytes = sum(item.size for item in self.object_candidates)
        return {
            "source_database_sha256": self.source_database_sha256,
            "inventory_sha256": self.inventory_sha256,
            "table_count": len(self.table_counts),
            "row_count": sum(self.table_counts.values()),
            "object_count": len(self.object_candidates),
            "object_bytes": object_bytes,
            "table_counts": dict(self.table_counts),
            "primary_key_digests": dict(self.primary_key_digests),
            "row_digests": dict(self.row_digests),
        }


def build_cutover_plan(source_database: Path) -> CutoverPlan:
    source = Path(source_database).expanduser().resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise CutoverRejected("SQLite source must be a regular non-symlink file.")
    inventory = build_inventory(source)
    database_sha256 = _hash_file(source)
    object_by_source_path: dict[str, ObjectCandidate] = {}
    objects_by_key: dict[str, ObjectCandidate] = {}
    rewritten_rows: dict[str, tuple[dict[str, Any], ...]] = {}
    table_counts: dict[str, int] = {}
    primary_key_digests: dict[str, str] = {}
    row_digests: dict[str, str] = {}

    with _source_connection(source) as connection:
        available_tables = {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }
        for table in _TABLE_ORDER:
            if table not in available_tables:
                continue
            rows = tuple(
                dict(row)
                for row in connection.execute(
                    f'SELECT * FROM "{table}"'  # table comes from the closed constant
                ).fetchall()
            )
            rewritten: list[dict[str, Any]] = []
            for row in rows:
                rewritten.append(
                    _rewrite_row(
                        table=table,
                        row=row,
                        source_database=source,
                        object_by_source_path=object_by_source_path,
                        objects_by_key=objects_by_key,
                    )
                )
            rewritten_rows[table] = tuple(rewritten)
            table_counts[table] = len(rewritten)
            primary_key_digests[table] = _primary_key_digest(
                rewritten,
                _PRIMARY_KEYS[table],
            )
            row_digests[table] = _row_digest(rewritten)

    _validate_atomization_artifact_relationships(rewritten_rows)
    _validate_requirement_enrichment_publication_relationships(rewritten_rows)
    _validate_requirement_set_impact_relationships(rewritten_rows)
    _validate_ai_analysis_node_execution_relationships(rewritten_rows)

    return CutoverPlan(
        source_database=source,
        source_database_sha256=database_sha256,
        inventory_sha256=str(inventory["report_sha256"]),
        table_counts=table_counts,
        primary_key_digests=primary_key_digests,
        row_digests=row_digests,
        object_candidates=tuple(
            objects_by_key[key] for key in sorted(objects_by_key)
        ),
        rewritten_rows=rewritten_rows,
    )


def execute_cutover(
    *,
    plan: CutoverPlan,
    database: PostgresDatabase,
    object_store: ObjectStore,
    target_environment: str,
) -> dict[str, Any]:
    environment = str(target_environment or "").strip().lower()
    if environment not in {"local", "cloud"}:
        raise ValueError("target_environment must be local or cloud.")
    _require_quiescent_source(plan.source_database)

    for candidate in plan.object_candidates:
        existing = object_store.stat(candidate.storage_key)
        if existing is None:
            with candidate.source_path.open("rb") as source:
                existing = object_store.put(
                    key=candidate.storage_key,
                    source=source,
                    expected_sha256=candidate.sha256,
                    media_type=candidate.media_type,
                )
        if (
            existing.sha256 != candidate.sha256
            or int(existing.size) != candidate.size
        ):
            raise CutoverRejected(
                "Existing object does not match the deterministic cutover identity."
            )

    run_id = f"cutover-{uuid.uuid4().hex}"
    started_at = datetime.now(timezone.utc)
    with database.connection() as connection:
        existing_run = connection.execute(
            """
            SELECT id, status
            FROM cutover_migration_runs
            WHERE source_database_sha256 = %s AND target_environment = %s
            """,
            (plan.source_database_sha256, environment),
        ).fetchone()
        if existing_run is not None:
            if str(existing_run["status"]) == "VERIFIED":
                return {
                    "ok": True,
                    "reused": True,
                    "run_id": str(existing_run["id"]),
                    **plan.public_summary(),
                }
            raise CutoverRejected(
                "A non-verified cutover run already exists for this source and target."
            )

        _require_empty_target(connection, plan.table_counts)
        connection.execute(
            """
            INSERT INTO cutover_migration_runs (
                id, source_database_sha256, source_inventory_sha256,
                target_environment, status, table_counts, object_counts, started_at
            )
            VALUES (%s, %s, %s, %s, 'RUNNING', %s::jsonb, %s::jsonb, %s)
            """,
            (
                run_id,
                plan.source_database_sha256,
                plan.inventory_sha256,
                environment,
                _canonical_json(plan.table_counts),
                _canonical_json(
                    {
                        "count": len(plan.object_candidates),
                        "bytes": sum(item.size for item in plan.object_candidates),
                    }
                ),
                started_at,
            ),
        )

        for candidate in plan.object_candidates:
            connection.execute(
                """
                INSERT INTO stored_objects (
                    storage_key, sha256, size_bytes, media_type, object_kind
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (storage_key) DO NOTHING
                """,
                (
                    candidate.storage_key,
                    candidate.sha256,
                    candidate.size,
                    candidate.media_type,
                    candidate.object_kind,
                ),
            )

        target_digests: dict[str, str] = {}
        target_row_digests: dict[str, str] = {}
        for table in _TABLE_ORDER:
            rows = plan.rewritten_rows.get(table)
            if rows is None:
                continue
            target_columns = _target_columns(connection, table)
            migrated_columns: tuple[str, ...] = ()
            for row in rows:
                unknown_columns = sorted(
                    set(row) - target_columns - {"_row_order"}
                )
                if unknown_columns:
                    raise CutoverRejected(
                        f"Target schema would drop columns from {table}: "
                        + ", ".join(unknown_columns)
                    )
                payload = {
                    key: value
                    for key, value in row.items()
                    if key in target_columns and key != "_row_order"
                }
                if not payload:
                    raise CutoverRejected(f"No compatible target columns for {table}.")
                columns = tuple(payload)
                if migrated_columns and columns != migrated_columns:
                    raise CutoverRejected(
                        f"Rows in {table} do not share one migration schema."
                    )
                migrated_columns = columns
                placeholders = ", ".join("%s" for _ in columns)
                column_sql = ", ".join(f'"{column}"' for column in columns)
                connection.execute(
                    f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
                    tuple(payload[column] for column in columns),
                )
            target_rows = connection.execute(
                f'SELECT {", ".join(_PRIMARY_KEYS[table])} FROM "{table}"'
            ).fetchall()
            target_digests[table] = _primary_key_digest(
                (dict(row) for row in target_rows),
                _PRIMARY_KEYS[table],
            )
            if migrated_columns:
                selected_column_sql = ", ".join(
                    f'"{column}"' for column in migrated_columns
                )
                selected_rows = connection.execute(
                    f'SELECT {selected_column_sql} FROM "{table}"'
                ).fetchall()
                target_row_digests[table] = _row_digest(
                    dict(row) for row in selected_rows
                )
            else:
                target_row_digests[table] = _row_digest(())
            if (
                len(target_rows) != plan.table_counts[table]
                or target_digests[table] != plan.primary_key_digests[table]
                or target_row_digests[table] != plan.row_digests[table]
            ):
                raise CutoverRejected(
                    f"Target verification failed for table {table}."
                )

        invariant_counts = _validate_target_invariants(connection)
        verification = {
            "table_counts": plan.table_counts,
            "primary_key_digests": target_digests,
            "row_digests": target_row_digests,
            "invariant_violations": invariant_counts,
            "object_count": len(plan.object_candidates),
            "object_sha256_digest": _value_digest(
                item.sha256 for item in plan.object_candidates
            ),
        }
        verification_sha256 = hashlib.sha256(
            _canonical_json(verification).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            UPDATE cutover_migration_runs
            SET status = 'VERIFIED',
                verification_sha256 = %s,
                finished_at = %s
            WHERE id = %s
            """,
            (verification_sha256, datetime.now(timezone.utc), run_id),
        )

    return {
        "ok": True,
        "reused": False,
        "run_id": run_id,
        "verification_sha256": verification_sha256,
        **plan.public_summary(),
    }


def _rewrite_row(
    *,
    table: str,
    row: dict[str, Any],
    source_database: Path,
    object_by_source_path: dict[str, ObjectCandidate],
    objects_by_key: dict[str, ObjectCandidate],
) -> dict[str, Any]:
    rewritten = dict(row)
    if table == "entry_classification_snapshots":
        _validate_entry_classification_snapshot_integrity(rewritten)
    elif table == "entry_classification_command_receipts":
        _validate_entry_classification_command_receipt_integrity(rewritten)
    elif table == "requirement_set_impact_receipts":
        _validate_requirement_set_impact_receipt_integrity(rewritten)
    elif table == "ai_analysis_jobs":
        _validate_ai_analysis_job_scope_integrity(rewritten)
    elif table == "ai_analysis_job_targets":
        _validate_ai_analysis_job_target_integrity(rewritten)
    elif table == "ai_analysis_node_attempts":
        _validate_ai_analysis_node_attempt_integrity(rewritten)
    elif table in {
        "requirement_atomization_runs",
        "requirement_atomization_scope_receipts",
        "requirement_atomization_scope_attempts",
    }:
        _validate_atomization_artifact_integrity(table=table, row=rewritten)
    elif table in {
        "requirement_enrichment_publication_binding_previews",
        "requirement_enrichment_publication_bindings",
        "requirement_enrichment_publication_row_bindings",
    }:
        _validate_requirement_enrichment_publication_artifact_integrity(
            table=table,
            row=rewritten,
        )
    source_column, target_column = _FILE_COLUMNS.get(table, ("", ""))
    if source_column:
        raw_path = str(rewritten.pop(source_column, "") or "").strip()
        if raw_path:
            candidate = _object_candidate(
                raw_path=raw_path,
                table=table,
                row=rewritten,
                source_database=source_database,
            )
            _register_candidate(
                candidate,
                object_by_source_path=object_by_source_path,
                objects_by_key=objects_by_key,
            )
            rewritten[target_column] = candidate.storage_key
            if table in {"sor_documents", "knowledge_documents"}:
                stored_hash = str(rewritten.get("source_sha256") or "").strip().lower()
                if stored_hash and stored_hash != candidate.sha256:
                    raise CutoverRejected(
                        f"Stored source hash does not match file bytes for {table}."
                    )
                rewritten["source_sha256"] = candidate.sha256
                rewritten["source_size"] = candidate.size
        elif table != "knowledge_document_syncs":
            raise CutoverRejected(f"{table}.{source_column} must not be blank.")
        else:
            rewritten[target_column] = None

    for column in _MUTABLE_JSON_COLUMNS.get(table, ()):
        raw = rewritten.get(column)
        if raw in {None, ""}:
            continue
        payload = _parse_json(raw, table=table, column=column)
        rewritten_payload = _rewrite_json_paths(
            payload,
            table=table,
            row=rewritten,
            source_database=source_database,
            object_by_source_path=object_by_source_path,
            objects_by_key=objects_by_key,
        )
        rewritten[column] = _canonical_json(rewritten_payload)

    for column in _HASHED_JSON_COLUMNS.get(table, ()):
        raw = rewritten.get(column)
        if raw in {None, ""}:
            continue
        payload = _parse_json(raw, table=table, column=column)
        if _contains_existing_local_path(payload, source_database=source_database):
            raise CutoverRejected(
                f"Immutable payload {table}.{column} contains a local file path."
            )

    if table == "ai_analysis_jobs" and str(rewritten.get("status") or "").upper() in {
        "PENDING",
        "RUNNING",
        "CANCEL_REQUESTED",
    }:
        rewritten["status"] = "FAILED"
        rewritten["error_message"] = (
            "Interrupted by the PostgreSQL hard cutover; explicitly rerun this job."
        )
        finished_at = str(rewritten.get("finished_at") or "").strip()
        rewritten["finished_at"] = finished_at or datetime.now(timezone.utc).isoformat()
        rewritten["updated_at"] = rewritten["finished_at"]
    elif table == "ai_analysis_job_targets" and str(
        rewritten.get("status") or ""
    ).upper() in {"WAITING", "RUNNING", "PUBLISHING"}:
        rewritten["status"] = "FAILED"
        rewritten["retryable"] = 1
        rewritten["error_code"] = "hard_cutover_interrupted"
        rewritten["error_message"] = (
            "Interrupted by the PostgreSQL hard cutover; retry this node explicitly."
        )
        rewritten["updated_at"] = datetime.now(timezone.utc).isoformat()
    elif table == "ai_analysis_node_attempts" and str(
        rewritten.get("status") or ""
    ).upper() == "RUNNING":
        rewritten["status"] = "FAILED"
        rewritten["error_code"] = "hard_cutover_interrupted"
        rewritten["error_message"] = (
            "Interrupted by the PostgreSQL hard cutover before publication."
        )
        finished_at = str(rewritten.get("finished_at") or "").strip()
        rewritten["finished_at"] = (
            finished_at or datetime.now(timezone.utc).isoformat()
        )
        rewritten["updated_at"] = rewritten["finished_at"]
    return rewritten


def _validate_ai_analysis_job_scope_integrity(row: Mapping[str, Any]) -> None:
    """Reject scoped jobs whose immutable envelope cannot survive cutover.

    Legacy whole-set jobs intentionally have blank scope identity and a ``{}``
    placeholder.  Once any scope identity is present, canonical bytes and their
    digest are part of the worker/publication fencing contract and must be
    verified before copying the row.
    """

    scope_sha256 = str(row.get("scope_sha256") or "")
    scope_schema_version = str(row.get("scope_schema_version") or "")
    scope_kind = str(row.get("scope_kind") or "")
    command_id = str(row.get("command_id") or "")
    command_sha256 = str(row.get("command_sha256") or "")
    requirement_tree_sha256 = str(
        row.get("requirement_tree_sha256") or ""
    )
    publication_mode = str(row.get("publication_mode") or "")
    raw_payload = str(row.get("scope_payload_json") or "")
    try:
        requirement_set_version = int(row.get("requirement_set_version") or 0)
        published_requirements = int(row.get("published_requirements") or 0)
        knowledge_base_version = int(row.get("knowledge_base_version") or 0)
    except (TypeError, ValueError) as exc:
        raise CutoverRejected(
            "AI analysis scope numeric identity is invalid."
        ) from exc
    if requirement_set_version < 0:
        raise CutoverRejected(
            "AI analysis requirement set version must be nonnegative."
        )
    if published_requirements < 0:
        raise CutoverRejected(
            "AI analysis published requirement count must be nonnegative."
        )
    if (
        requirement_tree_sha256
        and _SHA256_RE.fullmatch(requirement_tree_sha256) is None
    ):
        raise CutoverRejected("AI analysis requirement tree hash is invalid.")
    if publication_mode not in {"ATOMIC_BATCH", "PER_NODE_RECOVERABLE"}:
        raise CutoverRejected("AI analysis publication mode is invalid.")
    identity_values = (
        scope_sha256,
        scope_schema_version,
        scope_kind,
        command_id,
        command_sha256,
    )
    if not any(identity_values):
        if raw_payload != "{}":
            raise CutoverRejected(
                "AI analysis legacy scope payload has no scope identity."
            )
        if publication_mode != "ATOMIC_BATCH":
            raise CutoverRejected(
                "AI analysis legacy scope must use atomic publication."
            )
        return
    if not all(identity_values):
        raise CutoverRejected("AI analysis scope identity is incomplete.")
    supported_scope_modes = {
        "requirement-analysis-scope/v1": (
            {"CURRENT", "SELECTED", "ALL"},
            "ATOMIC_BATCH",
        ),
        "requirement-analysis-scope/v2": (
            {"CURRENT", "SELECTED", "ALL", "FAILED_ONLY"},
            "PER_NODE_RECOVERABLE",
        ),
    }
    if scope_schema_version not in supported_scope_modes:
        raise CutoverRejected("AI analysis scope schema is unsupported.")
    supported_kinds, expected_publication_mode = supported_scope_modes[
        scope_schema_version
    ]
    if scope_kind not in supported_kinds:
        raise CutoverRejected("AI analysis scope kind is invalid.")
    if publication_mode != expected_publication_mode:
        raise CutoverRejected(
            "AI analysis scope schema and publication mode do not match."
        )
    if _ASSESSMENT_COMMAND_ID_RE.fullmatch(command_id) is None:
        raise CutoverRejected("AI analysis command id is invalid.")
    if _SHA256_RE.fullmatch(scope_sha256) is None or _SHA256_RE.fullmatch(
        command_sha256
    ) is None:
        raise CutoverRejected("AI analysis scope identity hash is invalid.")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise CutoverRejected("AI analysis scope payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise CutoverRejected("AI analysis scope payload must be a JSON object.")
    canonical = _canonical_json(payload)
    if canonical != raw_payload:
        raise CutoverRejected("AI analysis scope payload is not canonical JSON.")
    if hashlib.sha256(raw_payload.encode("utf-8")).hexdigest() != scope_sha256:
        raise CutoverRejected("AI analysis scope payload hash does not match.")
    try:
        parse_assessment_scope(
            raw_payload,
            expected_scope_sha256=scope_sha256,
        )
    except AssessmentScopeError as exc:
        raise CutoverRejected(
            f"AI analysis scope payload is structurally invalid: {exc.code}."
        ) from exc
    if (
        str(payload.get("schema_version") or "") != scope_schema_version
        or str(payload.get("scope") or "") != scope_kind
        or str(payload.get("requirement_set_id") or "")
        != str(row.get("requirement_set_id") or "")
        or int(payload.get("requirement_set_version") or 0)
        != requirement_set_version
        or str(payload.get("tree_sha256") or "")
        != requirement_tree_sha256
        or str(payload.get("publication_mode") or "") != publication_mode
    ):
        raise CutoverRejected(
            "AI analysis scope column binding does not match its payload."
        )
    expected_command_sha256 = assessment_command_sha256(
        command_id=command_id,
        scope_sha256=scope_sha256,
        requirement_set_id=str(row.get("requirement_set_id") or ""),
        requirement_set_version=requirement_set_version,
        requirement_tree_sha256=requirement_tree_sha256,
        knowledge_base_version=knowledge_base_version,
        profile_id=str(row.get("profile_id") or ""),
        provider=str(row.get("provider") or ""),
        assessment_mode=str(row.get("assessment_mode") or ""),
        retrieval_backend=str(row.get("retrieval_backend") or ""),
        chat_model=str(row.get("chat_model") or ""),
        embedding_model=str(row.get("embedding_model") or ""),
    )
    if command_sha256 != expected_command_sha256:
        raise CutoverRejected(
            "AI analysis command hash does not match its execution identity."
        )


def _assessment_scope_pairs(
    payload: Mapping[str, Any],
    *,
    field: str,
    allow_empty: bool,
) -> tuple[tuple[str, str], ...]:
    raw_pairs = payload.get(field)
    if not isinstance(raw_pairs, list):
        raise CutoverRejected(
            f"AI analysis scope {field} must be a JSON array."
        )
    normalized: list[tuple[str, str]] = []
    for item in raw_pairs:
        if not isinstance(item, dict):
            raise CutoverRejected(
                f"AI analysis scope {field} contains an invalid binding."
            )
        node_id = str(item.get("node_id") or "").strip()
        requirement_id = str(item.get("requirement_id") or "").strip()
        if not node_id or not requirement_id:
            raise CutoverRejected(
                f"AI analysis scope {field} contains an incomplete binding."
            )
        normalized.append((node_id, requirement_id))
    if not allow_empty and not normalized:
        raise CutoverRejected(f"AI analysis scope {field} must not be empty.")
    if (
        len(normalized) != len(set(normalized))
        or len({item[0] for item in normalized}) != len(normalized)
        or len({item[1] for item in normalized}) != len(normalized)
    ):
        raise CutoverRejected(
            f"AI analysis scope {field} bindings must be one-to-one."
        )
    return tuple(normalized)


def _validate_ai_analysis_job_target_integrity(
    row: Mapping[str, Any],
) -> None:
    identifiers = {
        "id": row.get("id"),
        "job_id": row.get("job_id"),
        "sor_document_id": row.get("sor_document_id"),
        "node_id": row.get("node_id"),
        "requirement_id": row.get("requirement_id"),
        # This is the frozen user-facing code shown while the job ran.  It is
        # intentionally not required to equal requirements.req_code because
        # the Matrix may provide a stable display code for the selected view.
        "requirement_code": row.get("requirement_code"),
    }
    if any(not str(value or "").strip() for value in identifiers.values()):
        raise CutoverRejected("AI analysis job target identity is incomplete.")
    try:
        source_order = int(row.get("source_order"))
        latest_attempt_no = int(row.get("latest_attempt_no") or 0)
        retryable = int(row.get("retryable") or 0)
    except (TypeError, ValueError) as exc:
        raise CutoverRejected(
            "AI analysis job target numeric state is invalid."
        ) from exc
    if source_order < 0 or latest_attempt_no < 0 or retryable not in {0, 1}:
        raise CutoverRejected("AI analysis job target numeric state is invalid.")
    status = str(row.get("status") or "")
    if status not in {
        "WAITING",
        "RUNNING",
        "PUBLISHING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    }:
        raise CutoverRejected("AI analysis job target status is invalid.")
    latest_attempt_id = str(row.get("latest_attempt_id") or "").strip()
    published_attempt_id = str(row.get("published_attempt_id") or "").strip()
    if latest_attempt_id and latest_attempt_no < 1:
        raise CutoverRejected(
            "AI analysis job target latest-attempt pointer is inconsistent."
        )
    if status in {"RUNNING", "PUBLISHING", "SUCCEEDED"} and not latest_attempt_id:
        raise CutoverRejected(
            "Active AI analysis job target has no fenced attempt."
        )
    if status == "SUCCEEDED" and not published_attempt_id:
        raise CutoverRejected(
            "Successful AI analysis target has no published attempt."
        )
    if status != "SUCCEEDED" and published_attempt_id:
        raise CutoverRejected(
            "Non-successful AI analysis target claims a published attempt."
        )
    if not str(row.get("created_at") or "").strip() or not str(
        row.get("updated_at") or ""
    ).strip():
        raise CutoverRejected("AI analysis job target timestamps are incomplete.")


def _validate_ai_analysis_node_attempt_integrity(
    row: Mapping[str, Any],
) -> None:
    identifiers = {
        "id": row.get("id"),
        "target_id": row.get("target_id"),
        "job_id": row.get("job_id"),
        "node_id": row.get("node_id"),
        "requirement_id": row.get("requirement_id"),
        "fence_token": row.get("fence_token"),
    }
    if any(not str(value or "").strip() for value in identifiers.values()):
        raise CutoverRejected("AI analysis node attempt identity is incomplete.")
    try:
        attempt_no = int(row.get("attempt_no"))
    except (TypeError, ValueError) as exc:
        raise CutoverRejected(
            "AI analysis node attempt number is invalid."
        ) from exc
    if attempt_no < 1:
        raise CutoverRejected("AI analysis node attempt number is invalid.")
    status = str(row.get("status") or "")
    if status not in {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}:
        raise CutoverRejected("AI analysis node attempt status is invalid.")

    result_schema_version = str(row.get("result_schema_version") or "")
    result_payload_json = str(row.get("result_payload_json") or "")
    result_sha256 = str(row.get("result_sha256") or "")
    result_identity = (result_schema_version, result_sha256)
    if any(result_identity) and (
        not all(result_identity) or not result_payload_json
    ):
        raise CutoverRejected("AI analysis node attempt result is incomplete.")
    if status == "SUCCEEDED" and not all(
        (result_schema_version, result_payload_json, result_sha256)
    ):
        raise CutoverRejected(
            "Successful AI analysis node attempt has no durable result."
        )
    if result_payload_json:
        try:
            result_payload = json.loads(result_payload_json)
        except json.JSONDecodeError as exc:
            raise CutoverRejected(
                "AI analysis node attempt result is not valid JSON."
            ) from exc
        if (
            not isinstance(result_payload, dict)
            or _canonical_json(result_payload) != result_payload_json
        ):
            raise CutoverRejected(
                "AI analysis node attempt result is not canonical JSON."
            )
        if result_sha256 and (
            _SHA256_RE.fullmatch(result_sha256) is None
            or hashlib.sha256(result_payload_json.encode("utf-8")).hexdigest()
            != result_sha256
        ):
            raise CutoverRejected(
                "AI analysis node attempt result hash does not match."
            )

    input_schema_version = str(
        row.get("analysis_input_schema_version") or ""
    )
    input_sha256 = str(row.get("analysis_input_sha256") or "")
    if bool(input_schema_version) != bool(input_sha256) or (
        input_sha256 and _SHA256_RE.fullmatch(input_sha256) is None
    ):
        raise CutoverRejected(
            "AI analysis node attempt input identity is invalid."
        )
    if not str(row.get("started_at") or "").strip() or not str(
        row.get("updated_at") or ""
    ).strip():
        raise CutoverRejected("AI analysis node attempt timestamps are incomplete.")
    if status != "RUNNING" and not str(row.get("finished_at") or "").strip():
        raise CutoverRejected(
            "Terminal AI analysis node attempt has no finished timestamp."
        )


def _validate_ai_analysis_node_execution_relationships(
    rewritten_rows: Mapping[str, tuple[dict[str, Any], ...]],
) -> None:
    target_rows = rewritten_rows.get("ai_analysis_job_targets", ())
    attempt_rows = rewritten_rows.get("ai_analysis_node_attempts", ())
    assessment_run_rows = rewritten_rows.get("assessment_runs", ())
    recoverable_jobs = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("ai_analysis_jobs", ())
        if str(row.get("scope_schema_version") or "")
        == "requirement-analysis-scope/v2"
    }
    if not target_rows and not attempt_rows and not recoverable_jobs:
        if any(
            str(row.get("analysis_node_attempt_id") or "").strip()
            for row in assessment_run_rows
        ):
            raise CutoverRejected(
                "Assessment run points to a missing AI analysis node attempt."
            )
        return

    jobs = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("ai_analysis_jobs", ())
    }
    documents = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("sor_documents", ())
    }
    requirements = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("requirements", ())
    }
    targets = {str(row.get("id") or ""): row for row in target_rows}
    attempts = {str(row.get("id") or ""): row for row in attempt_rows}
    if len(targets) != len(target_rows) or len(attempts) != len(attempt_rows):
        raise CutoverRejected("AI analysis node execution identities are duplicated.")
    if (
        len(
            {
                (str(row.get("job_id") or ""), str(row.get("node_id") or ""))
                for row in target_rows
            }
        )
        != len(target_rows)
        or len(
            {
                (
                    str(row.get("job_id") or ""),
                    str(row.get("requirement_id") or ""),
                )
                for row in target_rows
            }
        )
        != len(target_rows)
        or len(
            {
                (str(row.get("job_id") or ""), int(row.get("source_order") or 0))
                for row in target_rows
            }
        )
        != len(target_rows)
    ):
        raise CutoverRejected("AI analysis job target bindings are duplicated.")
    if (
        len({str(row.get("fence_token") or "") for row in attempt_rows})
        != len(attempt_rows)
        or len(
            {
                (
                    str(row.get("target_id") or ""),
                    int(row.get("attempt_no") or 0),
                )
                for row in attempt_rows
            }
        )
        != len(attempt_rows)
    ):
        raise CutoverRejected("AI analysis node attempt bindings are duplicated.")

    targets_by_job: dict[str, list[dict[str, Any]]] = {}
    for target in target_rows:
        target_id = str(target.get("id") or "")
        job_id = str(target.get("job_id") or "")
        job = jobs.get(job_id)
        if job is None:
            raise CutoverRejected(
                "AI analysis job target points to a missing job."
            )
        if job_id not in recoverable_jobs or str(
            job.get("publication_mode") or ""
        ) != "PER_NODE_RECOVERABLE":
            raise CutoverRejected(
                "AI analysis target cannot be attached to an atomic legacy job."
            )
        document_id = str(target.get("sor_document_id") or "")
        requirement_id = str(target.get("requirement_id") or "")
        requirement = requirements.get(requirement_id)
        if (
            document_id not in documents
            or document_id != str(job.get("sor_document_id") or "")
            or requirement is None
            or str(requirement.get("sor_document_id") or "") != document_id
            or str(requirement.get("requirement_set_id") or "")
            != str(job.get("requirement_set_id") or "")
        ):
            raise CutoverRejected(
                f"AI analysis job target {target_id} has invalid document or requirement binding."
            )
        targets_by_job.setdefault(job_id, []).append(target)

    for job_id, job in recoverable_jobs.items():
        try:
            payload = json.loads(str(job.get("scope_payload_json") or ""))
        except json.JSONDecodeError as exc:
            raise CutoverRejected(
                "AI analysis recoverable job scope is not valid JSON."
            ) from exc
        expected_pairs = _assessment_scope_pairs(
            payload,
            field="target_pairs",
            allow_empty=True,
        )
        ordered_targets = sorted(
            targets_by_job.get(job_id, ()),
            key=lambda item: (
                int(item.get("source_order") or 0),
                str(item.get("id") or ""),
            ),
        )
        actual_pairs = tuple(
            (
                str(target.get("node_id") or ""),
                str(target.get("requirement_id") or ""),
            )
            for target in ordered_targets
        )
        if actual_pairs != expected_pairs:
            raise CutoverRejected(
                "AI analysis recoverable job targets do not match its frozen scope."
            )

    attempts_by_target: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempt_rows:
        target_id = str(attempt.get("target_id") or "")
        target = targets.get(target_id)
        if target is None:
            raise CutoverRejected(
                "AI analysis node attempt points to a missing target."
            )
        if (
            str(attempt.get("job_id") or "")
            != str(target.get("job_id") or "")
            or str(attempt.get("node_id") or "")
            != str(target.get("node_id") or "")
            or str(attempt.get("requirement_id") or "")
            != str(target.get("requirement_id") or "")
        ):
            raise CutoverRejected(
                "AI analysis node attempt disagrees with its target binding."
            )
        attempts_by_target.setdefault(target_id, []).append(attempt)

    for target_id, target in targets.items():
        target_attempts = attempts_by_target.get(target_id, ())
        latest_attempt_no = int(target.get("latest_attempt_no") or 0)
        latest_attempt_id = str(target.get("latest_attempt_id") or "")
        if target_attempts:
            latest_attempt = max(
                target_attempts,
                key=lambda item: int(item.get("attempt_no") or 0),
            )
            if (
                latest_attempt_no != int(latest_attempt.get("attempt_no") or 0)
                or (
                    latest_attempt_id
                    and latest_attempt_id != str(latest_attempt.get("id") or "")
                )
            ):
                raise CutoverRejected(
                    "AI analysis target latest-attempt pointer is stale."
                )
        elif latest_attempt_no != 0 or latest_attempt_id:
            raise CutoverRejected(
                "AI analysis target points to a missing latest attempt."
            )
        published_attempt_id = str(target.get("published_attempt_id") or "")
        if published_attempt_id:
            published_attempt = attempts.get(published_attempt_id)
            if (
                published_attempt is None
                or str(published_attempt.get("target_id") or "") != target_id
                or str(published_attempt.get("status") or "") != "SUCCEEDED"
            ):
                raise CutoverRejected(
                    "AI analysis target published-attempt pointer is invalid."
                )

    for run in assessment_run_rows:
        attempt_id = str(run.get("analysis_node_attempt_id") or "").strip()
        try:
            run_index = int(run.get("run_index", -1))
        except (TypeError, ValueError) as exc:
            raise CutoverRejected("Assessment run index is invalid.") from exc
        if not attempt_id:
            if run_index != -1:
                raise CutoverRejected(
                    "Legacy assessment run has an unexpected node run index."
                )
            continue
        attempt = attempts.get(attempt_id)
        if (
            run_index < 0
            or attempt is None
            or str(run.get("requirement_id") or "")
            != str(attempt.get("requirement_id") or "")
        ):
            raise CutoverRejected(
                "Assessment run has an invalid AI analysis node attempt binding."
            )


def _validate_entry_classification_snapshot_integrity(
    row: Mapping[str, Any],
) -> None:
    payload_json = row.get("payload_json")
    payload_sha256 = str(row.get("payload_sha256") or "")
    if not isinstance(payload_json, str) or hashlib.sha256(
        payload_json.encode("utf-8")
    ).hexdigest() != payload_sha256:
        raise CutoverRejected(
            "Entry classification snapshot payload hash does not match."
        )
    envelope_sha256 = str(row.get("envelope_sha256") or "")
    envelope = {
        "schema_version": _ENTRY_CLASSIFICATION_ENVELOPE_VERSION,
        **{
            field: (
                int(row.get(field) or 0)
                if field == "revision"
                else str(row.get(field) or "")
            )
            for field in _ENTRY_CLASSIFICATION_ENVELOPE_FIELDS
        },
    }
    actual_envelope_sha256 = hashlib.sha256(
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        not _SHA256_RE.fullmatch(envelope_sha256)
        or actual_envelope_sha256 != envelope_sha256
    ):
        raise CutoverRejected(
            "Entry classification snapshot persistence envelope does not match."
        )


def _validate_atomization_artifact_integrity(
    *,
    table: str,
    row: Mapping[str, Any],
) -> None:
    payload_json = row.get("payload_json")
    envelope_sha256 = str(row.get("payload_sha256") or "")
    if (
        not isinstance(payload_json, str)
        or not _SHA256_RE.fullmatch(envelope_sha256)
        or hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        != envelope_sha256
    ):
        raise CutoverRejected(f"{table} payload envelope hash does not match.")
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CutoverRejected(f"{table} payload is not valid JSON.") from exc
    if not isinstance(payload, dict) or _canonical_json(payload) != payload_json:
        raise CutoverRejected(f"{table} payload is not canonical JSON.")
    try:
        if table == "requirement_atomization_runs":
            artifact = RequirementAtomizationRun.model_validate(payload)
            bindings = {
                "atomization_run_id": artifact.atomization_run_id,
                "schema_version": artifact.schema_version,
                "project_id": artifact.lineage.project_id,
                "document_id": artifact.lineage.document_id,
                "session_id": artifact.lineage.session_id,
                "review_revision": artifact.lineage.review_revision,
                "atomization_revision": artifact.revision,
                "lineage_sha256": artifact.lineage.lineage_sha256,
                "created_at_utc": artifact.created_at_utc.isoformat(),
            }
        elif table == "requirement_atomization_scope_receipts":
            artifact = AtomizationScopeReceiptV1.model_validate(payload)
            bindings = {
                "receipt_id": artifact.receipt_id,
                "schema_version": artifact.schema_version,
                "project_id": artifact.project_id,
                "document_id": artifact.document_id,
                "session_id": artifact.session_id,
                "review_revision": artifact.review_revision,
                "lineage_sha256": artifact.lineage_sha256,
                "scope_revision": artifact.scope_revision,
                "command_id": artifact.command_id,
                "command_sha256": artifact.command_sha256,
                "atomization_run_id": artifact.atomization_run_id,
                "created_at_utc": artifact.created_at_utc.isoformat(),
            }
        else:
            artifact = AtomizationScopeAttemptV1.model_validate(payload)
            bindings = {
                "attempt_id": artifact.attempt_id,
                "schema_version": artifact.schema_version,
                "project_id": artifact.project_id,
                "document_id": artifact.document_id,
                "session_id": artifact.session_id,
                "review_revision": artifact.review_revision,
                "lineage_sha256": artifact.lineage_sha256,
                "attempt_sequence": artifact.attempt_sequence,
                "command_id": artifact.command_id,
                "command_sha256": artifact.command_sha256,
                "expected_scope_revision": artifact.expected_scope_revision,
                "status": artifact.status.value,
                "started_at_utc": artifact.started_at_utc.isoformat(),
                "updated_at_utc": artifact.updated_at_utc.isoformat(),
            }
    except (TypeError, ValueError) as exc:
        raise CutoverRejected(f"{table} payload integrity is invalid.") from exc
    if any(str(row.get(field)) != str(value) for field, value in bindings.items()):
        raise CutoverRejected(f"{table} column binding does not match its payload.")


def _validate_requirement_enrichment_publication_artifact_integrity(
    *,
    table: str,
    row: Mapping[str, Any],
) -> None:
    payload_json = row.get("payload_json")
    payload_sha256 = str(row.get("payload_sha256") or "")
    state = str(row.get("publication_state") or "").upper()
    if table == "requirement_enrichment_publication_bindings" and state == "PUBLISHING":
        if payload_json not in {None, ""} or payload_sha256:
            raise CutoverRejected(
                "PUBLISHING enrichment binding cannot carry final payload."
            )
        return
    if not isinstance(payload_json, str) or not payload_json:
        raise CutoverRejected(f"{table} payload is missing.")
    try:
        payload = json.loads(payload_json)
        if table == "requirement_enrichment_publication_binding_previews":
            artifact: Any = (
                RequirementEnrichmentPublicationBindingPreviewV2.model_validate(
                    payload
                )
            )
            bindings = {
                "binding_preview_id": artifact.binding_preview_id,
                "schema_version": artifact.schema_version,
                "project_id": artifact.project_id,
                "document_id": artifact.document_id,
                "session_id": artifact.session_id,
                "review_revision": artifact.review_revision,
                "base_active_requirement_set_id": (
                    artifact.base_active_requirement_set_id
                ),
                "accepted_classification_snapshot_id": (
                    artifact.projection.entry_classification_snapshot_id
                ),
                "accepted_classification_sha256": (
                    artifact.projection.entry_classification_sha256
                ),
                "atomization_state": artifact.atomization_state.value,
                "projection_sha256": artifact.projection.projection_sha256,
            }
        elif table == "requirement_enrichment_publication_bindings":
            artifact = RequirementEnrichmentPublicationBindingV2.model_validate(
                payload
            )
            bindings = {
                "binding_id": artifact.binding_id,
                "binding_preview_id": artifact.binding_preview_id,
                "project_id": artifact.project_id,
                "document_id": artifact.document_id,
                "requirement_set_id": artifact.requirement_set_id,
                "projection_sha256": artifact.projection_sha256,
            }
        else:
            artifact = RequirementEnrichmentPublicationRowBindingV2.model_validate(
                payload
            )
            bindings = {
                "binding_id": artifact.binding_id,
                "publication_order": artifact.publication_order,
                "requirement_tree_node_id": artifact.requirement_tree_node_id,
                "requirement_entry_id": artifact.requirement_entry_id,
                "projection_row_sha256": artifact.projection_row_sha256,
                "decision_trace_sha256": artifact.decision_trace_sha256,
            }
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CutoverRejected(f"{table} payload integrity is invalid.") from exc
    if _canonical_json(payload) != payload_json:
        raise CutoverRejected(f"{table} payload is not canonical JSON.")
    if payload_sha256 != artifact.payload_sha256:
        raise CutoverRejected(f"{table} payload hash does not match.")
    if any(str(row.get(field)) != str(value) for field, value in bindings.items()):
        raise CutoverRejected(f"{table} column binding does not match its payload.")


def _requirement_set_impact_receipt_from_row(
    row: Mapping[str, Any],
) -> RequirementSetImpactReceiptV1:
    payload_json = row.get("payload_json")
    if not isinstance(payload_json, str) or not payload_json:
        raise CutoverRejected("Requirement Set impact receipt payload is missing.")
    try:
        payload = json.loads(payload_json)
        receipt = RequirementSetImpactReceiptV1.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CutoverRejected(
            "Requirement Set impact receipt payload integrity is invalid."
        ) from exc
    if not isinstance(payload, dict) or _canonical_json(payload) != payload_json:
        raise CutoverRejected(
            "Requirement Set impact receipt payload is not canonical JSON."
        )
    bindings = {
        "receipt_id": receipt.receipt_id,
        "schema_version": receipt.schema_version,
        "algorithm_version": receipt.algorithm_version,
        "project_id": receipt.project_id,
        "sor_document_id": receipt.document_id,
        "old_requirement_set_id": receipt.old_requirement_set_id,
        "new_requirement_set_id": receipt.new_requirement_set_id,
        "old_tree_sha256": receipt.old_tree_sha256,
        "new_tree_sha256": receipt.new_tree_sha256,
        "payload_sha256": receipt.payload_sha256,
    }
    if any(str(row.get(field) or "") != value for field, value in bindings.items()):
        raise CutoverRejected(
            "Requirement Set impact receipt columns disagree with its payload."
        )
    return receipt


def _validate_requirement_set_impact_receipt_integrity(
    row: Mapping[str, Any],
) -> None:
    _requirement_set_impact_receipt_from_row(row)


def _validate_requirement_set_impact_relationships(
    rewritten_rows: Mapping[str, tuple[dict[str, Any], ...]],
) -> None:
    receipt_rows = rewritten_rows.get("requirement_set_impact_receipts", ())
    assessment_rows = rewritten_rows.get("assessments", ())
    matrix_rows = rewritten_rows.get("requirement_matrix_node_states", ())
    if not receipt_rows and not assessment_rows and not matrix_rows:
        return

    requirement_sets = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("requirement_sets", ())
    }
    project_ids = {
        str(row.get("id") or "")
        for row in rewritten_rows.get("projects", ())
    }
    documents = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("sor_documents", ())
    }
    requirements = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("requirements", ())
    }
    assessments = {
        str(row.get("id") or ""): row for row in assessment_rows
    }
    if len(assessments) != len(assessment_rows):
        raise CutoverRejected("Assessment identities are not unique.")
    evidences_by_requirement: dict[str, list[Mapping[str, Any]]] = {}
    for evidence in rewritten_rows.get("evidences", ()):
        evidences_by_requirement.setdefault(
            str(evidence.get("requirement_id") or ""), []
        ).append(evidence)

    receipts: dict[str, RequirementSetImpactReceiptV1] = {}
    receipt_payload_keys: set[tuple[str, str]] = set()
    for row in receipt_rows:
        receipt = _requirement_set_impact_receipt_from_row(row)
        if receipt.receipt_id in receipts:
            raise CutoverRejected(
                "Requirement Set impact receipt identities are not unique."
            )
        payload_key = (receipt.new_requirement_set_id, receipt.payload_sha256)
        if payload_key in receipt_payload_keys:
            raise CutoverRejected(
                "Requirement Set impact payload is duplicated for one target set."
            )
        receipts[receipt.receipt_id] = receipt
        receipt_payload_keys.add(payload_key)
        _validate_requirement_set_impact_lineage(
            receipt=receipt,
            requirement_sets=requirement_sets,
            requirements=requirements,
            assessments=assessments,
            project_ids=project_ids,
            documents=documents,
        )

    for assessment in assessment_rows:
        _validate_cutover_assessment_impact_binding(
            assessment=assessment,
            receipts=receipts,
            requirement_sets=requirement_sets,
            requirements=requirements,
            assessments=assessments,
            evidences_by_requirement=evidences_by_requirement,
        )

    seen_matrix_ids: set[str] = set()
    seen_matrix_nodes: set[tuple[str, str]] = set()
    for state in matrix_rows:
        state_id = str(state.get("id") or "")
        requirement_set_id = str(state.get("requirement_set_id") or "")
        node_id = str(state.get("node_id") or "")
        if not state_id or state_id in seen_matrix_ids:
            raise CutoverRejected(
                "Requirement Matrix state identities must be non-empty and unique."
            )
        matrix_key = (requirement_set_id, node_id)
        if not requirement_set_id or not node_id or matrix_key in seen_matrix_nodes:
            raise CutoverRejected(
                "Requirement Matrix node bindings must be non-empty and unique per set."
            )
        requirement_set = requirement_sets.get(requirement_set_id)
        if (
            requirement_set is None
            or str(state.get("project_id") or "")
            != str(requirement_set.get("project_id") or "")
            or str(state.get("sor_document_id") or "")
            != str(requirement_set.get("sor_document_id") or "")
        ):
            raise CutoverRejected(
                "Requirement Matrix state scope does not match its Requirement Set."
            )
        if str(state.get("disposition") or "").upper() not in {
            "ANALYZE",
            "RETAIN",
            "EXCLUDE",
        }:
            raise CutoverRejected("Requirement Matrix state disposition is invalid.")
        try:
            revision = int(state.get("revision") or 0)
        except (TypeError, ValueError) as exc:
            raise CutoverRejected(
                "Requirement Matrix state revision is invalid."
            ) from exc
        if revision < 1:
            raise CutoverRejected("Requirement Matrix state revision is invalid.")
        seen_matrix_ids.add(state_id)
        seen_matrix_nodes.add(matrix_key)

    for receipt in receipts.values():
        _validate_cutover_receipt_matrix_state_binding(
            receipt=receipt,
            matrix_rows=matrix_rows,
        )


def _validate_cutover_receipt_matrix_state_binding(
    *,
    receipt: RequirementSetImpactReceiptV1,
    matrix_rows: Iterable[Mapping[str, Any]],
) -> None:
    if receipt.matrix_state_schema_version != (
        REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
    ):
        raise CutoverRejected(
            "Requirement Set impact receipt Matrix state schema is unsupported."
        )
    actual_old = _cutover_matrix_state_sha256(
        project_id=receipt.project_id,
        document_id=receipt.document_id,
        requirement_set_id=receipt.old_requirement_set_id,
        matrix_rows=matrix_rows,
    )
    actual_new = _cutover_matrix_state_sha256(
        project_id=receipt.project_id,
        document_id=receipt.document_id,
        requirement_set_id=receipt.new_requirement_set_id,
        matrix_rows=matrix_rows,
    )
    if (
        actual_old != receipt.old_matrix_state_sha256
        or actual_new != receipt.new_matrix_state_sha256
    ):
        raise CutoverRejected(
            "Requirement Set impact receipt Matrix state hash does not match durable state."
        )


def _cutover_matrix_state_sha256(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    matrix_rows: Iterable[Mapping[str, Any]],
) -> str:
    node_states = sorted(
        (
            {
                "node_id": str(row.get("node_id") or ""),
                "disposition": str(row.get("disposition") or ""),
                "reason": str(row.get("reason") or ""),
                "revision": int(row.get("revision") or 0),
            }
            for row in matrix_rows
            if str(row.get("project_id") or "") == project_id
            and str(row.get("sor_document_id") or "") == document_id
            and str(row.get("requirement_set_id") or "")
            == requirement_set_id
        ),
        key=lambda row: row["node_id"],
    )
    return hashlib.sha256(
        _canonical_json(
            {
                "schema_version": REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION,
                "project_id": project_id,
                "document_id": document_id,
                "requirement_set_id": requirement_set_id,
                "node_states": node_states,
            }
        ).encode("utf-8")
    ).hexdigest()


def _validate_requirement_set_impact_lineage(
    *,
    receipt: RequirementSetImpactReceiptV1,
    requirement_sets: Mapping[str, Mapping[str, Any]],
    requirements: Mapping[str, Mapping[str, Any]],
    assessments: Mapping[str, Mapping[str, Any]],
    project_ids: set[str],
    documents: Mapping[str, Mapping[str, Any]],
) -> None:
    document = documents.get(receipt.document_id)
    if (
        receipt.project_id not in project_ids
        or document is None
        or str(document.get("project_id") or "") != receipt.project_id
    ):
        raise CutoverRejected(
            "Requirement Set impact receipt project/document scope does not exist."
        )
    old_set = requirement_sets.get(receipt.old_requirement_set_id)
    new_set = requirement_sets.get(receipt.new_requirement_set_id)
    if old_set is None or new_set is None:
        raise CutoverRejected(
            "Requirement Set impact receipt references a missing Requirement Set."
        )
    expected_scope = (receipt.project_id, receipt.document_id)
    if any(
        (
            str(requirement_set.get("project_id") or ""),
            str(requirement_set.get("sor_document_id") or ""),
        )
        != expected_scope
        for requirement_set in (old_set, new_set)
    ):
        raise CutoverRejected(
            "Requirement Set impact receipt scope disagrees with its Requirement Sets."
        )
    if str(new_set.get("supersedes_id") or "") != receipt.old_requirement_set_id:
        raise CutoverRejected(
            "Requirement Set impact target must directly supersede its source."
        )
    try:
        old_version = int(old_set.get("version") or 0)
        new_version = int(new_set.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise CutoverRejected(
            "Requirement Set impact lineage version is invalid."
        ) from exc
    if new_version <= old_version:
        raise CutoverRejected(
            "Requirement Set impact target version must be newer than its source."
        )
    if (
        str(old_set.get("requirement_tree_sha256") or "").lower()
        != receipt.old_tree_sha256
        or str(new_set.get("requirement_tree_sha256") or "").lower()
        != receipt.new_tree_sha256
    ):
        raise CutoverRejected(
            "Requirement Set impact receipt tree hashes disagree with durable state."
        )

    seen_old_requirements: set[str] = set()
    seen_new_requirements: set[str] = set()
    for node in receipt.node_results:
        old_requirement = _impact_requirement_binding(
            requirement_id=node.old_requirement_id,
            expected_set_id=receipt.old_requirement_set_id,
            receipt=receipt,
            requirements=requirements,
            side="source",
        )
        _impact_requirement_binding(
            requirement_id=node.new_requirement_id,
            expected_set_id=receipt.new_requirement_set_id,
            receipt=receipt,
            requirements=requirements,
            side="target",
        )
        if node.old_requirement_id is not None:
            if node.old_requirement_id in seen_old_requirements:
                raise CutoverRejected(
                    "Requirement Set impact receipt maps one source requirement twice."
                )
            seen_old_requirements.add(node.old_requirement_id)
        if node.new_requirement_id is not None:
            if node.new_requirement_id in seen_new_requirements:
                raise CutoverRejected(
                    "Requirement Set impact receipt maps one target requirement twice."
                )
            seen_new_requirements.add(node.new_requirement_id)

        source_metadata_id = str(
            node.source_result_metadata.get("assessment_id") or ""
        )
        expects_source_result = node.outcome.value in {"RETAINED", "STALE"} or (
            node.outcome.value == "REMOVED" and bool(node.source_result_metadata)
        )
        source_assessment = assessments.get(source_metadata_id)
        if expects_source_result and (
            old_requirement is None
            or not source_metadata_id
            or source_assessment is None
            or str(source_assessment.get("requirement_id") or "")
            != str(node.old_requirement_id or "")
            or str(source_assessment.get("reference_status") or "CURRENT").upper()
            != "CURRENT"
            or not _cutover_assessment_has_result(source_assessment)
        ):
            raise CutoverRejected(
                "Requirement Set impact receipt source result binding is invalid."
            )
        if source_metadata_id and source_assessment is None:
            raise CutoverRejected(
                "Requirement Set impact receipt references a missing source assessment."
            )
        if source_assessment is not None and node.source_result_metadata:
            expected_metadata = {
                "assessment_updated_at": str(
                    source_assessment.get("updated_at") or ""
                ),
                "result_origin": str(
                    source_assessment.get("result_origin") or "NATIVE"
                ).upper(),
                "source_assessment_id": str(
                    source_assessment.get("source_assessment_id") or ""
                ),
                "has_manual_decision": bool(
                    str(source_assessment.get("final_decision") or "").strip()
                ),
            }
            if any(
                field in node.source_result_metadata
                and node.source_result_metadata[field] != value
                for field, value in expected_metadata.items()
            ):
                raise CutoverRejected(
                    "Requirement Set impact source result metadata changed."
                )
        if (
            node.outcome.value == "NO_PRIOR_RESULT"
            and old_requirement is not None
            and any(
                str(candidate.get("requirement_id") or "")
                == str(node.old_requirement_id or "")
                and str(
                    candidate.get("reference_status") or "CURRENT"
                ).upper()
                == "CURRENT"
                and _cutover_assessment_has_result(candidate)
                for candidate in assessments.values()
            )
        ):
            raise CutoverRejected(
                "Requirement Set impact node claims no prior result, but one is current."
            )


def _impact_requirement_binding(
    *,
    requirement_id: str | None,
    expected_set_id: str,
    receipt: RequirementSetImpactReceiptV1,
    requirements: Mapping[str, Mapping[str, Any]],
    side: str,
) -> Mapping[str, Any] | None:
    if requirement_id is None:
        return None
    requirement = requirements.get(requirement_id)
    if (
        requirement is None
        or str(requirement.get("requirement_set_id") or "") != expected_set_id
        or str(requirement.get("project_id") or "") != receipt.project_id
        or str(requirement.get("sor_document_id") or "") != receipt.document_id
    ):
        raise CutoverRejected(
            f"Requirement Set impact {side} requirement binding is invalid."
        )
    return requirement


def _cutover_assessment_has_result(assessment: Mapping[str, Any]) -> bool:
    return any(
        str(assessment.get(field) or "").strip()
        for field in (
            "ai_decision",
            "compliance_status",
            "ai_reason",
            "final_decision",
        )
    )


def _validate_cutover_assessment_impact_binding(
    *,
    assessment: Mapping[str, Any],
    receipts: Mapping[str, RequirementSetImpactReceiptV1],
    requirement_sets: Mapping[str, Mapping[str, Any]],
    requirements: Mapping[str, Mapping[str, Any]],
    assessments: Mapping[str, Mapping[str, Any]],
    evidences_by_requirement: Mapping[str, list[Mapping[str, Any]]],
) -> None:
    requirement_id = str(assessment.get("requirement_id") or "")
    requirement = requirements.get(requirement_id)
    if requirement is None:
        raise CutoverRejected("Assessment references a missing requirement.")
    reference_status = str(
        assessment.get("reference_status") or "CURRENT"
    ).upper()
    result_origin = str(assessment.get("result_origin") or "NATIVE").upper()
    source_requirement_id = str(
        assessment.get("source_requirement_id") or ""
    )
    source_assessment_id = str(
        assessment.get("source_assessment_id") or ""
    )
    impact_receipt_id = str(assessment.get("impact_receipt_id") or "")
    analysis_input_sha256 = str(
        assessment.get("analysis_input_sha256") or ""
    ).lower()

    if reference_status not in {"CURRENT", "STALE_REFERENCE"}:
        raise CutoverRejected("Assessment reference status is invalid.")
    if result_origin not in {"NATIVE", "CARRIED"}:
        raise CutoverRejected("Assessment result origin is invalid.")
    if analysis_input_sha256 and not _SHA256_RE.fullmatch(analysis_input_sha256):
        raise CutoverRejected("Assessment analysis input hash is invalid.")
    if reference_status == "STALE_REFERENCE" and any(
        str(assessment.get(field) or "").strip()
        for field in ("final_decision", "engineer_reason")
    ):
        raise CutoverRejected(
            "A stale assessment reference cannot retain a human decision."
        )

    if result_origin == "NATIVE":
        if source_assessment_id or impact_receipt_id:
            raise CutoverRejected(
                "A native assessment cannot reference a carried result receipt."
            )
        if reference_status == "CURRENT" and source_requirement_id:
            raise CutoverRejected(
                "A current native assessment cannot reference another requirement."
            )
        if source_requirement_id:
            _validate_legacy_stale_source_requirement(
                requirement=requirement,
                source_requirement_id=source_requirement_id,
                requirement_sets=requirement_sets,
                requirements=requirements,
            )
        return

    if not source_requirement_id or not source_assessment_id or not impact_receipt_id:
        raise CutoverRejected(
            "A carried assessment requires source requirement, assessment, and receipt identities."
        )
    receipt = receipts.get(impact_receipt_id)
    if receipt is None:
        raise CutoverRejected(
            "Assessment impact receipt reference is missing from the cutover source."
        )
    if str(requirement.get("requirement_set_id") or "") != receipt.new_requirement_set_id:
        raise CutoverRejected(
            "Assessment impact receipt does not belong to the same target Requirement Set."
        )
    source_requirement = requirements.get(source_requirement_id)
    source_assessment = assessments.get(source_assessment_id)
    if (
        source_requirement is None
        or str(source_requirement.get("requirement_set_id") or "")
        != receipt.old_requirement_set_id
        or str(source_requirement.get("project_id") or "") != receipt.project_id
        or str(source_requirement.get("sor_document_id") or "")
        != receipt.document_id
        or source_assessment is None
        or str(source_assessment.get("requirement_id") or "")
        != source_requirement_id
    ):
        raise CutoverRejected("Carried assessment source binding is invalid.")
    expected_outcome = "RETAINED" if reference_status == "CURRENT" else "STALE"
    matching_nodes = [
        node
        for node in receipt.node_results
        if node.old_requirement_id == source_requirement_id
        and node.new_requirement_id == requirement_id
        and node.outcome.value == expected_outcome
        and str(node.source_result_metadata.get("assessment_id") or "")
        == source_assessment_id
    ]
    if len(matching_nodes) != 1:
        raise CutoverRejected(
            "Carried assessment does not match exactly one impact receipt node."
        )
    matching_node = matching_nodes[0]
    if (
        str(assessment.get("analysis_input_schema_version") or "")
        != REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
        or analysis_input_sha256
        != str(matching_node.new_analysis_input_sha256 or "")
    ):
        raise CutoverRejected(
            "Carried assessment analysis input does not match its impact receipt."
        )
    if any(
        column not in assessment or column not in source_assessment
        for column in _CARRIED_ASSESSMENT_RESULT_COLUMNS
    ):
        raise CutoverRejected(
            "Carried assessment result payload is incomplete."
        )
    if any(
        assessment.get(column) != source_assessment.get(column)
        for column in _CARRIED_ASSESSMENT_RESULT_COLUMNS
    ):
        raise CutoverRejected(
            "Carried assessment result payload does not match its source."
        )
    expected_final_decision = (
        source_assessment.get("final_decision")
        if expected_outcome == "RETAINED"
        else None
    )
    expected_engineer_reason = (
        source_assessment.get("engineer_reason")
        if expected_outcome == "RETAINED"
        else None
    )
    if (
        assessment.get("final_decision") != expected_final_decision
        or assessment.get("engineer_reason") != expected_engineer_reason
    ):
        raise CutoverRejected(
            "Carried assessment human result does not match its impact outcome."
        )
    if _cutover_evidence_signature(
        evidences_by_requirement.get(requirement_id, ())
    ) != _cutover_evidence_signature(
        evidences_by_requirement.get(source_requirement_id, ())
    ):
        raise CutoverRejected(
            "Carried assessment evidence does not match its source."
        )


def _cutover_evidence_signature(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    materialized = tuple(rows)
    if any(
        column not in row
        for row in materialized
        for column in _CARRIED_EVIDENCE_COLUMNS
    ):
        raise CutoverRejected("Carried assessment evidence payload is incomplete.")
    return tuple(
        sorted(
            _canonical_json(
                [row.get(column) for column in _CARRIED_EVIDENCE_COLUMNS]
            )
            for row in materialized
        )
    )


def _validate_legacy_stale_source_requirement(
    *,
    requirement: Mapping[str, Any],
    source_requirement_id: str,
    requirement_sets: Mapping[str, Mapping[str, Any]],
    requirements: Mapping[str, Mapping[str, Any]],
) -> None:
    source_requirement = requirements.get(source_requirement_id)
    target_set = requirement_sets.get(
        str(requirement.get("requirement_set_id") or "")
    )
    if (
        source_requirement is None
        or target_set is None
        or str(source_requirement.get("project_id") or "")
        != str(requirement.get("project_id") or "")
        or str(source_requirement.get("sor_document_id") or "")
        != str(requirement.get("sor_document_id") or "")
        or str(target_set.get("supersedes_id") or "")
        != str(source_requirement.get("requirement_set_id") or "")
    ):
        raise CutoverRejected(
            "Legacy stale assessment source requirement binding is invalid."
        )


def _validate_requirement_enrichment_publication_relationships(
    rewritten_rows: Mapping[str, tuple[dict[str, Any], ...]],
) -> None:
    preview_rows = rewritten_rows.get(
        "requirement_enrichment_publication_binding_previews",
        (),
    )
    binding_rows = rewritten_rows.get(
        "requirement_enrichment_publication_bindings",
        (),
    )
    row_rows = rewritten_rows.get(
        "requirement_enrichment_publication_row_bindings",
        (),
    )
    if not preview_rows and not binding_rows and not row_rows:
        return
    try:
        previews = {
            artifact.binding_preview_id: artifact
            for artifact in (
                RequirementEnrichmentPublicationBindingPreviewV2.model_validate(
                    json.loads(str(row["payload_json"]))
                )
                for row in preview_rows
            )
        }
        ready_bindings = {
            artifact.binding_id: artifact
            for artifact in (
                RequirementEnrichmentPublicationBindingV2.model_validate(
                    json.loads(str(row["payload_json"]))
                )
                for row in binding_rows
                if str(row.get("publication_state") or "").upper() == "READY"
            )
        }
        row_bindings = tuple(
            RequirementEnrichmentPublicationRowBindingV2.model_validate(
                json.loads(str(row["payload_json"]))
            )
            for row in row_rows
        )
        receipt_artifacts = {
            artifact.receipt_id: artifact
            for artifact in (
                AtomizationScopeReceiptV1.model_validate(
                    json.loads(str(row["payload_json"]))
                )
                for row in rewritten_rows.get(
                    "requirement_atomization_scope_receipts",
                    (),
                )
            )
        }
        run_artifacts = {
            artifact.atomization_run_id: artifact
            for artifact in (
                RequirementAtomizationRun.model_validate(
                    json.loads(str(row["payload_json"]))
                )
                for row in rewritten_rows.get(
                    "requirement_atomization_runs",
                    (),
                )
            )
        }
        core_preview_artifacts = {
            artifact.preview_id: artifact
            for artifact in (
                RequirementPublicationPreview.model_validate(
                    json.loads(str(row["payload_json"]))
                )
                for row in rewritten_rows.get(
                    "requirement_enrichment_publication_previews",
                    (),
                )
            )
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CutoverRejected(
            "Enrichment publication relationship payloads are invalid."
        ) from exc
    classification_by_id = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("entry_classification_snapshots", ())
    }
    for preview in previews.values():
        projection = preview.projection
        classification = classification_by_id.get(
            projection.entry_classification_snapshot_id
        )
        if (
            classification is None
            or str(classification.get("status") or "").upper() != "ACCEPTED"
            or str(classification.get("payload_sha256") or "")
            != projection.entry_classification_sha256
            or str(classification.get("working_tree_id") or "")
            != projection.accepted_working_tree_id
            or str(classification.get("working_tree_sha256") or "")
            != projection.accepted_working_tree_sha256
        ):
            raise CutoverRejected(
                "Enrichment preview references a missing accepted classification."
            )
        if (
            projection.atomization_state
            is RequirementEnrichmentAtomizationStateV2.APPLIED
        ):
            receipt = receipt_artifacts.get(
                projection.atomization_scope_receipt_id
            )
            run = run_artifacts.get(projection.atomization_run_id)
            core_preview = core_preview_artifacts.get(
                projection.publication_preview_id
            )
            if (
                receipt is None
                or run is None
                or core_preview is None
                or receipt.payload_sha256
                != projection.atomization_scope_receipt_sha256
                or receipt.atomization_run_id != projection.atomization_run_id
                or canonical_model_sha256(run)
                != projection.atomization_run_sha256
                or canonical_model_sha256(core_preview)
                != projection.publication_preview_sha256
            ):
                raise CutoverRejected(
                    "APPLIED enrichment preview references missing exact sidecars."
                )

    requirement_sets = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("requirement_sets", ())
    }
    requirements = {
        str(row.get("id") or ""): row
        for row in rewritten_rows.get("requirements", ())
    }
    row_bindings_by_id: dict[
        str,
        list[RequirementEnrichmentPublicationRowBindingV2],
    ] = {}
    for row_binding in row_bindings:
        row_bindings_by_id.setdefault(row_binding.binding_id, []).append(row_binding)
        requirement = requirements.get(row_binding.requirement_entry_id)
        if requirement is None:
            raise CutoverRejected(
                "Enrichment publication row references a missing requirement."
            )

    binding_row_by_id = {
        str(row.get("binding_id") or ""): row for row in binding_rows
    }
    for binding_id, binding_row in binding_row_by_id.items():
        preview = previews.get(str(binding_row.get("binding_preview_id") or ""))
        requirement_set = requirement_sets.get(
            str(binding_row.get("requirement_set_id") or "")
        )
        rows = sorted(
            row_bindings_by_id.get(binding_id, []),
            key=lambda item: item.publication_order,
        )
        if (
            preview is None
            or requirement_set is None
            or tuple(item.publication_order for item in rows)
            != tuple(range(len(preview.projection.rows)))
            or tuple(item.projection_row for item in rows)
            != preview.projection.rows
        ):
            raise CutoverRejected(
                "Enrichment binding does not exactly cover its preview rows."
            )
        try:
            traces_are_exact = all(
                str(
                    requirements[item.requirement_entry_id].get(
                        "decision_reference_json"
                    )
                    or ""
                )
                == canonical_publication_decision_trace_v2_json(
                    preview=preview,
                    row_binding=item,
                )
                for item in rows
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CutoverRejected(
                "Enrichment requirement decision trace is invalid."
            ) from exc
        if not traces_are_exact:
            raise CutoverRejected(
                "Enrichment requirement decision trace is not canonical."
            )
        state = str(binding_row.get("publication_state") or "").upper()
        if state == "READY":
            binding = ready_bindings.get(binding_id)
            if (
                binding is None
                or binding.row_bindings != tuple(rows)
                or str(requirement_set.get("publication_state") or "").upper()
                != "READY"
                or str(
                    requirement_set.get("enrichment_publication_binding_id") or ""
                )
                != binding.binding_id
                or str(
                    requirement_set.get("enrichment_publication_binding_sha256")
                    or ""
                )
                != binding.payload_sha256
                or str(requirement_set.get("publication_key") or "")
            ):
                raise CutoverRejected(
                    "READY enrichment binding does not match its Requirement Set."
                )
        elif (
            str(requirement_set.get("publication_state") or "").upper()
            != "PUBLISHING"
            or str(
                requirement_set.get("enrichment_publication_binding_id") or ""
            )
            or str(
                requirement_set.get("enrichment_publication_binding_sha256") or ""
            )
        ):
            raise CutoverRejected(
                "PUBLISHING enrichment binding has an invalid Requirement Set."
            )

    for requirement_set in requirement_sets.values():
        binding_id = str(
            requirement_set.get("enrichment_publication_binding_id") or ""
        )
        binding_sha256 = str(
            requirement_set.get("enrichment_publication_binding_sha256") or ""
        )
        if bool(binding_id) != bool(binding_sha256):
            raise CutoverRejected(
                "Requirement Set enrichment binding pair is incomplete."
            )
        if binding_id and binding_id not in ready_bindings:
            raise CutoverRejected(
                "Requirement Set references a missing READY enrichment binding."
            )


def _validate_atomization_artifact_relationships(
    rewritten_rows: Mapping[str, tuple[dict[str, Any], ...]],
) -> None:
    try:
        runs = {
            artifact.atomization_run_id: artifact
            for artifact in (
                RequirementAtomizationRun.model_validate(
                    json.loads(str(row["payload_json"]))
                )
                for row in rewritten_rows.get("requirement_atomization_runs", ())
            )
        }
        receipts = {
            artifact.receipt_id: artifact
            for artifact in (
                AtomizationScopeReceiptV1.model_validate(
                    json.loads(str(row["payload_json"]))
                )
                for row in rewritten_rows.get(
                    "requirement_atomization_scope_receipts", ()
                )
            )
        }
        attempts = tuple(
            AtomizationScopeAttemptV1.model_validate(
                json.loads(str(row["payload_json"]))
            )
            for row in rewritten_rows.get(
                "requirement_atomization_scope_attempts", ()
            )
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CutoverRejected(
            "Atomization scope relationship payloads are invalid."
        ) from exc

    running_scope_revisions: set[tuple[str, int]] = set()
    for attempt in attempts:
        if attempt.status.value != "RUNNING":
            continue
        running_key = (
            attempt.lineage_sha256,
            attempt.expected_scope_revision,
        )
        if running_key in running_scope_revisions:
            raise CutoverRejected(
                "Atomization scope revision has multiple RUNNING attempts."
            )
        running_scope_revisions.add(running_key)
    succeeded_attempts = tuple(
        attempt for attempt in attempts if attempt.status.value == "SUCCEEDED"
    )

    attempts_by_receipt: dict[str, list[AtomizationScopeAttemptV1]] = {}
    for attempt in succeeded_attempts:
        attempts_by_receipt.setdefault(attempt.receipt_id, []).append(attempt)

    for receipt in receipts.values():
        run = runs.get(receipt.atomization_run_id)
        if run is None:
            raise CutoverRejected(
                "Atomization scope receipt references a missing run."
            )
        _validate_atomization_run_receipt_relationship(run=run, receipt=receipt)
        matching_attempts = attempts_by_receipt.get(receipt.receipt_id, [])
        if not matching_attempts:
            raise CutoverRejected(
                "Atomization scope receipt must bind at least one SUCCEEDED attempt."
            )
        for attempt in matching_attempts:
            _validate_atomization_attempt_relationship(
                attempt=attempt,
                receipt=receipt,
                run=run,
            )

    for attempt in succeeded_attempts:
        receipt = receipts.get(attempt.receipt_id)
        run = runs.get(attempt.atomization_run_id)
        if receipt is None or run is None:
            raise CutoverRejected(
                "SUCCEEDED atomization scope attempt references missing output."
            )
        _validate_atomization_attempt_relationship(
            attempt=attempt,
            receipt=receipt,
            run=run,
        )

    receipt_run_ids = {receipt.atomization_run_id for receipt in receipts.values()}
    if any(
        run.revision > 0 and run.atomization_run_id not in receipt_run_ids
        for run in runs.values()
    ):
        raise CutoverRejected(
            "Scoped atomization run is missing its scope receipt."
        )


def _validate_atomization_run_receipt_relationship(
    *,
    run: RequirementAtomizationRun,
    receipt: AtomizationScopeReceiptV1,
) -> None:
    if (
        receipt.atomization_run_id != run.atomization_run_id
        or receipt.scope_revision != run.revision
        or receipt.atomization_run_revision != run.revision
        or receipt.project_id != run.lineage.project_id
        or receipt.document_id != run.lineage.document_id
        or receipt.session_id != run.lineage.session_id
        or receipt.review_revision != run.lineage.review_revision
        or receipt.lineage_sha256 != run.lineage.lineage_sha256
    ):
        raise CutoverRejected(
            "Atomization run and scope receipt bindings do not match."
        )


def _validate_atomization_attempt_relationship(
    *,
    attempt: AtomizationScopeAttemptV1,
    receipt: AtomizationScopeReceiptV1,
    run: RequirementAtomizationRun,
) -> None:
    if (
        attempt.receipt_id != receipt.receipt_id
        or attempt.atomization_run_id != receipt.atomization_run_id
        or attempt.atomization_run_id != run.atomization_run_id
        or attempt.atomization_run_revision != receipt.atomization_run_revision
        or attempt.atomization_run_revision != run.revision
        or attempt.expected_scope_revision + 1 != receipt.scope_revision
        or attempt.project_id != receipt.project_id
        or attempt.document_id != receipt.document_id
        or attempt.session_id != receipt.session_id
        or attempt.review_revision != receipt.review_revision
        or attempt.lineage_sha256 != receipt.lineage_sha256
        or attempt.command_id != receipt.command_id
        or attempt.command_sha256 != receipt.command_sha256
        or attempt.operation is not receipt.operation
        or attempt.scope is not receipt.scope
    ):
        raise CutoverRejected(
            "SUCCEEDED atomization scope attempt does not bind its exact receipt and run."
        )


def _validate_entry_classification_command_receipt_integrity(
    row: Mapping[str, Any],
) -> None:
    envelope = {
        "schema_version": _ENTRY_CLASSIFICATION_RECEIPT_ENVELOPE_VERSION,
        **{
            field: str(row.get(field) or "")
            for field in _ENTRY_CLASSIFICATION_RECEIPT_ENVELOPE_FIELDS
        },
    }
    actual_envelope_sha256 = hashlib.sha256(
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    envelope_sha256 = str(row.get("envelope_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(envelope_sha256)
        or actual_envelope_sha256 != envelope_sha256
    ):
        raise CutoverRejected(
            "Entry classification command receipt envelope does not match."
        )


def _rewrite_json_paths(
    value: Any,
    *,
    table: str,
    row: Mapping[str, Any],
    source_database: Path,
    object_by_source_path: dict[str, ObjectCandidate],
    objects_by_key: dict[str, ObjectCandidate],
) -> Any:
    if isinstance(value, list):
        return [
            _rewrite_json_paths(
                item,
                table=table,
                row=row,
                source_database=source_database,
                object_by_source_path=object_by_source_path,
                objects_by_key=objects_by_key,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    rewritten: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key)
        if (
            isinstance(item, str)
            and normalized_key.endswith("_path")
            and item.strip()
        ):
            resolved = _resolve_source_path(item, source_database=source_database)
            if resolved is not None:
                source_token = str(resolved)
                candidate = object_by_source_path.get(source_token)
                if candidate is None:
                    candidate = _object_candidate(
                        raw_path=source_token,
                        table=f"{table}-asset",
                        row=row,
                        source_database=source_database,
                    )
                    _register_candidate(
                        candidate,
                        object_by_source_path=object_by_source_path,
                        objects_by_key=objects_by_key,
                    )
                rewritten[f"{normalized_key[:-5]}_storage_key"] = candidate.storage_key
                continue
        rewritten[normalized_key] = _rewrite_json_paths(
            item,
            table=table,
            row=row,
            source_database=source_database,
            object_by_source_path=object_by_source_path,
            objects_by_key=objects_by_key,
        )
    return rewritten


def _object_candidate(
    *,
    raw_path: str,
    table: str,
    row: Mapping[str, Any],
    source_database: Path,
) -> ObjectCandidate:
    path = _resolve_source_path(raw_path, source_database=source_database)
    if path is None:
        raise CutoverRejected(f"A referenced {table} object is missing.")
    sha256 = _hash_file(path)
    object_id = _safe_token(
        str(
            row.get("id")
            or row.get("document_id")
            or row.get("sor_document_id")
            or sha256[:16]
        )
    )
    project_id = _safe_token(str(row.get("project_id") or "shared"))
    kind = _safe_token(table.replace("_", "-"))
    suffix = _safe_suffix(path.suffix)
    storage_key = f"{kind}/{project_id}/{object_id}/{sha256}{suffix}"
    return ObjectCandidate(
        source_path=path,
        storage_key=storage_key,
        sha256=sha256,
        size=path.stat().st_size,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        object_kind=kind,
    )


def _register_candidate(
    candidate: ObjectCandidate,
    *,
    object_by_source_path: dict[str, ObjectCandidate],
    objects_by_key: dict[str, ObjectCandidate],
) -> None:
    source_token = str(candidate.source_path)
    existing_path = object_by_source_path.get(source_token)
    if existing_path is not None and existing_path.sha256 != candidate.sha256:
        raise CutoverRejected("One source path changed while building the cutover plan.")
    existing_key = objects_by_key.get(candidate.storage_key)
    if existing_key is not None and (
        existing_key.sha256 != candidate.sha256
        or existing_key.size != candidate.size
    ):
        raise CutoverRejected("Deterministic object key collision detected.")
    object_by_source_path[source_token] = candidate
    objects_by_key[candidate.storage_key] = candidate


def _resolve_source_path(
    value: str,
    *,
    source_database: Path,
) -> Path | None:
    token = str(value or "").strip()
    if (
        not token
        or len(token) > 4096
        or "\x00" in token
        or (
            not Path(token).is_absolute()
            and "/" not in token
            and "\\" not in token
        )
    ):
        return None
    raw = Path(token).expanduser()
    candidates = (raw,) if raw.is_absolute() else (
        source_database.parent / raw,
        source_database.parents[1] / raw,
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            if resolved.is_file() and not resolved.is_symlink():
                return resolved
        except OSError:
            continue
    return None


def _contains_existing_local_path(value: Any, *, source_database: Path) -> bool:
    if isinstance(value, list):
        return any(
            _contains_existing_local_path(item, source_database=source_database)
            for item in value
        )
    if isinstance(value, dict):
        return any(
            _contains_existing_local_path(item, source_database=source_database)
            for item in value.values()
        )
    if not isinstance(value, str) or not value.strip():
        return False
    return _resolve_source_path(value, source_database=source_database) is not None


def _target_columns(connection: Any, table: str) -> set[str]:
    rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        """,
        (table,),
    ).fetchall()
    columns = {str(row["column_name"]) for row in rows}
    if not columns:
        raise CutoverRejected(f"Target table {table} is missing.")
    return columns


def _require_empty_target(connection: Any, table_counts: Mapping[str, int]) -> None:
    occupied: list[str] = []
    for table in table_counts:
        row = connection.execute(
            f'SELECT count(*) AS count FROM "{table}"'
        ).fetchone()
        if row is not None and int(row["count"]) > 0:
            occupied.append(table)
    if occupied:
        raise CutoverRejected(
            "Target business schema is not empty: " + ", ".join(sorted(occupied))
        )


def _validate_target_invariants(connection: Any) -> dict[str, int]:
    checks = {
        "document_active_set_identity": """
            SELECT count(*) AS count
            FROM sor_documents AS document
            LEFT JOIN requirement_sets AS selected
              ON selected.id = document.active_requirement_set_id
            WHERE document.active_requirement_set_id <> ''
              AND (
                selected.id IS NULL
                OR selected.project_id <> document.project_id
                OR selected.sor_document_id <> document.id
                OR selected.is_active <> 1
              )
        """,
        "active_set_document_pointer": """
            SELECT count(*) AS count
            FROM requirement_sets AS selected
            JOIN sor_documents AS document
              ON document.id = selected.sor_document_id
            WHERE selected.is_active = 1
              AND document.active_requirement_set_id <> selected.id
        """,
        "requirement_set_scope": """
            SELECT count(*) AS count
            FROM requirements AS requirement
            JOIN requirement_sets AS selected
              ON selected.id = requirement.requirement_set_id
            WHERE requirement.project_id <> selected.project_id
               OR requirement.sor_document_id <> selected.sor_document_id
        """,
        "requirement_matrix_state_scope": """
            SELECT count(*) AS count
            FROM requirement_matrix_node_states AS state
            LEFT JOIN requirement_sets AS selected
              ON selected.id = state.requirement_set_id
            WHERE selected.id IS NULL
               OR state.project_id <> selected.project_id
               OR state.sor_document_id <> selected.sor_document_id
               OR state.node_id = ''
               OR state.disposition NOT IN ('ANALYZE', 'RETAIN', 'EXCLUDE')
               OR state.revision < 1
        """,
        "analysis_node_target_scope": """
            SELECT count(*) AS count
            FROM ai_analysis_job_targets AS target
            LEFT JOIN ai_analysis_jobs AS job ON job.id = target.job_id
            LEFT JOIN sor_documents AS document
              ON document.id = target.sor_document_id
            LEFT JOIN requirements AS requirement
              ON requirement.id = target.requirement_id
            WHERE job.id IS NULL
               OR document.id IS NULL
               OR requirement.id IS NULL
               OR job.publication_mode <> 'PER_NODE_RECOVERABLE'
               OR job.sor_document_id <> target.sor_document_id
               OR requirement.sor_document_id <> target.sor_document_id
               OR requirement.requirement_set_id <> job.requirement_set_id
               OR target.requirement_code = ''
        """,
        "analysis_node_attempt_scope": """
            SELECT count(*) AS count
            FROM ai_analysis_node_attempts AS attempt
            LEFT JOIN ai_analysis_job_targets AS target
              ON target.id = attempt.target_id
            WHERE target.id IS NULL
               OR attempt.job_id <> target.job_id
               OR attempt.node_id <> target.node_id
               OR attempt.requirement_id <> target.requirement_id
        """,
        "assessment_run_node_attempt_scope": """
            SELECT count(*) AS count
            FROM assessment_runs AS run
            LEFT JOIN ai_analysis_node_attempts AS attempt
              ON attempt.id = run.analysis_node_attempt_id
            WHERE (run.analysis_node_attempt_id = '') <> (run.run_index = -1)
               OR (
                    run.analysis_node_attempt_id <> ''
                    AND (
                      attempt.id IS NULL
                      OR run.requirement_id <> attempt.requirement_id
                      OR run.run_index < 0
                    )
               )
        """,
        "requirement_set_impact_lineage": """
            SELECT count(*) AS count
            FROM requirement_set_impact_receipts AS receipt
            LEFT JOIN projects AS project
              ON project.id = receipt.project_id
            LEFT JOIN sor_documents AS document
              ON document.id = receipt.sor_document_id
            LEFT JOIN requirement_sets AS source_set
              ON source_set.id = receipt.old_requirement_set_id
            LEFT JOIN requirement_sets AS target_set
              ON target_set.id = receipt.new_requirement_set_id
            WHERE project.id IS NULL
               OR document.id IS NULL
               OR document.project_id <> receipt.project_id
               OR source_set.id IS NULL
               OR target_set.id IS NULL
               OR source_set.project_id <> receipt.project_id
               OR target_set.project_id <> receipt.project_id
               OR source_set.sor_document_id <> receipt.sor_document_id
               OR target_set.sor_document_id <> receipt.sor_document_id
               OR target_set.supersedes_id <> source_set.id
               OR target_set.version <= source_set.version
               OR source_set.requirement_tree_sha256 <> receipt.old_tree_sha256
               OR target_set.requirement_tree_sha256 <> receipt.new_tree_sha256
        """,
        "assessment_impact_binding": f"""
            SELECT count(*) AS count
            FROM assessments AS assessment
            LEFT JOIN requirements AS requirement
              ON requirement.id = assessment.requirement_id
            LEFT JOIN requirement_sets AS target_set
              ON target_set.id = requirement.requirement_set_id
            LEFT JOIN requirement_set_impact_receipts AS receipt
              ON receipt.receipt_id = assessment.impact_receipt_id
            LEFT JOIN requirements AS source_requirement
              ON source_requirement.id = assessment.source_requirement_id
            LEFT JOIN assessments AS source_assessment
              ON source_assessment.id = assessment.source_assessment_id
            WHERE assessment.reference_status NOT IN ('CURRENT', 'STALE_REFERENCE')
               OR assessment.result_origin NOT IN ('NATIVE', 'CARRIED')
               OR (
                    assessment.analysis_input_sha256 <> ''
                    AND assessment.analysis_input_sha256 !~ '^[0-9a-f]{{64}}$'
               )
               OR (
                    assessment.reference_status = 'STALE_REFERENCE'
                    AND (
                      COALESCE(assessment.final_decision, '') <> ''
                      OR COALESCE(assessment.engineer_reason, '') <> ''
                    )
               )
               OR (
                    assessment.result_origin = 'NATIVE'
                    AND (
                      assessment.source_assessment_id <> ''
                      OR assessment.impact_receipt_id <> ''
                      OR (
                        assessment.reference_status = 'CURRENT'
                        AND assessment.source_requirement_id <> ''
                      )
                      OR (
                        assessment.source_requirement_id <> ''
                        AND (
                          source_requirement.id IS NULL
                          OR target_set.supersedes_id
                             <> source_requirement.requirement_set_id
                          OR source_requirement.project_id <> requirement.project_id
                          OR source_requirement.sor_document_id
                             <> requirement.sor_document_id
                        )
                      )
                    )
               )
               OR (
                    assessment.result_origin = 'CARRIED'
                    AND (
                      assessment.source_requirement_id = ''
                      OR assessment.source_assessment_id = ''
                      OR assessment.impact_receipt_id = ''
                      OR receipt.receipt_id IS NULL
                      OR source_requirement.id IS NULL
                      OR source_assessment.id IS NULL
                      OR receipt.new_requirement_set_id
                         <> requirement.requirement_set_id
                      OR receipt.old_requirement_set_id
                         <> source_requirement.requirement_set_id
                      OR receipt.project_id <> requirement.project_id
                      OR receipt.sor_document_id <> requirement.sor_document_id
                      OR source_assessment.requirement_id
                         <> source_requirement.id
                      OR assessment.analysis_input_schema_version
                         <> '{REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION}'
                      OR ROW(
                           assessment.profile_id,
                           assessment.provider,
                           assessment.retrieval_backend,
                           assessment.ai_decision,
                           assessment.compliance_status,
                           assessment.ai_reason,
                           assessment.ai_confidence,
                           assessment.assessment_mode,
                           assessment.analysis_label,
                           assessment.analysis_basis,
                           assessment.normalized_requirement,
                           assessment.risk_level,
                           assessment.clarification_question,
                           assessment.model_name,
                           assessment.prompt_version,
                           assessment.evidence_findings_json,
                           assessment.structured_information_gap_json,
                           assessment.sub_judgments_json
                         ) IS DISTINCT FROM ROW(
                           source_assessment.profile_id,
                           source_assessment.provider,
                           source_assessment.retrieval_backend,
                           source_assessment.ai_decision,
                           source_assessment.compliance_status,
                           source_assessment.ai_reason,
                           source_assessment.ai_confidence,
                           source_assessment.assessment_mode,
                           source_assessment.analysis_label,
                           source_assessment.analysis_basis,
                           source_assessment.normalized_requirement,
                           source_assessment.risk_level,
                           source_assessment.clarification_question,
                           source_assessment.model_name,
                           source_assessment.prompt_version,
                           source_assessment.evidence_findings_json,
                           source_assessment.structured_information_gap_json,
                           source_assessment.sub_judgments_json
                         )
                      OR assessment.final_decision IS DISTINCT FROM CASE
                           WHEN assessment.reference_status = 'CURRENT'
                           THEN source_assessment.final_decision
                           ELSE NULL
                         END
                      OR assessment.engineer_reason IS DISTINCT FROM CASE
                           WHEN assessment.reference_status = 'CURRENT'
                           THEN source_assessment.engineer_reason
                           ELSE NULL
                         END
                      OR EXISTS (
                           SELECT source_doc_id, source_doc_name,
                                  source_doc_version, retrieval_evidence_id,
                                  content_hash, evidence_contract_version,
                                  content_kind, evidence_metadata_json,
                                  source_location, excerpt, relevance
                           FROM evidences
                           WHERE requirement_id = assessment.requirement_id
                           EXCEPT ALL
                           SELECT source_doc_id, source_doc_name,
                                  source_doc_version, retrieval_evidence_id,
                                  content_hash, evidence_contract_version,
                                  content_kind, evidence_metadata_json,
                                  source_location, excerpt, relevance
                           FROM evidences
                           WHERE requirement_id = source_requirement.id
                         )
                      OR EXISTS (
                           SELECT source_doc_id, source_doc_name,
                                  source_doc_version, retrieval_evidence_id,
                                  content_hash, evidence_contract_version,
                                  content_kind, evidence_metadata_json,
                                  source_location, excerpt, relevance
                           FROM evidences
                           WHERE requirement_id = source_requirement.id
                           EXCEPT ALL
                           SELECT source_doc_id, source_doc_name,
                                  source_doc_version, retrieval_evidence_id,
                                  content_hash, evidence_contract_version,
                                  content_kind, evidence_metadata_json,
                                  source_location, excerpt, relevance
                           FROM evidences
                           WHERE requirement_id = assessment.requirement_id
                         )
                      OR NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                          (receipt.payload_json::jsonb)->'node_results'
                        ) AS node
                        WHERE node->>'old_requirement_id'
                              = assessment.source_requirement_id
                          AND node->>'new_requirement_id'
                              = assessment.requirement_id
                          AND node->'source_result_metadata'->>'assessment_id'
                              = assessment.source_assessment_id
                          AND node->>'outcome' = CASE
                            WHEN assessment.reference_status = 'CURRENT'
                            THEN 'RETAINED'
                            ELSE 'STALE'
                          END
                          AND COALESCE(
                                node->>'new_analysis_input_sha256', ''
                              ) = assessment.analysis_input_sha256
                      )
                    )
               )
        """,
        "enrichment_publication_binding_pair": """
            SELECT count(*) AS count
            FROM requirement_sets AS selected
            LEFT JOIN requirement_enrichment_publication_bindings AS binding
              ON binding.binding_id = selected.enrichment_publication_binding_id
            WHERE
              (
                (selected.enrichment_publication_binding_id = '')
                <> (selected.enrichment_publication_binding_sha256 = '')
              )
              OR (
                selected.enrichment_publication_binding_id <> ''
                AND (
                  selected.publication_key <> ''
                  OR selected.publication_state <> 'READY'
                  OR binding.binding_id IS NULL
                  OR binding.publication_state <> 'READY'
                  OR binding.requirement_set_id <> selected.id
                  OR binding.payload_sha256
                     <> selected.enrichment_publication_binding_sha256
                )
              )
        """,
        "enrichment_publishing_set_state": """
            SELECT count(*) AS count
            FROM requirement_enrichment_publication_bindings AS binding
            JOIN requirement_sets AS selected
              ON selected.id = binding.requirement_set_id
            WHERE binding.publication_state = 'PUBLISHING'
              AND (
                selected.publication_state <> 'PUBLISHING'
                OR selected.status <> 'DRAFT'
                OR selected.is_active <> 0
                OR selected.enrichment_publication_binding_id <> ''
                OR selected.enrichment_publication_binding_sha256 <> ''
              )
        """,
        "knowledge_sync_object_identity": """
            SELECT count(*) AS count
            FROM knowledge_document_syncs AS sync
            LEFT JOIN stored_objects AS object
              ON object.storage_key = sync.payload_storage_key
            WHERE sync.payload_storage_key IS NOT NULL
              AND object.storage_key IS NULL
        """,
    }
    violations: dict[str, int] = {}
    for name, statement in checks.items():
        row = connection.execute(statement).fetchone()
        count = int((row or {}).get("count") or 0)
        violations[name] = count
    failed = [name for name, count in violations.items() if count]
    if failed:
        raise CutoverRejected(
            "Target business invariant validation failed: "
            + ", ".join(sorted(failed))
        )
    return violations


def _require_quiescent_source(source: Path) -> None:
    wal = Path(f"{source}-wal")
    shared_memory = Path(f"{source}-shm")
    if wal.exists() or shared_memory.exists():
        raise CutoverRejected(
            "Stop the SQLite application and remove no files manually; the cutover "
            "requires SQLite to close and checkpoint its WAL first."
        )
    expected = _hash_file(source)
    with _source_connection(source):
        observed = _hash_file(source)
    if observed != expected:
        raise CutoverRejected("SQLite source changed during the cutover preflight.")


@contextmanager
def _source_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]).lower() != "ok":
            raise CutoverRejected("SQLite quick_check failed.")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise CutoverRejected("SQLite foreign_key_check found violations.")
        yield connection
    finally:
        connection.close()


def _primary_key_digest(
    rows: Iterable[Mapping[str, Any]],
    columns: tuple[str, ...],
) -> str:
    tokens = sorted(
        _canonical_json([row.get(column) for column in columns])
        for row in rows
    )
    return _value_digest(tokens)


def _row_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    return _value_digest(
        sorted(_canonical_json(dict(row)) for row in rows)
    )


def _value_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _parse_json(value: Any, *, table: str, column: str) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CutoverRejected(f"{table}.{column} contains invalid JSON.") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_token(value: str) -> str:
    normalized = _SAFE_TOKEN_RE.sub("-", str(value or "").strip()).strip(".-")
    return normalized[:128] or "unknown"


def _safe_suffix(value: str) -> str:
    normalized = str(value or "").lower()
    return normalized if re.fullmatch(r"\.[a-z0-9]{1,12}", normalized) else ""


__all__ = [
    "CutoverPlan",
    "CutoverRejected",
    "ObjectCandidate",
    "build_cutover_plan",
    "execute_cutover",
]
