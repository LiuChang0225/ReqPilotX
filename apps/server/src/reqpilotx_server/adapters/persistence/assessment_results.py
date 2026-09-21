"""Database-neutral assessment serving-result persistence.

The active connection is supplied by ``reqpilotx_server.db`` and therefore follows the
runtime's explicit PostgreSQL or isolated test configuration.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from reqpilotx_server.adapters.persistence.requirement_matrix_states import (
    lock_requirement_matrix_sets_in_connection,
)
from reqpilotx_server.ports.job_queue import JobLeaseFence, JobLeaseLostError

from reqpilotx_server import db


PER_NODE_RECOVERABLE_PUBLICATION_MODE = "PER_NODE_RECOVERABLE"
ASSESSMENT_NODE_RESULT_SCHEMA_VERSION = "assessment-node-result/v1"


def _assert_active_analysis_delivery_fence(
    conn: Any,
    *,
    job_id: str,
    delivery_fence: JobLeaseFence,
) -> None:
    durable = conn.execute(
        """
        SELECT runtime.id
        FROM runtime_jobs AS runtime
        INNER JOIN ai_analysis_jobs AS analysis
            ON analysis.id = ? AND analysis.runtime_job_id = runtime.id
        WHERE runtime.id = ?
          AND runtime.generation = ?
          AND runtime.lease_owner = ?
          AND runtime.status = 'RUNNING'
          AND runtime.cancel_requested = FALSE
          AND runtime.lease_expires_at >= now()
        FOR UPDATE OF runtime
        """,
        (
            job_id,
            delivery_fence.job_id,
            delivery_fence.generation,
            delivery_fence.worker_id,
        ),
    ).fetchone()
    if durable is None:
        raise JobLeaseLostError(
            "The durable delivery fence no longer owns this analysis job."
        )


def claim_analysis_job(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    profile_id: str,
    provider: str,
    assessment_mode: str,
    retrieval_backend: str,
    chat_model: str,
    embedding_model: str,
    total_requirements: int,
    queued_message: str,
    command_id: str = "",
    command_sha256: str = "",
    scope_schema_version: str = "",
    scope_kind: str = "",
    scope_payload_json: str = "{}",
    scope_sha256: str = "",
    requirement_set_version: int | None = None,
    requirement_tree_sha256: str | None = None,
    expected_knowledge_base_version: int | None = None,
    publication_mode: str = "ATOMIC_BATCH",
    target_descriptors: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Atomically replay one exact command or create the sole document job."""

    job_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not db._lock_requirement_set_document_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=document_id,
        ):
            raise RuntimeError(
                f"SOR document {document_id} disappeared before AI job claim."
            )
        document = conn.execute(
            """
            SELECT d.active_requirement_set_id, d.requirement_set_version,
                   p.knowledge_base_version
            FROM sor_documents d
            INNER JOIN projects p ON p.id = d.project_id
            WHERE d.id = ? AND d.project_id = ?
            """,
            (document_id, project_id),
        ).fetchone()
        if document is None:
            raise RuntimeError(f"SOR document {document_id} disappeared before AI job claim.")
        active_requirement_set_id = str(
            document["active_requirement_set_id"] or ""
        )
        expected_requirement_set_id = str(requirement_set_id or "").strip()
        if (
            not expected_requirement_set_id
            or active_requirement_set_id != expected_requirement_set_id
        ):
            raise ValueError("requirement_set_selection_changed")
        lock_requirement_matrix_sets_in_connection(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_ids=(expected_requirement_set_id,),
        )
        target_set = conn.execute(
            """
            SELECT version, status, is_active, requirement_tree_sha256
            FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (expected_requirement_set_id, project_id, document_id),
        ).fetchone()
        if target_set is None:
            raise ValueError("requirement_set_selection_changed")
        current_knowledge_base_version = max(
            int(document["knowledge_base_version"] or 0), 0
        )
        if (
            expected_knowledge_base_version is not None
            and current_knowledge_base_version
            != max(int(expected_knowledge_base_version or 0), 0)
        ):
            raise ValueError("analysis_knowledge_base_changed")
        expected_set_version = max(
            int(
                target_set["version"]
                if requirement_set_version is None
                else requirement_set_version
            ),
            0,
        )
        expected_tree_sha256 = str(
            target_set["requirement_tree_sha256"]
            if requirement_tree_sha256 is None
            else requirement_tree_sha256
            or ""
        ).strip()
        persisted_tree_sha256 = str(
            target_set["requirement_tree_sha256"] or ""
        ).strip()
        normalized_scope_sha256 = str(scope_sha256 or "").strip()
        if (
            int(target_set["version"] or 0)
            != expected_set_version
            or str(target_set["status"] or "").strip().upper() != "LOCKED"
            or not bool(int(target_set["is_active"] or 0))
            or (normalized_scope_sha256 and not persisted_tree_sha256)
            or (
                persisted_tree_sha256
                and persisted_tree_sha256 != expected_tree_sha256
            )
            or int(document["requirement_set_version"] or 0)
            != expected_set_version
        ):
            raise ValueError("requirement_set_selection_changed")
        normalized_command_id = str(command_id or "").strip()
        normalized_command_sha256 = str(command_sha256 or "").strip()
        if normalized_command_id:
            replay = conn.execute(
                """
                SELECT * FROM ai_analysis_jobs
                WHERE sor_document_id = ? AND command_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (document_id, normalized_command_id),
            ).fetchone()
            if replay is not None:
                if (
                    str(replay["command_sha256"] or "").strip()
                    != normalized_command_sha256
                ):
                    raise ValueError("analysis_command_id_conflict")
                return _row_to_dict(replay), False
        existing = conn.execute(
            """
            SELECT *
            FROM ai_analysis_jobs
            WHERE sor_document_id = ?
              AND status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if existing is not None:
            existing_command_id = str(existing["command_id"] or "").strip()
            existing_command_sha256 = str(
                existing["command_sha256"] or ""
            ).strip()
            if (
                not normalized_command_id
                and not existing_command_id
                and existing_command_sha256 == normalized_command_sha256
            ):
                if (
                    str(existing["requirement_set_id"] or "")
                    != expected_requirement_set_id
                ):
                    raise ValueError("requirement_set_selection_changed")
                return _row_to_dict(existing), False
            raise ValueError("analysis_job_already_running")
        if not normalized_scope_sha256:
            cursor = conn.execute(
                """
                UPDATE sor_documents
                SET ai_status = 'RUNNING', workflow_stage = 'AI_ANALYZING', updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (timestamp, document_id, project_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"SOR document {document_id} disappeared before AI job claim."
                )
        conn.execute(
            """
            INSERT INTO ai_analysis_jobs
            (id, project_id, sor_document_id, requirement_set_id, knowledge_base_version,
             profile_id, provider, assessment_mode,
             retrieval_backend, chat_model, embedding_model,
             command_id, command_sha256, scope_schema_version, scope_kind,
             scope_payload_json, scope_sha256, requirement_set_version,
             requirement_tree_sha256, publication_mode, published_requirements,
             status, total_requirements,
             completed_requirements, analyzed_requirements, info_requirements,
             screened_requirements, current_requirement_id, current_requirement_code,
             current_requirement_index, current_stage, last_message, error_message,
             created_at, started_at, finished_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                    'PENDING', ?, 0, 0, 0, 0, '', '', 0,
                    'queued', ?, '', ?, '', '', ?)
            """,
            (
                job_id,
                project_id,
                document_id,
                expected_requirement_set_id,
                current_knowledge_base_version,
                str(profile_id or "").strip(),
                str(provider or "").strip(),
                str(assessment_mode or "").strip(),
                str(retrieval_backend or "").strip(),
                str(chat_model or "").strip(),
                str(embedding_model or "").strip(),
                normalized_command_id,
                normalized_command_sha256,
                str(scope_schema_version or "").strip(),
                str(scope_kind or "").strip().upper(),
                str(scope_payload_json or "").strip(),
                normalized_scope_sha256,
                expected_set_version,
                expected_tree_sha256,
                str(publication_mode or "").strip().upper(),
                max(int(total_requirements or 0), 0),
                str(queued_message or "").strip(),
                timestamp,
                timestamp,
            ),
        )
        normalized_publication_mode = str(publication_mode or "").strip().upper()
        if normalized_publication_mode == "PER_NODE_RECOVERABLE":
            descriptors = _normalize_target_descriptors(target_descriptors or [])
            scope_pairs = _scope_pairs_from_payload(
                str(scope_payload_json or "{}"),
                expected_sha256=normalized_scope_sha256,
                field="target_pairs",
            )
            descriptor_pairs = tuple(
                (item["node_id"], item["requirement_id"])
                for item in descriptors
            )
            if descriptor_pairs != scope_pairs:
                raise RuntimeError(
                    "AI analysis target ledger does not exactly match the frozen scope."
                )
            if len(descriptors) != max(int(total_requirements or 0), 0):
                raise RuntimeError(
                    "AI analysis target ledger count does not match the claimed job."
                )
            for item in descriptors:
                conn.execute(
                    """
                    INSERT INTO ai_analysis_job_targets
                    (id, job_id, sor_document_id, node_id, requirement_id,
                     requirement_code, source_order, status, latest_attempt_no,
                     latest_attempt_id, published_attempt_id, retryable,
                     error_code, error_message, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'WAITING', 0, '', '', 0, '', '', ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        job_id,
                        document_id,
                        item["node_id"],
                        item["requirement_id"],
                        item["requirement_code"],
                        item["source_order"],
                        timestamp,
                        timestamp,
                    ),
                )
        elif target_descriptors:
            raise RuntimeError(
                "Atomic AI analysis jobs must not materialize per-node targets."
            )
        conn.execute(
            """
            INSERT INTO ai_analysis_events
            (id, job_id, sor_document_id, profile_id, provider, event_type, stage, message,
             requirement_id, requirement_code, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, 'job_created', 'queued', ?, '', '', ?, ?)
            """,
            (
                str(uuid.uuid4()),
                job_id,
                document_id,
                str(profile_id or "").strip(),
                str(provider or "").strip(),
                str(queued_message or "").strip(),
                json.dumps(
                    {
                        "profile_id": str(profile_id or "").strip(),
                        "provider": str(provider or "").strip(),
                        "total_requirements": max(int(total_requirements or 0), 0),
                        "requirement_set_id": requirement_set_id,
                        "requirement_set_version": max(
                            expected_set_version, 0
                        ),
                        "requirement_tree_sha256": str(
                            expected_tree_sha256
                        ).strip(),
                        "command_id": normalized_command_id,
                        "scope_kind": str(scope_kind or "").strip().upper(),
                        "scope_sha256": str(scope_sha256 or "").strip(),
                        "publication_mode": str(
                            publication_mode or ""
                        ).strip().upper(),
                        "knowledge_base_version": current_knowledge_base_version,
                    },
                    ensure_ascii=False,
                ),
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM ai_analysis_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"AI analysis job {job_id} disappeared during atomic claim.")
        return _row_to_dict(row), True


def recover_analysis_job_targets(job_id: str) -> list[dict[str, Any]]:
    """Fence attempts left RUNNING by an interrupted delivery.

    A durable redelivery may call the provider again, so provider execution is
    deliberately at-least-once.  The old attempt is made terminal before the
    target becomes claimable, which prevents a late worker from publishing
    through its stale fence token.
    """

    recovered_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError("analysis_job_not_found")
        if (
            str(job["publication_mode"] or "").strip().upper()
            != PER_NODE_RECOVERABLE_PUBLICATION_MODE
        ):
            return []
        status = str(job["status"] or "").strip().upper()
        if status not in {"PENDING", "RUNNING"}:
            return _target_rows_in_connection(conn, job_id)
        running = conn.execute(
            """
            SELECT latest_attempt_id
            FROM ai_analysis_job_targets
            WHERE job_id = ? AND status = 'RUNNING' AND latest_attempt_id <> ''
            """,
            (job_id,),
        ).fetchall()
        attempt_ids = [str(row["latest_attempt_id"] or "") for row in running]
        if attempt_ids:
            placeholders = ", ".join("?" for _ in attempt_ids)
            conn.execute(
                f"""
                UPDATE ai_analysis_node_attempts
                SET status = 'FAILED', error_code = 'analysis_worker_interrupted',
                    error_message = 'The previous worker stopped before this item completed.',
                    finished_at = ?, updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'RUNNING'
                """,
                [recovered_at, recovered_at, *attempt_ids],
            )
        conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'WAITING', latest_attempt_id = '', retryable = 0,
                error_code = '', error_message = '', updated_at = ?
            WHERE job_id = ? AND status = 'RUNNING'
            """,
            (recovered_at, job_id),
        )
        return _target_rows_in_connection(conn, job_id)


def claim_analysis_node_attempt(
    *,
    job_id: str,
    target_id: str,
    delivery_fence: JobLeaseFence | None = None,
) -> dict[str, Any]:
    """Claim one WAITING target and return its immutable attempt fence."""

    attempt_id = str(uuid.uuid4())
    fence_token = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if delivery_fence is not None:
            _assert_active_analysis_delivery_fence(
                conn,
                job_id=job_id,
                delivery_fence=delivery_fence,
            )
        job = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError("analysis_job_not_found")
        if str(job["status"] or "").strip().upper() != "RUNNING":
            raise RuntimeError("analysis_job_not_running")
        if (
            str(job["publication_mode"] or "").strip().upper()
            != PER_NODE_RECOVERABLE_PUBLICATION_MODE
        ):
            raise RuntimeError("analysis_job_not_per_node")
        target = conn.execute(
            """
            SELECT * FROM ai_analysis_job_targets
            WHERE id = ? AND job_id = ?
            """,
            (target_id, job_id),
        ).fetchone()
        if target is None:
            raise LookupError("analysis_target_not_found")
        if str(target["status"] or "").strip().upper() != "WAITING":
            raise RuntimeError("analysis_target_not_waiting")
        attempt_no = max(int(target["latest_attempt_no"] or 0), 0) + 1
        conn.execute(
            """
            INSERT INTO ai_analysis_node_attempts
            (id, target_id, job_id, node_id, requirement_id, attempt_no,
             fence_token, status, result_schema_version, result_payload_json,
             result_sha256, analysis_input_schema_version,
             analysis_input_sha256, error_code, error_message,
             started_at, finished_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', '', '{}', '', '', '', '', '', ?, '', ?)
            """,
            (
                attempt_id,
                target_id,
                job_id,
                str(target["node_id"] or ""),
                str(target["requirement_id"] or ""),
                attempt_no,
                fence_token,
                started_at,
                started_at,
            ),
        )
        cursor = conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'RUNNING', latest_attempt_no = ?, latest_attempt_id = ?,
                retryable = 0, error_code = '', error_message = '', updated_at = ?
            WHERE id = ? AND job_id = ? AND status = 'WAITING'
            """,
            (attempt_no, attempt_id, started_at, target_id, job_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("analysis_target_claim_conflict")
        claimed = conn.execute(
            """
            SELECT t.*, a.fence_token, a.attempt_no, a.started_at AS attempt_started_at
            FROM ai_analysis_job_targets t
            INNER JOIN ai_analysis_node_attempts a ON a.id = t.latest_attempt_id
            WHERE t.id = ?
            """,
            (target_id,),
        ).fetchone()
        if claimed is None:
            raise RuntimeError("analysis_target_attempt_disappeared")
        item = _row_to_dict(claimed)
        item["attempt_id"] = attempt_id
        return item


def stage_analysis_node_result(
    *,
    job_id: str,
    target_id: str,
    attempt_id: str,
    fence_token: str,
    result: dict[str, Any],
    analysis_input_schema_version: str,
    analysis_input_sha256: str,
) -> dict[str, Any]:
    """Persist one immutable provider result before serving publication."""

    input_schema = str(analysis_input_schema_version or "").strip()
    input_sha256 = str(analysis_input_sha256 or "").strip()
    _validate_analysis_input_provenance(input_schema, input_sha256)
    payload = {
        "schema_version": ASSESSMENT_NODE_RESULT_SCHEMA_VERSION,
        "result": result,
    }
    payload_json = _canonical_json(payload)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    completed_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT status, publication_mode FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError("analysis_job_not_found")
        if str(job["status"] or "").strip().upper() != "RUNNING":
            raise RuntimeError("analysis_node_attempt_cancelled")
        target = conn.execute(
            """
            SELECT * FROM ai_analysis_job_targets
            WHERE id = ? AND job_id = ?
            """,
            (target_id, job_id),
        ).fetchone()
        attempt = conn.execute(
            """
            SELECT * FROM ai_analysis_node_attempts
            WHERE id = ? AND target_id = ? AND job_id = ?
            """,
            (attempt_id, target_id, job_id),
        ).fetchone()
        if (
            target is None
            or attempt is None
            or str(target["status"] or "").upper() != "RUNNING"
            or str(target["latest_attempt_id"] or "") != attempt_id
            or str(attempt["status"] or "").upper() != "RUNNING"
            or str(attempt["fence_token"] or "") != str(fence_token or "")
        ):
            raise RuntimeError("analysis_node_attempt_stale")
        attempt_cursor = conn.execute(
            """
            UPDATE ai_analysis_node_attempts
            SET status = 'SUCCEEDED', result_schema_version = ?,
                result_payload_json = ?, result_sha256 = ?,
                analysis_input_schema_version = ?, analysis_input_sha256 = ?,
                error_code = '', error_message = '', finished_at = ?, updated_at = ?
            WHERE id = ? AND target_id = ? AND status = 'RUNNING' AND fence_token = ?
            """,
            (
                ASSESSMENT_NODE_RESULT_SCHEMA_VERSION,
                payload_json,
                payload_sha256,
                input_schema,
                input_sha256,
                completed_at,
                completed_at,
                attempt_id,
                target_id,
                fence_token,
            ),
        )
        if attempt_cursor.rowcount != 1:
            raise RuntimeError("analysis_node_attempt_stale")
        target_cursor = conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'PUBLISHING', retryable = 0, error_code = '',
                error_message = '', updated_at = ?
            WHERE id = ? AND job_id = ? AND status = 'RUNNING'
              AND latest_attempt_id = ?
            """,
            (completed_at, target_id, job_id, attempt_id),
        )
        if target_cursor.rowcount != 1:
            raise RuntimeError("analysis_node_attempt_stale")
        return {
            "attempt_id": attempt_id,
            "target_id": target_id,
            "result_sha256": payload_sha256,
            "status": "PUBLISHING",
        }


def publish_staged_analysis_node_result(
    *,
    job_id: str,
    target_id: str,
    attempt_id: str,
    fence_token: str,
) -> dict[str, Any]:
    """Publish one staged result and its target status in one transaction."""

    published_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        target = conn.execute(
            """
            SELECT * FROM ai_analysis_job_targets
            WHERE id = ? AND job_id = ?
            """,
            (target_id, job_id),
        ).fetchone()
        attempt = conn.execute(
            """
            SELECT * FROM ai_analysis_node_attempts
            WHERE id = ? AND target_id = ? AND job_id = ?
            """,
            (attempt_id, target_id, job_id),
        ).fetchone()
        if job is None or target is None or attempt is None:
            raise LookupError("analysis_node_publication_not_found")
        if (
            str(job["publication_mode"] or "").strip().upper()
            != PER_NODE_RECOVERABLE_PUBLICATION_MODE
        ):
            raise RuntimeError("analysis_job_not_per_node")
        # A duplicate delivery may arrive after the final target has already
        # completed the job.  Returning the committed publication keeps the
        # serving write idempotent without reopening a terminal job.
        if (
            str(target["status"] or "").strip().upper() == "SUCCEEDED"
            and str(target["published_attempt_id"] or "") == attempt_id
            and str(attempt["fence_token"] or "") == str(fence_token or "")
        ):
            return {
                "target": _row_to_dict(target),
                "job": _row_to_dict(job),
                "replayed": True,
            }
        if str(job["status"] or "").strip().upper() != "RUNNING":
            raise RuntimeError("analysis_node_publication_cancelled")
        if (
            str(target["status"] or "").strip().upper() != "PUBLISHING"
            or str(target["latest_attempt_id"] or "") != attempt_id
            or str(attempt["status"] or "").strip().upper() != "SUCCEEDED"
            or str(attempt["fence_token"] or "") != str(fence_token or "")
        ):
            raise RuntimeError("analysis_node_publication_stale")

        payload_json = str(attempt["result_payload_json"] or "").strip()
        payload_sha256 = str(attempt["result_sha256"] or "").strip()
        payload = _parse_node_result_payload(
            payload_json,
            expected_sha256=payload_sha256,
            expected_schema=str(attempt["result_schema_version"] or ""),
        )
        result = payload["result"]
        if not isinstance(result, dict):
            raise RuntimeError("analysis_node_result_invalid")

        document_id = str(job["sor_document_id"] or "")
        project_id = str(job["project_id"] or "")
        requirement_set_id = str(job["requirement_set_id"] or "")
        requirement_id = str(target["requirement_id"] or "")
        node_id = str(target["node_id"] or "")
        expected_set_version = max(int(job["requirement_set_version"] or 0), 0)
        expected_knowledge_version = max(int(job["knowledge_base_version"] or 0), 0)
        expected_tree_sha256 = str(job["requirement_tree_sha256"] or "").strip()

        if not db._lock_requirement_set_document_in_conn(
            conn,
            project_id=project_id,
            sor_document_id=document_id,
        ):
            raise RuntimeError(
                "AI analysis input snapshot was superseded before item publication."
            )
        lock_requirement_matrix_sets_in_connection(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_ids=(requirement_set_id,),
        )
        snapshot = conn.execute(
            """
            SELECT d.active_requirement_set_id, d.requirement_set_version,
                   d.ai_assessed_at, d.ai_requirement_set_version,
                   d.ai_knowledge_base_version, p.knowledge_base_version,
                   rs.status AS requirement_set_status,
                   rs.is_active AS requirement_set_is_active,
                   rs.version AS persisted_set_version,
                   rs.requirement_tree_sha256 AS persisted_tree_sha256
            FROM sor_documents d
            INNER JOIN projects p ON p.id = d.project_id
            INNER JOIN requirement_sets rs ON rs.id = ?
            WHERE d.id = ? AND d.project_id = ?
            """,
            (requirement_set_id, document_id, project_id),
        ).fetchone()
        if (
            snapshot is None
            or str(snapshot["active_requirement_set_id"] or "")
            != requirement_set_id
            or int(snapshot["requirement_set_version"] or 0)
            != expected_set_version
            or int(snapshot["persisted_set_version"] or 0)
            != expected_set_version
            or str(snapshot["requirement_set_status"] or "").upper() != "LOCKED"
            or not bool(int(snapshot["requirement_set_is_active"] or 0))
            or str(snapshot["persisted_tree_sha256"] or "").strip()
            != expected_tree_sha256
            or int(snapshot["knowledge_base_version"] or 0)
            != expected_knowledge_version
        ):
            raise RuntimeError(
                "AI analysis input snapshot was superseded before item publication."
            )

        frozen_pairs = _analysis_job_target_pairs(job)
        source_order = max(int(target["source_order"] or 0), 0)
        if (
            source_order >= len(frozen_pairs)
            or frozen_pairs[source_order] != (node_id, requirement_id)
        ):
            raise RuntimeError(
                "AI analysis item no longer matches the frozen scope order."
            )
        membership = conn.execute(
            """
            SELECT 1 FROM requirements
            WHERE id = ? AND sor_document_id = ? AND requirement_set_id = ?
            """,
            (requirement_id, document_id, requirement_set_id),
        ).fetchone()
        if membership is None:
            raise RuntimeError(
                "AI analysis item no longer belongs to the frozen Requirement Set."
            )

        input_schema = str(attempt["analysis_input_schema_version"] or "").strip()
        input_sha256 = str(attempt["analysis_input_sha256"] or "").strip()
        _validate_analysis_input_provenance(input_schema, input_sha256)
        _persist_result(
            conn,
            requirement_id,
            result,
            profile_id=str(job["profile_id"] or ""),
            provider=str(job["provider"] or ""),
            retrieval_backend=str(job["retrieval_backend"] or ""),
            analysis_input_schema_version=input_schema,
            analysis_input_sha256=input_sha256,
            published_at=published_at,
        )
        _persist_assessment_runs_for_attempt(
            conn,
            requirement_id=requirement_id,
            attempt_id=attempt_id,
            runs=result.get("runs") or [],
            published_at=published_at,
        )
        target_cursor = conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'SUCCEEDED', published_attempt_id = ?, retryable = 0,
                error_code = '', error_message = '', updated_at = ?
            WHERE id = ? AND job_id = ? AND status = 'PUBLISHING'
              AND latest_attempt_id = ?
            """,
            (attempt_id, published_at, target_id, job_id, attempt_id),
        )
        if target_cursor.rowcount != 1:
            raise RuntimeError("analysis_node_publication_stale")

        counts = _analysis_target_counts(conn, job_id)
        outcome_counts = _analysis_outcome_counts(conn, job_id)
        current_running = _first_running_target(conn, job_id)
        conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET completed_requirements = ?, published_requirements = ?,
                analyzed_requirements = ?, info_requirements = ?,
                screened_requirements = ?, current_requirement_id = ?,
                current_requirement_code = ?, current_requirement_index = ?,
                current_stage = 'assessment', last_message = ?, error_message = '',
                updated_at = ?
            WHERE id = ? AND status = 'RUNNING'
            """,
            (
                counts["computed"],
                counts["succeeded"],
                outcome_counts["analyzed"],
                outcome_counts["info"],
                outcome_counts["screened"],
                str((current_running or {}).get("requirement_id") or ""),
                str((current_running or {}).get("requirement_code") or ""),
                max(int((current_running or {}).get("source_order") or 0), 0) + 1
                if current_running
                else source_order + 1,
                (
                    f"已完成并保留 {str(target['requirement_code'] or requirement_id)}；"
                    f"成功 {counts['succeeded']}/{counts['total']}，失败 {counts['failed']}。"
                ),
                published_at,
                job_id,
            ),
        )

        universe_ids = [
            item_requirement_id
            for _item_node_id, item_requirement_id in _analysis_job_universe_pairs(job)
        ]
        projection_ready = _incremental_projection_ready(
            conn,
            snapshot=snapshot,
            requirement_set_id=requirement_set_id,
            requirement_set_version=expected_set_version,
            requirement_tree_sha256=expected_tree_sha256,
            knowledge_base_version=expected_knowledge_version,
            universe_requirement_ids=universe_ids,
        )
        conn.execute(
            """
            UPDATE sor_documents
            SET ai_status = ?,
                ai_assessed_at = CASE WHEN ? THEN ? ELSE ai_assessed_at END,
                ai_requirement_set_version = CASE WHEN ? THEN ? ELSE ai_requirement_set_version END,
                ai_knowledge_base_version = CASE WHEN ? THEN ? ELSE ai_knowledge_base_version END,
                workflow_stage = ?, updated_at = ?
            WHERE id = ? AND active_requirement_set_id = ?
              AND requirement_set_version = ?
            """,
            (
                "READY" if projection_ready else "STALE",
                bool(projection_ready),
                published_at,
                bool(projection_ready),
                expected_set_version,
                bool(projection_ready),
                expected_knowledge_version,
                "AI_READY" if projection_ready else "EXTRACTION_LOCKED",
                published_at,
                document_id,
                requirement_set_id,
                expected_set_version,
            ),
        )
        event_message = (
            f"完成并保留 {str(target['requirement_code'] or requirement_id)}；"
            f"本次已发布 {counts['succeeded']}/{counts['total']}。"
        )
        conn.execute(
            """
            INSERT INTO ai_analysis_events
            (id, job_id, sor_document_id, profile_id, provider, event_type, stage,
             message, requirement_id, requirement_code, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, 'requirement_completed', 'assessment', ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                job_id,
                document_id,
                str(job["profile_id"] or ""),
                str(job["provider"] or ""),
                event_message,
                requirement_id,
                str(target["requirement_code"] or ""),
                _canonical_json(
                    {
                        "attempt_id": attempt_id,
                        "result_sha256": payload_sha256,
                        "published": True,
                        "published_requirements": counts["succeeded"],
                        "total_requirements": counts["total"],
                    }
                ),
                published_at,
            ),
        )
        refreshed_target = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        refreshed_job = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return {
            "target": _row_to_dict(refreshed_target),
            "job": _row_to_dict(refreshed_job),
            "counts": counts,
            "projection_ready": projection_ready,
            "replayed": False,
        }


def fail_analysis_node_attempt(
    *,
    job_id: str,
    target_id: str,
    attempt_id: str,
    fence_token: str,
    error_code: str,
    error_message: str,
    retryable: bool,
) -> dict[str, Any]:
    """Fence one failed provider attempt without affecting peer targets."""

    failed_at = datetime.now(timezone.utc).isoformat()
    safe_code = _safe_error_code(error_code)
    safe_message = _safe_error_message(error_message)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ? AND job_id = ?",
            (target_id, job_id),
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM ai_analysis_node_attempts WHERE id = ? AND target_id = ?",
            (attempt_id, target_id),
        ).fetchone()
        if target is None or attempt is None:
            raise LookupError("analysis_node_attempt_not_found")
        if (
            str(target["status"] or "").upper() == "FAILED"
            and str(target["latest_attempt_id"] or "") == attempt_id
            and str(attempt["fence_token"] or "") == str(fence_token or "")
        ):
            return _row_to_dict(target)
        if (
            str(target["status"] or "").upper() != "RUNNING"
            or str(target["latest_attempt_id"] or "") != attempt_id
            or str(attempt["status"] or "").upper() != "RUNNING"
            or str(attempt["fence_token"] or "") != str(fence_token or "")
        ):
            raise RuntimeError("analysis_node_attempt_stale")
        conn.execute(
            """
            UPDATE ai_analysis_node_attempts
            SET status = 'FAILED', error_code = ?, error_message = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND fence_token = ?
            """,
            (safe_code, safe_message, failed_at, failed_at, attempt_id, fence_token),
        )
        conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'FAILED', retryable = ?, error_code = ?,
                error_message = ?, updated_at = ?
            WHERE id = ? AND job_id = ? AND status = 'RUNNING'
              AND latest_attempt_id = ?
            """,
            (
                1 if retryable else 0,
                safe_code,
                safe_message,
                failed_at,
                target_id,
                job_id,
                attempt_id,
            ),
        )
        _refresh_incremental_job_counts(conn, job_id, updated_at=failed_at)
        job = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO ai_analysis_events
            (id, job_id, sor_document_id, profile_id, provider, event_type, stage,
             message, requirement_id, requirement_code, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, 'requirement_failed', 'failed', ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                job_id,
                str(job["sor_document_id"] or ""),
                str(job["profile_id"] or ""),
                str(job["provider"] or ""),
                safe_message,
                str(target["requirement_id"] or ""),
                str(target["requirement_code"] or ""),
                _canonical_json(
                    {
                        "attempt_id": attempt_id,
                        "error_code": safe_code,
                        "retryable": bool(retryable),
                        "published": False,
                    }
                ),
                failed_at,
            ),
        )
        refreshed = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        return _row_to_dict(refreshed)


def fail_analysis_node_publication(
    *,
    job_id: str,
    target_id: str,
    attempt_id: str,
    fence_token: str,
    error_code: str,
    error_message: str,
    retryable: bool,
) -> dict[str, Any]:
    """Close a staged result whose serving publication could not commit."""

    failed_at = datetime.now(timezone.utc).isoformat()
    safe_code = _safe_error_code(error_code)
    safe_message = _safe_error_message(error_message)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ? AND job_id = ?",
            (target_id, job_id),
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM ai_analysis_node_attempts WHERE id = ? AND target_id = ?",
            (attempt_id, target_id),
        ).fetchone()
        if target is None or attempt is None:
            raise LookupError("analysis_node_publication_not_found")
        if (
            str(target["status"] or "").upper() == "FAILED"
            and str(target["latest_attempt_id"] or "") == attempt_id
        ):
            return _row_to_dict(target)
        if (
            str(target["status"] or "").upper() != "PUBLISHING"
            or str(target["latest_attempt_id"] or "") != attempt_id
            or str(attempt["status"] or "").upper() != "SUCCEEDED"
            or str(attempt["fence_token"] or "") != str(fence_token or "")
        ):
            raise RuntimeError("analysis_node_publication_stale")
        conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'FAILED', retryable = ?, error_code = ?,
                error_message = ?, updated_at = ?
            WHERE id = ? AND job_id = ? AND status = 'PUBLISHING'
              AND latest_attempt_id = ?
            """,
            (
                1 if retryable else 0,
                safe_code,
                safe_message,
                failed_at,
                target_id,
                job_id,
                attempt_id,
            ),
        )
        _refresh_incremental_job_counts(conn, job_id, updated_at=failed_at)
        refreshed = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        return _row_to_dict(refreshed)


def cancel_analysis_node_attempt(
    *,
    job_id: str,
    target_id: str,
    attempt_id: str,
    fence_token: str,
) -> dict[str, Any]:
    """Cancel one in-flight attempt if publication has not won the race."""

    cancelled_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ? AND job_id = ?",
            (target_id, job_id),
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM ai_analysis_node_attempts WHERE id = ? AND target_id = ?",
            (attempt_id, target_id),
        ).fetchone()
        if target is None or attempt is None:
            raise LookupError("analysis_node_attempt_not_found")
        if str(target["status"] or "").upper() in {"SUCCEEDED", "CANCELLED"}:
            return _row_to_dict(target)
        if (
            str(target["status"] or "").upper() not in {"RUNNING", "PUBLISHING"}
            or str(target["latest_attempt_id"] or "") != attempt_id
            or str(attempt["fence_token"] or "") != str(fence_token or "")
        ):
            raise RuntimeError("analysis_node_attempt_stale")
        conn.execute(
            """
            UPDATE ai_analysis_node_attempts
            SET status = 'CANCELLED', error_code = 'analysis_cancelled',
                error_message = 'This item was stopped before publication.',
                finished_at = CASE WHEN finished_at = '' THEN ? ELSE finished_at END,
                updated_at = ?
            WHERE id = ? AND status IN ('RUNNING', 'SUCCEEDED') AND fence_token = ?
            """,
            (cancelled_at, cancelled_at, attempt_id, fence_token),
        )
        conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'CANCELLED', retryable = 0,
                error_code = 'analysis_cancelled',
                error_message = 'This item was stopped before publication.',
                updated_at = ?
            WHERE id = ? AND job_id = ? AND status IN ('RUNNING', 'PUBLISHING')
              AND latest_attempt_id = ?
            """,
            (cancelled_at, target_id, job_id, attempt_id),
        )
        _refresh_incremental_job_counts(conn, job_id, updated_at=cancelled_at)
        refreshed = conn.execute(
            "SELECT * FROM ai_analysis_job_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        return _row_to_dict(refreshed)


def cancel_waiting_analysis_targets(job_id: str) -> list[dict[str, Any]]:
    """Stop unclaimed targets after the job cancel request commits."""

    cancelled_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT status FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError("analysis_job_not_found")
        if str(job["status"] or "").upper() not in {
            "CANCEL_REQUESTED",
            "CANCELLED",
        }:
            return _target_rows_in_connection(conn, job_id)
        conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = 'CANCELLED', retryable = 0,
                error_code = 'analysis_cancelled',
                error_message = 'This item was stopped before it started.',
                updated_at = ?
            WHERE job_id = ? AND status = 'WAITING'
            """,
            (cancelled_at, job_id),
        )
        _refresh_incremental_job_counts(conn, job_id, updated_at=cancelled_at)
        return _target_rows_in_connection(conn, job_id)


def abandon_incremental_analysis_job(
    job_id: str,
    *,
    cancelled: bool,
) -> dict[str, Any]:
    """Close an orphaned delivery while preserving already published items."""

    observed_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError("analysis_job_not_found")
        if (
            str(job["publication_mode"] or "").strip().upper()
            != PER_NODE_RECOVERABLE_PUBLICATION_MODE
        ):
            return _row_to_dict(job)
        target_status = "CANCELLED" if cancelled else "FAILED"
        attempt_status = target_status
        error_code = "analysis_cancelled" if cancelled else "analysis_worker_interrupted"
        error_message = (
            "This item was stopped before publication."
            if cancelled
            else "The analysis worker stopped before this item completed."
        )
        conn.execute(
            """
            UPDATE ai_analysis_node_attempts
            SET status = ?, error_code = ?, error_message = ?,
                finished_at = CASE WHEN finished_at = '' THEN ? ELSE finished_at END,
                updated_at = ?
            WHERE job_id = ? AND status IN ('RUNNING', 'SUCCEEDED')
              AND id NOT IN (
                  SELECT published_attempt_id FROM ai_analysis_job_targets
                  WHERE job_id = ? AND published_attempt_id <> ''
              )
            """,
            (
                attempt_status,
                error_code,
                error_message,
                observed_at,
                observed_at,
                job_id,
                job_id,
            ),
        )
        conn.execute(
            """
            UPDATE ai_analysis_job_targets
            SET status = ?, retryable = ?, error_code = ?, error_message = ?,
                updated_at = ?
            WHERE job_id = ? AND status IN ('WAITING', 'RUNNING', 'PUBLISHING')
            """,
            (
                target_status,
                0 if cancelled else 1,
                error_code,
                error_message,
                observed_at,
                job_id,
            ),
        )
        if cancelled:
            conn.execute(
                """
                UPDATE ai_analysis_jobs
                SET status = 'CANCEL_REQUESTED', current_stage = 'cancelling',
                    updated_at = ?
                WHERE id = ? AND status IN ('PENDING', 'RUNNING')
                """,
                (observed_at, job_id),
            )
        _refresh_incremental_job_counts(conn, job_id, updated_at=observed_at)
    return finalize_incremental_analysis_job(job_id)


def finalize_incremental_analysis_job(job_id: str) -> dict[str, Any]:
    """Derive the terminal job state from its durable target ledger."""

    finished_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError("analysis_job_not_found")
        prior_status = str(job["status"] or "").strip().upper()
        if prior_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return _row_to_dict(job)
        if prior_status == "CANCEL_REQUESTED":
            conn.execute(
                """
                UPDATE ai_analysis_job_targets
                SET status = 'CANCELLED', retryable = 0,
                    error_code = 'analysis_cancelled',
                    error_message = 'This item was stopped before publication.',
                    updated_at = ?
                WHERE job_id = ? AND status IN ('WAITING', 'RUNNING', 'PUBLISHING')
                """,
                (finished_at, job_id),
            )
            conn.execute(
                """
                UPDATE ai_analysis_node_attempts
                SET status = 'CANCELLED', error_code = 'analysis_cancelled',
                    error_message = 'This item was stopped before publication.',
                    finished_at = CASE WHEN finished_at = '' THEN ? ELSE finished_at END,
                    updated_at = ?
                WHERE job_id = ? AND status IN ('RUNNING', 'SUCCEEDED')
                  AND id NOT IN (
                      SELECT published_attempt_id FROM ai_analysis_job_targets
                      WHERE job_id = ? AND published_attempt_id <> ''
                  )
                """,
                (finished_at, finished_at, job_id, job_id),
            )
        counts = _analysis_target_counts(conn, job_id)
        unfinished = counts["waiting"] + counts["running"] + counts["publishing"]
        if unfinished:
            raise RuntimeError("analysis_job_targets_not_terminal")
        if prior_status == "CANCEL_REQUESTED" or counts["cancelled"]:
            terminal_status = "CANCELLED"
            event_type = "job_cancelled"
            stage = "cancelled"
            message = (
                f"AI 分析已停止：成功保留 {counts['succeeded']} 条，"
                f"失败 {counts['failed']} 条，停止 {counts['cancelled']} 条。"
            )
        elif counts["failed"]:
            terminal_status = "FAILED"
            event_type = "job_failed"
            stage = "failed"
            message = (
                f"AI 分析完成，其中 {counts['failed']} 条失败；"
                f"已成功保留 {counts['succeeded']} 条，可仅重试失败项。"
            )
        else:
            terminal_status = "COMPLETED"
            event_type = "job_completed"
            stage = "completed"
            message = f"AI 分析完成，已成功保留 {counts['succeeded']} 条。"
        outcome_counts = _analysis_outcome_counts(conn, job_id)
        conn.execute(
            """
            UPDATE ai_analysis_jobs
            SET status = ?, completed_requirements = ?, published_requirements = ?,
                analyzed_requirements = ?, info_requirements = ?,
                screened_requirements = ?, current_requirement_id = '',
                current_requirement_code = '', current_requirement_index = ?,
                current_stage = ?, last_message = ?,
                error_message = CASE WHEN ? = 'FAILED' THEN ? ELSE '' END,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
            """,
            (
                terminal_status,
                counts["computed"],
                counts["succeeded"],
                outcome_counts["analyzed"],
                outcome_counts["info"],
                outcome_counts["screened"],
                counts["total"],
                stage,
                message,
                terminal_status,
                message if terminal_status == "FAILED" else "",
                finished_at,
                finished_at,
                job_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO ai_analysis_events
            (id, job_id, sor_document_id, profile_id, provider, event_type, stage,
             message, requirement_id, requirement_code, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?)
            """,
            (
                str(uuid.uuid4()),
                job_id,
                str(job["sor_document_id"] or ""),
                str(job["profile_id"] or ""),
                str(job["provider"] or ""),
                event_type,
                stage,
                message,
                _canonical_json(
                    {
                        "outcome": terminal_status,
                        "published": bool(counts["succeeded"]),
                        "publication_mode": PER_NODE_RECOVERABLE_PUBLICATION_MODE,
                        "execution_counts": counts,
                        "analyzed_requirements": outcome_counts["analyzed"],
                        "info_requirements": outcome_counts["info"],
                        "screened_requirements": outcome_counts["screened"],
                    }
                ),
                finished_at,
            ),
        )
        refreshed = conn.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if refreshed is None:
            raise RuntimeError("analysis_job_disappeared")
        return _row_to_dict(refreshed)


def persist_serving_result(
    requirement_id: str,
    result: dict[str, Any],
    *,
    profile_id: str,
    provider: str,
    retrieval_backend: str,
    analysis_input_schema_version: str = "",
    analysis_input_sha256: str = "",
) -> None:
    published_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _lock_single_result_draft(conn, requirement_id=requirement_id)
        _persist_result(
            conn,
            requirement_id,
            result,
            profile_id=profile_id,
            provider=provider,
            retrieval_backend=retrieval_backend,
            analysis_input_schema_version=analysis_input_schema_version,
            analysis_input_sha256=analysis_input_sha256,
            published_at=published_at,
        )


def publish_serving_results(
    pending_results: list[tuple[str, dict[str, Any]]],
    *,
    document_id: str,
    requirement_set_id: str = "",
    requirement_set_version: int,
    knowledge_base_version: int,
    profile_id: str,
    provider: str,
    retrieval_backend: str,
    analysis_input_schema_version: str = "",
    analysis_input_sha256_by_requirement_id: dict[str, str] | None = None,
    scope_sha256: str = "",
    requirement_tree_sha256: str = "",
    pending_target_pairs: list[tuple[str, str]] | None = None,
    all_analysis_target_requirement_ids: list[str] | None = None,
    job_completion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    published_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        scope = conn.execute(
            "SELECT project_id FROM sor_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if scope is None or not db._lock_requirement_set_document_in_conn(
            conn,
            project_id=str(scope["project_id"] or ""),
            sor_document_id=document_id,
        ):
            raise RuntimeError(
                f"SOR document {document_id} disappeared during AI result publication."
            )
        snapshot = conn.execute(
            """
            SELECT d.project_id, d.active_requirement_set_id, d.requirement_set_version,
                   d.ai_assessed_at, d.ai_requirement_set_version,
                   d.ai_knowledge_base_version, p.knowledge_base_version
            FROM sor_documents d
            INNER JOIN projects p ON p.id = d.project_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if snapshot is None:
            raise RuntimeError(f"SOR document {document_id} disappeared during AI result publication.")
        current_requirement_version = max(int(snapshot["requirement_set_version"] or 0), 0)
        current_knowledge_version = max(int(snapshot["knowledge_base_version"] or 0), 0)
        expected_requirement_version = max(int(requirement_set_version or 0), 0)
        expected_knowledge_version = max(int(knowledge_base_version or 0), 0)
        expected_requirement_set_id = str(requirement_set_id or "").strip()
        completion_job_id = str((job_completion or {}).get("job_id") or "").strip()
        completion_job: Any | None = None
        frozen_global_target_ids: list[str] | None = None
        if completion_job_id:
            completion_job = conn.execute(
                """
                SELECT * FROM ai_analysis_jobs
                WHERE id = ? AND sor_document_id = ?
                """,
                (completion_job_id, document_id),
            ).fetchone()
            if completion_job is None or str(completion_job["status"] or "").upper() != "RUNNING":
                raise RuntimeError(
                    f"AI analysis job {completion_job_id} was superseded before publication."
                )
            job_requirement_set_id = str(completion_job["requirement_set_id"] or "")
            if expected_requirement_set_id and expected_requirement_set_id != job_requirement_set_id:
                raise RuntimeError(
                    "AI analysis input snapshot was superseded before publication "
                    "(job Requirement Set lineage changed)."
                )
            expected_requirement_set_id = job_requirement_set_id
            job_knowledge_version = max(int(completion_job["knowledge_base_version"] or 0), 0)
            if job_knowledge_version != expected_knowledge_version:
                raise RuntimeError(
                    "AI analysis input snapshot was superseded before publication "
                    f"(job knowledge {job_knowledge_version}, worker knowledge {expected_knowledge_version})."
                )
            execution_identity = {
                "profile_id": str(profile_id or "").strip(),
                "provider": str(provider or "").strip(),
                "retrieval_backend": str(retrieval_backend or "").strip(),
            }
            identity_mismatches = [
                field
                for field, value in execution_identity.items()
                if str(completion_job[field] or "").strip() != value
            ]
            if identity_mismatches:
                raise RuntimeError(
                    "AI analysis execution identity changed before publication "
                    f"({', '.join(identity_mismatches)})."
                )
            job_scope_sha256 = str(completion_job["scope_sha256"] or "").strip()
            expected_scope_sha256 = str(scope_sha256 or "").strip()
            if job_scope_sha256:
                if not expected_scope_sha256 or job_scope_sha256 != expected_scope_sha256:
                    raise RuntimeError(
                        "AI analysis scope changed before publication (scope SHA-256 mismatch)."
                    )
                frozen_pairs = _analysis_job_target_pairs(completion_job)
                supplied_pairs = tuple(
                    (
                        str(node_id or "").strip(),
                        str(requirement_id or "").strip(),
                    )
                    for node_id, requirement_id in (pending_target_pairs or [])
                )
                if supplied_pairs != frozen_pairs:
                    raise RuntimeError(
                        "AI analysis pending results do not exactly match the claimed node scope."
                    )
                frozen_universe_pairs = _analysis_job_universe_pairs(
                    completion_job
                )
                universe_positions = {
                    pair: index for index, pair in enumerate(frozen_universe_pairs)
                }
                if any(
                    pair not in universe_positions for pair in frozen_pairs
                ) or tuple(
                    sorted(frozen_pairs, key=universe_positions.get)
                ) != frozen_pairs:
                    raise RuntimeError(
                        "AI analysis target scope is not an ordered subset of its frozen Matrix universe."
                    )
                frozen_global_target_ids = [
                    requirement_id
                    for _node_id, requirement_id in frozen_universe_pairs
                ]
                supplied_global_target_ids = [
                    str(requirement_id or "").strip()
                    for requirement_id in (
                        all_analysis_target_requirement_ids or []
                    )
                ]
                if supplied_global_target_ids != frozen_global_target_ids:
                    raise RuntimeError(
                        "AI analysis target universe does not exactly match the frozen Matrix scope."
                    )
                if (
                    int(completion_job["requirement_set_version"] or 0)
                    != expected_requirement_version
                    or str(
                        completion_job["requirement_tree_sha256"] or ""
                    ).strip()
                    != str(requirement_tree_sha256 or "").strip()
                    or str(completion_job["publication_mode"] or "").strip().upper()
                    != "ATOMIC_BATCH"
                ):
                    raise RuntimeError(
                        "AI analysis scope lineage changed before publication."
                    )
        if not expected_requirement_set_id:
            expected_requirement_set_id = str(snapshot["active_requirement_set_id"] or "")
        lock_requirement_matrix_sets_in_connection(
            conn,
            project_id=str(snapshot["project_id"] or ""),
            document_id=document_id,
            requirement_set_ids=(expected_requirement_set_id,),
        )
        target_set = conn.execute(
            """
            SELECT version, status, is_active, requirement_tree_sha256
            FROM requirement_sets
            WHERE id = ? AND sor_document_id = ?
            """,
            (expected_requirement_set_id, document_id),
        ).fetchone()
        if target_set is None or int(target_set["version"] or 0) != expected_requirement_version:
            raise RuntimeError(
                "AI analysis input snapshot was superseded before publication "
                "(Requirement Set identity/version changed)."
            )
        expected_tree_sha256 = str(requirement_tree_sha256 or "").strip()
        persisted_tree_sha256 = str(
            target_set["requirement_tree_sha256"] or ""
        ).strip()
        if (
            expected_tree_sha256
            and persisted_tree_sha256
            and persisted_tree_sha256 != expected_tree_sha256
        ):
            raise RuntimeError(
                "AI analysis input snapshot was superseded before publication "
                "(Requirement Tree identity changed)."
            )
        if (
            str(target_set["status"] or "").strip().upper() != "LOCKED"
            or not bool(int(target_set["is_active"] or 0))
        ):
            raise RuntimeError(
                "AI analysis input snapshot was superseded before publication "
                "(Requirement Set is no longer the active LOCKED analysis set)."
            )
        if (
            str(snapshot["active_requirement_set_id"] or "") != expected_requirement_set_id
            or current_requirement_version != expected_requirement_version
            or current_knowledge_version != expected_knowledge_version
        ):
            raise RuntimeError(
                "AI analysis input snapshot was superseded before publication "
                f"(requirements {expected_requirement_version}->{current_requirement_version}, "
                f"knowledge {expected_knowledge_version}->{current_knowledge_version})."
            )
        pending_ids = [str(requirement_id or "").strip() for requirement_id, _result in pending_results]
        if not pending_ids or any(not item for item in pending_ids) or len(pending_ids) != len(set(pending_ids)):
            raise RuntimeError("AI analysis pending results have invalid Requirement Set membership.")
        placeholders = ", ".join("?" for _ in pending_ids)
        membership_count = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM requirements
            WHERE id IN ({placeholders})
              AND sor_document_id = ?
              AND requirement_set_id = ?
            """,
            [*pending_ids, document_id, expected_requirement_set_id],
        ).fetchone()[0]
        if int(membership_count or 0) != len(pending_ids):
            raise RuntimeError(
                "AI analysis input snapshot was superseded before publication "
                "(pending requirements no longer belong to the job Requirement Set)."
            )
        if completion_job is not None and str(completion_job["scope_sha256"] or "").strip():
            frozen_requirement_ids = [
                requirement_id
                for _node_id, requirement_id in _analysis_job_target_pairs(
                    completion_job
                )
            ]
            if pending_ids != frozen_requirement_ids:
                raise RuntimeError(
                    "AI analysis pending results do not exactly match the claimed requirement scope."
                )
        input_schema = str(analysis_input_schema_version or "").strip()
        input_hashes = {
            str(key or "").strip(): str(value or "").strip()
            for key, value in (
                analysis_input_sha256_by_requirement_id or {}
            ).items()
        }
        if bool(input_schema) != bool(input_hashes) or (
            input_hashes and set(input_hashes) != set(pending_ids)
        ):
            raise RuntimeError(
                "AI semantic-context provenance is incomplete for atomic publication."
            )
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in input_hashes.values()
        ):
            raise RuntimeError("AI semantic-context provenance hash is invalid.")
        for requirement_id, result in pending_results:
            _persist_result(
                conn,
                requirement_id,
                result,
                profile_id=profile_id,
                provider=provider,
                retrieval_backend=retrieval_backend,
                analysis_input_schema_version=input_schema,
                analysis_input_sha256=input_hashes.get(requirement_id, ""),
                published_at=published_at,
            )
        global_target_ids = frozen_global_target_ids or [
            str(requirement_id or "").strip()
            for requirement_id in (all_analysis_target_requirement_ids or [])
        ]
        global_targets_valid = bool(
            global_target_ids
            and all(global_target_ids)
            and len(global_target_ids) == len(set(global_target_ids))
        )
        if global_targets_valid:
            global_placeholders = ", ".join("?" for _ in global_target_ids)
            global_membership = conn.execute(
                f"""
                SELECT COUNT(*) FROM requirements
                WHERE id IN ({global_placeholders})
                  AND sor_document_id = ?
                  AND requirement_set_id = ?
                """,
                [*global_target_ids, document_id, expected_requirement_set_id],
            ).fetchone()[0]
            global_targets_valid = int(global_membership or 0) == len(
                global_target_ids
            )
        if global_targets_valid:
            current_placeholders = ", ".join("?" for _ in global_target_ids)
            current_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM assessments a
                WHERE a.requirement_id IN ({current_placeholders})
                  AND a.reference_status = 'CURRENT'
                """,
                global_target_ids,
            ).fetchone()[0]
            all_targets_current = int(current_count or 0) == len(global_target_ids)
            prior_projection_current = bool(
                str(snapshot["ai_assessed_at"] or "").strip()
                and int(snapshot["ai_requirement_set_version"] or 0)
                == expected_requirement_version
                and int(snapshot["ai_knowledge_base_version"] or 0)
                == expected_knowledge_version
            )
            current_batch_covers_all = pending_ids == global_target_ids
            # A partial batch can preserve or complete a projection whose
            # document-level set/knowledge provenance is already current.  It
            # must not upgrade results from an older knowledge snapshot merely
            # because those assessment rows still say CURRENT.  Until W6-B
            # persists per-node knowledge provenance, only a full current batch
            # may establish a fresh whole-document READY projection.
            projection_ready = bool(
                all_targets_current
                and (prior_projection_current or current_batch_covers_all)
            )
        else:
            # Legacy whole-set calls predate frozen scopes.  Their pending list
            # was the complete Matrix target and retains the previous behavior.
            projection_ready = completion_job is None or not str(
                completion_job["scope_sha256"] or ""
            ).strip()
        cursor = conn.execute(
            """
            UPDATE sor_documents
            SET ai_status = ?,
                ai_assessed_at = CASE WHEN ? THEN ? ELSE ai_assessed_at END,
                ai_requirement_set_version = CASE WHEN ? THEN ? ELSE ai_requirement_set_version END,
                ai_knowledge_base_version = CASE WHEN ? THEN ? ELSE ai_knowledge_base_version END,
                workflow_stage = ?,
                updated_at = ?
            WHERE id = ? AND active_requirement_set_id = ? AND requirement_set_version = ?
            """,
            (
                "READY" if projection_ready else "STALE",
                bool(projection_ready),
                published_at,
                bool(projection_ready),
                max(int(requirement_set_version or 0), 0),
                bool(projection_ready),
                max(int(knowledge_base_version or 0), 0),
                "AI_READY" if projection_ready else "EXTRACTION_LOCKED",
                published_at,
                document_id,
                expected_requirement_set_id,
                expected_requirement_version,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"SOR document {document_id} disappeared during AI result publication.")
        row = conn.execute("SELECT * FROM sor_documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"SOR document {document_id} could not be reloaded after AI result publication.")
        updated_document = {key: row[key] for key in row.keys()}
        if job_completion:
            _complete_analysis_job(
                conn,
                document_id=document_id,
                profile_id=profile_id,
                provider=provider,
                completion=job_completion,
                requirement_set_id=expected_requirement_set_id,
                completed_at=published_at,
            )
    return updated_document


def _lock_single_result_draft(conn: Any, *, requirement_id: str) -> None:
    """Serialize legacy one-row generation with every Matrix/result mutation.

    Atomic analysis publication has its own LOCKED-set boundary above.  This
    one-row helper is intentionally limited to an inactive, never-activated
    DRAFT because it is used while a Requirement Set is still being generated.
    """

    scope = conn.execute(
        """
        SELECT r.project_id, r.sor_document_id, r.requirement_set_id
        FROM requirements r
        WHERE r.id = ?
        """,
        (requirement_id,),
    ).fetchone()
    if scope is None:
        raise LookupError("assessment_requirement_not_found")
    project_id = str(scope["project_id"] or "")
    document_id = str(scope["sor_document_id"] or "")
    requirement_set_id = str(scope["requirement_set_id"] or "")
    lock_requirement_matrix_sets_in_connection(
        conn,
        project_id=project_id,
        document_id=document_id,
        requirement_set_ids=(requirement_set_id,),
    )
    state = conn.execute(
        """
        SELECT rs.status, rs.is_active, rs.activated_at,
               d.active_requirement_set_id
        FROM requirements r
        INNER JOIN requirement_sets rs ON rs.id = r.requirement_set_id
        INNER JOIN sor_documents d ON d.id = r.sor_document_id
        WHERE r.id = ?
          AND r.project_id = ?
          AND r.sor_document_id = ?
          AND r.requirement_set_id = ?
        """,
        (requirement_id, project_id, document_id, requirement_set_id),
    ).fetchone()
    if state is None:
        raise LookupError("assessment_requirement_not_found")
    if (
        str(state["status"] or "").strip().upper() != "DRAFT"
        or bool(int(state["is_active"] or 0))
        or bool(str(state["activated_at"] or "").strip())
    ):
        raise PermissionError(
            "single assessment publication requires an inactive, "
            "never-activated DRAFT Requirement Set"
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _normalize_target_descriptors(
    descriptors: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(descriptors):
        node_id = str(item.get("node_id") or "").strip()
        requirement_id = str(item.get("requirement_id") or "").strip()
        requirement_code = str(
            item.get("requirement_code") or requirement_id
        ).strip()
        source_order = int(item.get("source_order", index))
        if not node_id or not requirement_id or source_order != index:
            raise ValueError("analysis_target_descriptor_invalid")
        normalized.append(
            {
                "node_id": node_id,
                "requirement_id": requirement_id,
                "requirement_code": requirement_code,
                "source_order": source_order,
            }
        )
    pairs = [(item["node_id"], item["requirement_id"]) for item in normalized]
    if (
        len(pairs) != len(set(pairs))
        or len({item[0] for item in pairs}) != len(pairs)
        or len({item[1] for item in pairs}) != len(pairs)
    ):
        raise ValueError("analysis_target_descriptor_invalid")
    return tuple(normalized)


def _scope_pairs_from_payload(
    payload_json: str,
    *,
    expected_sha256: str,
    field: str,
) -> tuple[tuple[str, str], ...]:
    raw = str(payload_json or "").strip()
    if not raw or hashlib.sha256(raw.encode("utf-8")).hexdigest() != str(
        expected_sha256 or ""
    ).strip():
        raise RuntimeError("AI analysis frozen scope failed integrity validation.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AI analysis frozen scope is invalid.") from exc
    if _canonical_json(payload) != raw:
        raise RuntimeError("AI analysis frozen scope is not canonical.")
    raw_pairs = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(raw_pairs, list):
        raise RuntimeError("AI analysis frozen scope has no target pairs.")
    pairs = tuple(
        (
            str(item.get("node_id") or "").strip(),
            str(item.get("requirement_id") or "").strip(),
        )
        for item in raw_pairs
        if isinstance(item, dict)
    )
    if len(pairs) != len(raw_pairs) or any(not all(pair) for pair in pairs):
        raise RuntimeError("AI analysis frozen scope target pairs are invalid.")
    return pairs


def _target_rows_in_connection(conn: Any, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM ai_analysis_job_targets
        WHERE job_id = ?
        ORDER BY source_order ASC
        """,
        (job_id,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _validate_analysis_input_provenance(schema: str, sha256: str) -> None:
    if not schema or not sha256:
        raise ValueError("analysis semantic-context provenance must be paired")
    if len(sha256) != 64 or any(
        character not in "0123456789abcdef" for character in sha256
    ):
        raise ValueError("analysis input SHA-256 is invalid")


def _parse_node_result_payload(
    payload_json: str,
    *,
    expected_sha256: str,
    expected_schema: str,
) -> dict[str, Any]:
    raw = str(payload_json or "").strip()
    if (
        expected_schema != ASSESSMENT_NODE_RESULT_SCHEMA_VERSION
        or not raw
        or hashlib.sha256(raw.encode("utf-8")).hexdigest()
        != str(expected_sha256 or "").strip()
    ):
        raise RuntimeError("analysis_node_result_integrity_failed")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("analysis_node_result_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ASSESSMENT_NODE_RESULT_SCHEMA_VERSION
        or not isinstance(payload.get("result"), dict)
        or _canonical_json(payload) != raw
    ):
        raise RuntimeError("analysis_node_result_invalid")
    return payload


def _persist_assessment_runs_for_attempt(
    conn: Any,
    *,
    requirement_id: str,
    attempt_id: str,
    runs: Any,
    published_at: str,
) -> None:
    if not isinstance(runs, list):
        return
    for run_index, run in enumerate(runs):
        if not isinstance(run, dict):
            continue
        conn.execute(
            """
            INSERT INTO assessment_runs
            (id, requirement_id, profile_id, provider, mode, retrieval_backend,
             model_name, prompt_version, input_payload_json, output_payload_json,
             validation_status, fallback_reason, analysis_node_attempt_id,
             run_index, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(analysis_node_attempt_id, run_index) WHERE analysis_node_attempt_id <> ''
            DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                requirement_id,
                str(run.get("profile_id") or ""),
                str(run.get("provider") or ""),
                str(run.get("mode") or ""),
                str(run.get("retrieval_backend") or ""),
                str(run.get("model_name") or ""),
                str(run.get("prompt_version") or ""),
                str(run.get("input_payload_json") or "{}"),
                str(run.get("output_payload_json") or ""),
                str(run.get("validation_status") or ""),
                str(run.get("fallback_reason") or ""),
                attempt_id,
                run_index,
                published_at,
            ),
        )


def _analysis_target_counts(conn: Any, job_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM ai_analysis_job_targets
        WHERE job_id = ?
        GROUP BY status
        """,
        (job_id,),
    ).fetchall()
    by_status = {
        str(row["status"] or "").strip().lower(): max(int(row["count"] or 0), 0)
        for row in rows
    }
    counts = {
        "waiting": by_status.get("waiting", 0),
        "running": by_status.get("running", 0),
        "publishing": by_status.get("publishing", 0),
        "succeeded": by_status.get("succeeded", 0),
        "failed": by_status.get("failed", 0),
        "cancelled": by_status.get("cancelled", 0),
    }
    counts["total"] = sum(counts.values())
    counts["computed"] = counts["publishing"] + counts["succeeded"]
    counts["settled"] = (
        counts["succeeded"] + counts["failed"] + counts["cancelled"]
    )
    retryable = conn.execute(
        """
        SELECT COUNT(*) FROM ai_analysis_job_targets
        WHERE job_id = ? AND status = 'FAILED' AND retryable = 1
        """,
        (job_id,),
    ).fetchone()[0]
    counts["retryable_failed"] = max(int(retryable or 0), 0)
    return counts


def _analysis_outcome_counts(conn: Any, job_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT a.result_payload_json
        FROM ai_analysis_job_targets t
        INNER JOIN ai_analysis_node_attempts a ON a.id = t.published_attempt_id
        WHERE t.job_id = ? AND t.status = 'SUCCEEDED'
        ORDER BY t.source_order ASC
        """,
        (job_id,),
    ).fetchall()
    counts = {"analyzed": 0, "info": 0, "screened": 0}
    for row in rows:
        try:
            result = json.loads(str(row["result_payload_json"] or "{}"))["result"]
            assessment = result["assessment"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if str(assessment.get("analysis_label") or "").strip().upper() == "INFO":
            counts["info"] += 1
        elif str(assessment.get("ai_decision") or "").strip():
            counts["analyzed"] += 1
        else:
            counts["screened"] += 1
    return counts


def _first_running_target(conn: Any, job_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM ai_analysis_job_targets
        WHERE job_id = ? AND status IN ('RUNNING', 'PUBLISHING')
        ORDER BY source_order ASC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def _refresh_incremental_job_counts(
    conn: Any,
    job_id: str,
    *,
    updated_at: str,
) -> dict[str, int]:
    counts = _analysis_target_counts(conn, job_id)
    outcome_counts = _analysis_outcome_counts(conn, job_id)
    running = _first_running_target(conn, job_id)
    conn.execute(
        """
        UPDATE ai_analysis_jobs
        SET completed_requirements = ?, published_requirements = ?,
            analyzed_requirements = ?, info_requirements = ?, screened_requirements = ?,
            current_requirement_id = ?, current_requirement_code = ?,
            current_requirement_index = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            counts["computed"],
            counts["succeeded"],
            outcome_counts["analyzed"],
            outcome_counts["info"],
            outcome_counts["screened"],
            str((running or {}).get("requirement_id") or ""),
            str((running or {}).get("requirement_code") or ""),
            max(int((running or {}).get("source_order") or 0), 0) + 1
            if running
            else 0,
            updated_at,
            job_id,
        ),
    )
    return counts


def _incremental_projection_ready(
    conn: Any,
    *,
    snapshot: Any,
    requirement_set_id: str,
    requirement_set_version: int,
    requirement_tree_sha256: str,
    knowledge_base_version: int,
    universe_requirement_ids: list[str],
) -> bool:
    if (
        not universe_requirement_ids
        or len(universe_requirement_ids) != len(set(universe_requirement_ids))
    ):
        return False
    placeholders = ", ".join("?" for _ in universe_requirement_ids)
    current_count = conn.execute(
        f"""
        SELECT COUNT(*) FROM assessments
        WHERE requirement_id IN ({placeholders}) AND reference_status = 'CURRENT'
        """,
        universe_requirement_ids,
    ).fetchone()[0]
    if int(current_count or 0) != len(universe_requirement_ids):
        return False
    prior_projection_current = bool(
        str(snapshot["ai_assessed_at"] or "").strip()
        and int(snapshot["ai_requirement_set_version"] or 0)
        == requirement_set_version
        and int(snapshot["ai_knowledge_base_version"] or 0)
        == knowledge_base_version
    )
    if prior_projection_current:
        return True
    published_rows = conn.execute(
        f"""
        SELECT DISTINCT t.requirement_id
        FROM ai_analysis_job_targets t
        INNER JOIN ai_analysis_jobs j ON j.id = t.job_id
        WHERE t.requirement_id IN ({placeholders})
          AND t.status = 'SUCCEEDED' AND t.published_attempt_id <> ''
          AND j.requirement_set_id = ? AND j.requirement_set_version = ?
          AND j.requirement_tree_sha256 = ? AND j.knowledge_base_version = ?
          AND j.publication_mode = 'PER_NODE_RECOVERABLE'
        """,
        [
            *universe_requirement_ids,
            requirement_set_id,
            requirement_set_version,
            requirement_tree_sha256,
            knowledge_base_version,
        ],
    ).fetchall()
    published_ids = {str(row["requirement_id"] or "") for row in published_rows}
    return published_ids == set(universe_requirement_ids)


def _safe_error_code(value: str) -> str:
    normalized = "".join(
        character
        for character in str(value or "").strip().lower()
        if character.isalnum() or character == "_"
    )[:120]
    return normalized or "analysis_item_failed"


def _safe_error_message(value: str) -> str:
    normalized = " ".join(str(value or "").split())[:800]
    return normalized or "This analysis item could not be completed."


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _analysis_job_target_pairs(job: Any) -> tuple[tuple[str, str], ...]:
    return _analysis_job_pairs(job, field="target_pairs", label="target")


def _analysis_job_universe_pairs(job: Any) -> tuple[tuple[str, str], ...]:
    pairs = _analysis_job_pairs(
        job,
        field="analysis_universe_pairs",
        label="analysis universe",
    )
    if not pairs:
        raise RuntimeError("AI analysis job frozen scope has no analysis universe.")
    return pairs


def _analysis_job_pairs(
    job: Any,
    *,
    field: str,
    label: str,
) -> tuple[tuple[str, str], ...]:
    raw = str(job["scope_payload_json"] or "").strip()
    expected_sha256 = str(job["scope_sha256"] or "").strip()
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("AI analysis job has an invalid frozen scope.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AI analysis job has an invalid frozen scope.")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    if canonical != raw or hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_sha256:
        raise RuntimeError("AI analysis job frozen scope failed integrity validation.")
    raw_pairs = payload.get(field)
    if not isinstance(raw_pairs, list):
        raise RuntimeError(f"AI analysis job frozen scope has no {label} pairs.")
    pairs: list[tuple[str, str]] = []
    for item in raw_pairs:
        if not isinstance(item, dict):
            raise RuntimeError(
                f"AI analysis job frozen scope {label} is invalid."
            )
        node_id = str(item.get("node_id") or "").strip()
        requirement_id = str(item.get("requirement_id") or "").strip()
        if not node_id or not requirement_id:
            raise RuntimeError(
                f"AI analysis job frozen scope {label} is invalid."
            )
        pairs.append((node_id, requirement_id))
    if (
        len(pairs) != len(set(pairs))
        or len({item[0] for item in pairs}) != len(pairs)
        or len({item[1] for item in pairs}) != len(pairs)
    ):
        raise RuntimeError(
            f"AI analysis job frozen scope {label} is not unique."
        )
    return tuple(pairs)


def _complete_analysis_job(
    conn: Any,
    *,
    document_id: str,
    profile_id: str,
    provider: str,
    completion: dict[str, Any],
    requirement_set_id: str,
    completed_at: str,
) -> None:
    job_id = str(completion.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("Atomic AI result publication is missing its job id.")
    total_requirements = max(int(completion.get("total_requirements") or 0), 0)
    cursor = conn.execute(
        """
        UPDATE ai_analysis_jobs
        SET status = 'COMPLETED',
            completed_requirements = ?,
            published_requirements = ?,
            analyzed_requirements = ?,
            info_requirements = ?,
            screened_requirements = ?,
            current_requirement_id = '',
            current_requirement_code = '',
            current_requirement_index = ?,
            current_stage = 'completed',
            last_message = ?,
            error_message = '',
            finished_at = ?,
            updated_at = ?
        WHERE id = ? AND sor_document_id = ? AND requirement_set_id = ? AND status = 'RUNNING'
        """,
        (
            max(int(completion.get("completed_requirements") or 0), 0),
            total_requirements,
            max(int(completion.get("analyzed_requirements") or 0), 0),
            max(int(completion.get("info_requirements") or 0), 0),
            max(int(completion.get("screened_requirements") or 0), 0),
            total_requirements,
            str(completion.get("message") or "").strip(),
            completed_at,
            completed_at,
            job_id,
            document_id,
            requirement_set_id,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"AI analysis job {job_id} was not publishable from RUNNING state.")
    conn.execute(
        """
        INSERT INTO ai_analysis_events
        (id, job_id, sor_document_id, profile_id, provider, event_type, stage, message,
         requirement_id, requirement_code, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, 'job_completed', 'completed', ?, '', '', ?, ?)
        """,
        (
            str(uuid.uuid4()),
            job_id,
            document_id,
            str(profile_id or "").strip(),
            str(provider or "").strip(),
            str(completion.get("message") or "").strip(),
            json.dumps(completion.get("event_payload") or {}, ensure_ascii=False),
            completed_at,
        ),
    )


def _persist_result(
    conn: Any,
    requirement_id: str,
    result: dict[str, Any],
    *,
    profile_id: str,
    provider: str,
    retrieval_backend: str,
    analysis_input_schema_version: str,
    analysis_input_sha256: str,
    published_at: str,
) -> None:
    input_schema = str(analysis_input_schema_version or "").strip()
    input_sha256 = str(analysis_input_sha256 or "").strip()
    if bool(input_schema) != bool(input_sha256):
        raise ValueError("analysis semantic-context provenance must be paired")
    if input_sha256 and (
        len(input_sha256) != 64
        or any(character not in "0123456789abcdef" for character in input_sha256)
    ):
        raise ValueError("analysis input SHA-256 is invalid")
    fields = _assessment_fields(
        result,
        profile_id=profile_id,
        provider=provider,
        retrieval_backend=retrieval_backend,
    )
    conn.execute(
        """
        INSERT INTO assessments
        (id, requirement_id, profile_id, provider, retrieval_backend, ai_decision, compliance_status, ai_reason,
         ai_confidence, assessment_mode, analysis_label, analysis_basis, normalized_requirement,
         risk_level, clarification_question, final_decision, engineer_reason, model_name,
         prompt_version, evidence_findings_json, structured_information_gap_json,
         sub_judgments_json, reference_status, source_requirement_id,
         result_origin, source_assessment_id, impact_receipt_id,
         analysis_input_schema_version, analysis_input_sha256, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?,
                'CURRENT', '', 'NATIVE', '', '', ?, ?, ?)
        ON CONFLICT(requirement_id) DO UPDATE SET
            profile_id = excluded.profile_id,
            provider = excluded.provider,
            retrieval_backend = excluded.retrieval_backend,
            ai_decision = excluded.ai_decision,
            compliance_status = excluded.compliance_status,
            ai_reason = excluded.ai_reason,
            ai_confidence = excluded.ai_confidence,
            assessment_mode = excluded.assessment_mode,
            analysis_label = excluded.analysis_label,
            analysis_basis = excluded.analysis_basis,
            normalized_requirement = excluded.normalized_requirement,
            risk_level = excluded.risk_level,
            clarification_question = excluded.clarification_question,
            model_name = excluded.model_name,
            prompt_version = excluded.prompt_version,
            evidence_findings_json = excluded.evidence_findings_json,
            structured_information_gap_json = excluded.structured_information_gap_json,
            sub_judgments_json = excluded.sub_judgments_json,
            reference_status = 'CURRENT',
            source_requirement_id = '',
            result_origin = 'NATIVE',
            source_assessment_id = '',
            impact_receipt_id = '',
            analysis_input_schema_version = excluded.analysis_input_schema_version,
            analysis_input_sha256 = excluded.analysis_input_sha256,
            updated_at = excluded.updated_at
        """,
        (
            str(uuid.uuid4()),
            requirement_id,
            fields["profile_id"],
            fields["provider"],
            fields["retrieval_backend"],
            fields["ai_decision"],
            fields["compliance_status"],
            fields["ai_reason"],
            fields["ai_confidence"],
            fields["assessment_mode"],
            fields["analysis_label"],
            fields["analysis_basis"],
            fields["normalized_requirement"],
            fields["risk_level"],
            fields["clarification_question"],
            fields["model_name"],
            fields["prompt_version"],
            fields["evidence_findings_json"],
            fields["structured_information_gap_json"],
            fields["sub_judgments_json"],
            input_schema,
            input_sha256,
            published_at,
        ),
    )
    conn.execute("DELETE FROM evidences WHERE requirement_id = ?", (requirement_id,))
    for evidence in result["evidences"]:
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
                _canonical_json_field(metadata, default={}),
                evidence["source_location"],
                evidence["excerpt"],
                float(evidence["relevance"]),
            ),
        )


def _assessment_fields(
    result: dict[str, Any],
    *,
    profile_id: str,
    provider: str,
    retrieval_backend: str,
) -> dict[str, Any]:
    assessment = result["assessment"]
    return {
        "profile_id": str(assessment.get("profile_id") or profile_id),
        "provider": str(assessment.get("provider") or provider),
        "ai_decision": assessment["ai_decision"],
        "compliance_status": str(assessment.get("compliance_status") or ""),
        "ai_reason": assessment["ai_reason"],
        "ai_confidence": float(assessment["ai_confidence"]),
        "assessment_mode": str(assessment.get("assessment_mode") or ""),
        "retrieval_backend": str(assessment.get("retrieval_backend") or retrieval_backend),
        "analysis_label": str(assessment.get("analysis_label") or "REQUIREMENT"),
        "analysis_basis": str(assessment.get("analysis_basis") or ""),
        "normalized_requirement": str(assessment.get("normalized_requirement") or ""),
        "risk_level": str(assessment.get("risk_level") or ""),
        "clarification_question": str(assessment.get("clarification_question") or ""),
        "model_name": assessment["model_name"],
        "prompt_version": assessment["prompt_version"],
        "evidence_findings_json": _canonical_json_field(
            assessment.get("evidence_findings"), default=[]
        ),
        "structured_information_gap_json": _canonical_json_field(
            assessment.get("structured_information_gap"), default={}
        ),
        "sub_judgments_json": _canonical_json_field(
            assessment.get("sub_judgments"), default=[]
        ),
    }


def _canonical_json_field(value: Any, *, default: Any) -> str:
    expected_type = list if isinstance(default, list) else dict
    normalized = value if isinstance(value, expected_type) else default
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
