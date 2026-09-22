from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from reqpilotx_server import db

_STATE_LOCK = RLock()
_DISPOSITIONS = {"ANALYZE", "RETAIN", "EXCLUDE"}
REQUIREMENT_MATRIX_MUTABLE_STATE_SCHEMA_VERSION = (
    "requirement-matrix-mutable-state/v1"
)
REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION = "requirement-matrix-state/v1"

_ASSESSMENT_MUTABLE_STATE_COLUMNS = (
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
    "final_decision",
    "engineer_reason",
    "model_name",
    "prompt_version",
    "evidence_findings_json",
    "structured_information_gap_json",
    "sub_judgments_json",
    "reference_status",
    "source_requirement_id",
    "result_origin",
    "source_assessment_id",
    "impact_receipt_id",
    "analysis_input_schema_version",
    "analysis_input_sha256",
    "updated_at",
)

_EVIDENCE_MUTABLE_STATE_COLUMNS = (
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: Any) -> None:
    if db.is_postgresql_runtime():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requirement_matrix_node_states (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            sor_document_id TEXT NOT NULL,
            requirement_set_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            disposition TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(requirement_set_id, node_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
            FOREIGN KEY(requirement_set_id) REFERENCES requirement_sets(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_requirement_matrix_node_states_scope
        ON requirement_matrix_node_states (
            project_id,
            sor_document_id,
            requirement_set_id,
            node_id
        )
        """
    )


def _lock_node_state(
    conn: Any,
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    node_id: str,
) -> None:
    lock_requirement_matrix_sets_in_connection(
        conn,
        project_id=project_id,
        document_id=document_id,
        requirement_set_ids=(requirement_set_id,),
    )
    if not db.is_postgresql_runtime():
        return
    lock_key = "\x1f".join(
        (
            "requirement-matrix-node-state/v1",
            project_id,
            document_id,
            requirement_set_id,
            node_id,
        )
    )
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (lock_key,))


def _lock_node_states(
    conn: Any,
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    node_ids: list[str],
) -> None:
    lock_requirement_matrix_sets_in_connection(
        conn,
        project_id=project_id,
        document_id=document_id,
        requirement_set_ids=(requirement_set_id,),
    )
    if not db.is_postgresql_runtime():
        return
    for node_id in sorted(node_ids):
        lock_key = "\x1f".join(
            (
                "requirement-matrix-node-state/v1",
                project_id,
                document_id,
                requirement_set_id,
                node_id,
            )
        )
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (lock_key,))


def lock_requirement_matrix_sets_in_connection(
    conn: Any,
    *,
    project_id: str,
    document_id: str,
    requirement_set_ids: tuple[str, ...] | list[str],
) -> None:
    """Serialize every mutable Matrix projection for the supplied sets.

    Node writes, impact projection and final lock all share this boundary.  The
    sorted multi-set order prevents deadlocks when an impact receipt binds a
    source and target set.  SQLite's write reservation is the equivalent lock.
    """

    normalized_ids = tuple(
        sorted({str(value or "").strip() for value in requirement_set_ids})
    )
    if not normalized_ids or any(not value for value in normalized_ids):
        raise ValueError("requirement Matrix lock scope is invalid")
    if not db.is_postgresql_runtime():
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        return
    for requirement_set_id in normalized_ids:
        lock_key = "\x1f".join(
            (
                "requirement-matrix-set/v1",
                project_id,
                document_id,
                requirement_set_id,
            )
        )
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (lock_key,))


def requirement_matrix_mutable_state_sha256(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> str:
    """Return a canonical token for every mutable Matrix/result input."""

    with db.get_conn() as conn:
        _ensure_schema(conn)
        return requirement_matrix_mutable_state_sha256_in_connection(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
        )


def requirement_matrix_state_sha256(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> str:
    with db.get_conn() as conn:
        _ensure_schema(conn)
        return requirement_matrix_state_sha256_in_connection(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
        )


def requirement_matrix_state_sha256_in_connection(
    conn: Any,
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> str:
    """Hash only user-editable Matrix state for impact receipt ordering."""

    _ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT node_id, disposition, reason, revision
        FROM requirement_matrix_node_states
        WHERE project_id = ?
          AND sor_document_id = ?
          AND requirement_set_id = ?
        ORDER BY node_id ASC
        """,
        (project_id, document_id, requirement_set_id),
    ).fetchall()
    payload = {
        "schema_version": REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "node_states": [
            {
                "node_id": str(row["node_id"] or ""),
                "disposition": str(row["disposition"] or ""),
                "reason": str(row["reason"] or ""),
                "revision": int(row["revision"] or 0),
            }
            for row in rows
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def requirement_matrix_mutable_state_sha256_in_connection(
    conn: Any,
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> str:
    """Hash persisted dispositions plus assessment and evidence currency/provenance.

    Requirement Tree content is immutable and bound separately by tree SHA.
    Revisions and the complete assessment payload make undo/redo and carried
    result replacement distinguishable even when their visible labels match.
    """

    _ensure_schema(conn)
    state_rows = conn.execute(
        """
        SELECT node_id, disposition, reason, revision
        FROM requirement_matrix_node_states
        WHERE project_id = ?
          AND sor_document_id = ?
          AND requirement_set_id = ?
        ORDER BY node_id ASC
        """,
        (project_id, document_id, requirement_set_id),
    ).fetchall()
    assessment_rows = conn.execute(
        """
        SELECT r.id AS bound_requirement_id, a.*
        FROM requirements r
        LEFT JOIN assessments a ON a.requirement_id = r.id
        WHERE r.project_id = ?
          AND r.sor_document_id = ?
          AND r.requirement_set_id = ?
        ORDER BY r.id ASC
        """,
        (project_id, document_id, requirement_set_id),
    ).fetchall()
    evidence_rows = conn.execute(
        """
        SELECT r.id AS bound_requirement_id, e.*
        FROM requirements r
        INNER JOIN evidences e ON e.requirement_id = r.id
        WHERE r.project_id = ?
          AND r.sor_document_id = ?
          AND r.requirement_set_id = ?
        ORDER BY r.id ASC, e.source_doc_id ASC, e.source_location ASC,
                 e.content_hash ASC, e.excerpt ASC, e.id ASC
        """,
        (project_id, document_id, requirement_set_id),
    ).fetchall()
    payload = {
        "schema_version": REQUIREMENT_MATRIX_MUTABLE_STATE_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "node_states": [
            {
                "node_id": str(row["node_id"] or ""),
                "disposition": str(row["disposition"] or ""),
                "reason": str(row["reason"] or ""),
                "revision": int(row["revision"] or 0),
            }
            for row in state_rows
        ],
        "assessments": [
            {
                "requirement_id": str(row["bound_requirement_id"] or ""),
                "assessment_id": str(row["id"] or ""),
                **{
                    column: row[column]
                    for column in _ASSESSMENT_MUTABLE_STATE_COLUMNS
                },
            }
            for row in assessment_rows
        ],
        # Evidence row IDs are deliberately excluded: they are persistence
        # identities regenerated by an otherwise equivalent publication.  The
        # ordered semantic multiset is what can change Matrix result currency.
        "evidences": [
            {
                "requirement_id": str(row["bound_requirement_id"] or ""),
                **{
                    column: row[column]
                    for column in _EVIDENCE_MUTABLE_STATE_COLUMNS
                },
            }
            for row in evidence_rows
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def list_requirement_matrix_node_states(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM requirement_matrix_node_states
            WHERE project_id = ?
              AND sor_document_id = ?
              AND requirement_set_id = ?
            ORDER BY node_id ASC
            """,
            (project_id, document_id, requirement_set_id),
        ).fetchall()
    return [dict(row) for row in rows]


def clone_requirement_matrix_node_states(
    *,
    project_id: str,
    document_id: str,
    source_requirement_set_id: str,
    target_requirement_set_id: str,
    allowed_node_ids: set[str],
) -> int:
    """Carry explicit Matrix dispositions into one verified derived DRAFT."""

    timestamp = _now_iso()
    with _STATE_LOCK, db.get_conn() as conn:
        _ensure_schema(conn)
        lock_requirement_matrix_sets_in_connection(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_ids=(
                source_requirement_set_id,
                target_requirement_set_id,
            ),
        )
        target = conn.execute(
            """
            SELECT rs.id, rs.status, rs.is_active, rs.supersedes_id,
                   d.active_requirement_set_id
            FROM requirement_sets rs
            INNER JOIN sor_documents d ON d.id = rs.sor_document_id
            WHERE rs.id = ? AND rs.project_id = ? AND rs.sor_document_id = ?
            """,
            (target_requirement_set_id, project_id, document_id),
        ).fetchone()
        source = conn.execute(
            """
            SELECT id, is_active FROM requirement_sets
            WHERE id = ? AND project_id = ? AND sor_document_id = ?
            """,
            (source_requirement_set_id, project_id, document_id),
        ).fetchone()
        if source is None or target is None:
            raise LookupError("matrix_state_clone_scope_invalid")
        active_target = bool(int(target["is_active"] or 0))
        inactive_staging_target = bool(
            not active_target
            and int(source["is_active"] or 0)
            and str(target["active_requirement_set_id"] or "")
            == source_requirement_set_id
        )
        if (
            str(target["status"] or "").strip().upper() != "DRAFT"
            or not (active_target or inactive_staging_target)
            or str(target["supersedes_id"] or "") != source_requirement_set_id
        ):
            raise PermissionError("matrix_state_clone_target_invalid")
        source_rows = conn.execute(
            """
            SELECT node_id, disposition, reason
            FROM requirement_matrix_node_states
            WHERE project_id = ?
              AND sor_document_id = ?
              AND requirement_set_id = ?
            ORDER BY node_id ASC
            """,
            (project_id, document_id, source_requirement_set_id),
        ).fetchall()
        invalid_node_ids = {
            str(row["node_id"] or "") for row in source_rows
        } - allowed_node_ids
        if invalid_node_ids:
            raise ValueError("matrix_state_clone_node_binding_invalid")
        existing = conn.execute(
            """
            SELECT node_id, disposition, reason
            FROM requirement_matrix_node_states
            WHERE project_id = ?
              AND sor_document_id = ?
              AND requirement_set_id = ?
            ORDER BY node_id ASC
            """,
            (project_id, document_id, target_requirement_set_id),
        ).fetchall()
        expected = [
            (
                str(row["node_id"] or ""),
                str(row["disposition"] or ""),
                str(row["reason"] or ""),
            )
            for row in source_rows
        ]
        if existing:
            actual = [
                (
                    str(row["node_id"] or ""),
                    str(row["disposition"] or ""),
                    str(row["reason"] or ""),
                )
                for row in existing
            ]
            if actual != expected:
                raise RuntimeError("matrix_state_clone_target_conflict")
            return len(actual)
        for row in source_rows:
            conn.execute(
                """
                INSERT INTO requirement_matrix_node_states (
                    id, project_id, sor_document_id, requirement_set_id,
                    node_id, disposition, reason, revision, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    project_id,
                    document_id,
                    target_requirement_set_id,
                    str(row["node_id"] or ""),
                    str(row["disposition"] or ""),
                    str(row["reason"] or ""),
                    timestamp,
                    timestamp,
                ),
            )
    return len(source_rows)


def set_requirement_matrix_node_disposition(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    node_id: str,
    disposition: str,
    reason: str,
    expected_revision: int,
) -> dict[str, Any]:
    normalized = str(disposition or "").strip().upper()
    if normalized not in _DISPOSITIONS:
        raise ValueError("matrix_disposition_invalid")
    timestamp = _now_iso()
    with _STATE_LOCK, db.get_conn() as conn:
        _ensure_schema(conn)
        _lock_node_state(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
            node_id=node_id,
        )
        requirement_set = conn.execute(
            """
            SELECT rs.status, rs.is_active, d.active_requirement_set_id
            FROM requirement_sets rs
            INNER JOIN sor_documents d ON d.id = rs.sor_document_id
            WHERE rs.id = ?
              AND rs.project_id = ?
              AND rs.sor_document_id = ?
            """,
            (requirement_set_id, project_id, document_id),
        ).fetchone()
        if requirement_set is None:
            raise LookupError("matrix_requirement_set_not_found")
        if (
            not int(requirement_set["is_active"] or 0)
            or str(requirement_set["active_requirement_set_id"] or "")
            != requirement_set_id
        ):
            raise PermissionError("matrix_requirement_set_not_active")
        if str(requirement_set["status"] or "").strip().upper() == "LOCKED":
            raise PermissionError("matrix_requirement_set_locked")

        current = conn.execute(
            """
            SELECT * FROM requirement_matrix_node_states
            WHERE project_id = ?
              AND sor_document_id = ?
              AND requirement_set_id = ?
              AND node_id = ?
            """,
            (project_id, document_id, requirement_set_id, node_id),
        ).fetchone()
        current_revision = int(current["revision"] or 0) if current else 0
        if current_revision != int(expected_revision):
            raise RuntimeError("matrix_state_revision_conflict")
        revision = current_revision + 1
        if current is None:
            state_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO requirement_matrix_node_states (
                    id, project_id, sor_document_id, requirement_set_id,
                    node_id, disposition, reason, revision, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state_id,
                    project_id,
                    document_id,
                    requirement_set_id,
                    node_id,
                    normalized,
                    str(reason or "").strip(),
                    revision,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            state_id = str(current["id"])
            conn.execute(
                """
                UPDATE requirement_matrix_node_states
                SET disposition = ?, reason = ?, revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized,
                    str(reason or "").strip(),
                    revision,
                    timestamp,
                    state_id,
                ),
            )

        row = conn.execute(
            "SELECT * FROM requirement_matrix_node_states WHERE id = ?",
            (state_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("matrix_state_not_persisted")
    return dict(row)


def set_requirement_matrix_node_dispositions(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist one validated disposition batch in a single transaction."""

    if not updates:
        raise ValueError("matrix_disposition_batch_empty")
    node_ids = [str(item.get("node_id") or "") for item in updates]
    if any(not node_id for node_id in node_ids) or len(node_ids) != len(set(node_ids)):
        raise ValueError("matrix_disposition_batch_node_ids_invalid")
    normalized_updates: list[dict[str, Any]] = []
    for item in updates:
        disposition = str(item.get("disposition") or "").strip().upper()
        if disposition not in _DISPOSITIONS:
            raise ValueError("matrix_disposition_invalid")
        normalized_updates.append(
            {
                "node_id": str(item["node_id"]),
                "disposition": disposition,
                "reason": str(item.get("reason") or "").strip(),
                "expected_revision": int(item.get("expected_revision") or 0),
            }
        )

    timestamp = _now_iso()
    output: list[dict[str, Any]] = []
    with _STATE_LOCK, db.get_conn() as conn:
        _ensure_schema(conn)
        _lock_node_states(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
            node_ids=node_ids,
        )
        requirement_set = conn.execute(
            """
            SELECT rs.status, rs.is_active, d.active_requirement_set_id
            FROM requirement_sets rs
            INNER JOIN sor_documents d ON d.id = rs.sor_document_id
            WHERE rs.id = ?
              AND rs.project_id = ?
              AND rs.sor_document_id = ?
            """,
            (requirement_set_id, project_id, document_id),
        ).fetchone()
        if requirement_set is None:
            raise LookupError("matrix_requirement_set_not_found")
        if (
            not int(requirement_set["is_active"] or 0)
            or str(requirement_set["active_requirement_set_id"] or "")
            != requirement_set_id
        ):
            raise PermissionError("matrix_requirement_set_not_active")
        if str(requirement_set["status"] or "").strip().upper() == "LOCKED":
            raise PermissionError("matrix_requirement_set_locked")

        placeholders = ",".join("?" for _ in node_ids)
        existing_rows = conn.execute(
            f"""
            SELECT * FROM requirement_matrix_node_states
            WHERE project_id = ?
              AND sor_document_id = ?
              AND requirement_set_id = ?
              AND node_id IN ({placeholders})
            """,
            (project_id, document_id, requirement_set_id, *node_ids),
        ).fetchall()
        existing_by_node = {
            str(row["node_id"] or ""): row for row in existing_rows
        }
        for item in normalized_updates:
            current = existing_by_node.get(item["node_id"])
            current_revision = int(current["revision"] or 0) if current else 0
            if current_revision != item["expected_revision"]:
                raise RuntimeError("matrix_state_revision_conflict")

        state_ids: list[str] = []
        for item in normalized_updates:
            current = existing_by_node.get(item["node_id"])
            revision = item["expected_revision"] + 1
            if current is None:
                state_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO requirement_matrix_node_states (
                        id, project_id, sor_document_id, requirement_set_id,
                        node_id, disposition, reason, revision, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state_id,
                        project_id,
                        document_id,
                        requirement_set_id,
                        item["node_id"],
                        item["disposition"],
                        item["reason"],
                        revision,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                state_id = str(current["id"])
                conn.execute(
                    """
                    UPDATE requirement_matrix_node_states
                    SET disposition = ?, reason = ?, revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        item["disposition"],
                        item["reason"],
                        revision,
                        timestamp,
                        state_id,
                    ),
                )
            state_ids.append(state_id)

        for state_id in state_ids:
            row = conn.execute(
                "SELECT * FROM requirement_matrix_node_states WHERE id = ?",
                (state_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("matrix_state_not_persisted")
            output.append(dict(row))
    return output


def save_requirement_matrix_node_decision(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    node_id: str,
    requirement_id: str,
    disposition: str,
    final_decision: str,
    engineer_reason: str,
    expected_revision: int,
    analysis_input_schema_version: str,
    analysis_input_sha256: str,
) -> dict[str, Any]:
    normalized_disposition = str(disposition or "").strip().upper()
    if normalized_disposition not in _DISPOSITIONS:
        raise ValueError("matrix_disposition_invalid")
    timestamp = _now_iso()
    with _STATE_LOCK, db.get_conn() as conn:
        _ensure_schema(conn)
        _lock_node_state(
            conn,
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
            node_id=node_id,
        )
        requirement = conn.execute(
            """
            SELECT rs.status, rs.is_active, d.active_requirement_set_id
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
        if requirement is None:
            raise LookupError("matrix_requirement_entry_not_found")
        if (
            not int(requirement["is_active"] or 0)
            or str(requirement["active_requirement_set_id"] or "")
            != requirement_set_id
            or str(requirement["status"] or "").strip().upper() != "LOCKED"
        ):
            raise PermissionError("matrix_decision_requires_current_locked_set")

        current = conn.execute(
            """
            SELECT * FROM requirement_matrix_node_states
            WHERE project_id = ?
              AND sor_document_id = ?
              AND requirement_set_id = ?
              AND node_id = ?
            """,
            (project_id, document_id, requirement_set_id, node_id),
        ).fetchone()
        current_revision = int(current["revision"] or 0) if current else 0
        if current_revision != int(expected_revision):
            raise RuntimeError("matrix_state_revision_conflict")
        revision = current_revision + 1
        if current is None:
            state_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO requirement_matrix_node_states (
                    id, project_id, sor_document_id, requirement_set_id,
                    node_id, disposition, reason, revision, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    state_id,
                    project_id,
                    document_id,
                    requirement_set_id,
                    node_id,
                    normalized_disposition,
                    revision,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            state_id = str(current["id"])
            conn.execute(
                """
                UPDATE requirement_matrix_node_states
                SET revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (revision, timestamp, state_id),
            )

        assessment = conn.execute(
            "SELECT * FROM assessments WHERE requirement_id = ?",
            (requirement_id,),
        ).fetchone()
        old_value = {
            "final_decision": assessment["final_decision"] if assessment else None,
            "engineer_reason": assessment["engineer_reason"] if assessment else None,
            "reference_status": (
                str(assessment["reference_status"] or "CURRENT")
                if assessment
                else ""
            ),
        }
        if assessment is None:
            conn.execute(
                """
                INSERT INTO assessments (
                    id, requirement_id, ai_decision, ai_reason, ai_confidence,
                    assessment_mode, analysis_label, analysis_basis,
                    normalized_requirement, risk_level, clarification_question,
                    final_decision, engineer_reason, model_name, prompt_version,
                    reference_status, source_requirement_id, result_origin,
                    source_assessment_id, impact_receipt_id,
                    analysis_input_schema_version, analysis_input_sha256,
                    updated_at
                )
                VALUES (?, ?, 'NEED_CLARIFICATION', 'No AI assessment available.', 0,
                        'manual', 'REQUIREMENT', '', '', 'HIGH', '', ?, ?,
                        'manual', 'manual', 'CURRENT', '', 'NATIVE', '', '', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    requirement_id,
                    final_decision,
                    engineer_reason,
                    analysis_input_schema_version,
                    analysis_input_sha256,
                    timestamp,
                ),
            )
        elif str(assessment["reference_status"] or "CURRENT").upper() == (
            "STALE_REFERENCE"
        ):
            # A human conclusion is a new current result.  Never relabel the
            # carried AI payload as current: neutralize it and its evidences,
            # then reset every carried provenance pointer in the same write.
            conn.execute(
                "DELETE FROM evidences WHERE requirement_id = ?",
                (requirement_id,),
            )
            conn.execute(
                """
                UPDATE assessments
                SET profile_id = '', provider = '', retrieval_backend = '',
                    ai_decision = 'NEED_CLARIFICATION', compliance_status = '',
                    ai_reason = 'No AI assessment available.', ai_confidence = 0,
                    assessment_mode = 'manual', analysis_label = 'REQUIREMENT',
                    analysis_basis = '', normalized_requirement = '',
                    risk_level = 'HIGH', clarification_question = '',
                    model_name = 'manual', prompt_version = 'manual',
                    evidence_findings_json = '[]',
                    structured_information_gap_json = '{}',
                    sub_judgments_json = '[]',
                    final_decision = ?, engineer_reason = ?,
                    reference_status = 'CURRENT', source_requirement_id = '',
                    result_origin = 'NATIVE', source_assessment_id = '',
                    impact_receipt_id = ?, analysis_input_schema_version = ?,
                    analysis_input_sha256 = ?, updated_at = ?
                WHERE requirement_id = ?
                """,
                (
                    final_decision,
                    engineer_reason,
                    "",
                    analysis_input_schema_version,
                    analysis_input_sha256,
                    timestamp,
                    requirement_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE assessments
                SET final_decision = ?, engineer_reason = ?,
                    analysis_input_schema_version = ?,
                    analysis_input_sha256 = ?, updated_at = ?
                WHERE requirement_id = ?
                """,
                (
                    final_decision,
                    engineer_reason,
                    analysis_input_schema_version,
                    analysis_input_sha256,
                    timestamp,
                    requirement_id,
                ),
            )
        conn.execute(
            """
            INSERT INTO review_logs (
                id, requirement_id, action, old_value, new_value,
                comment, operator, operated_at
            )
            VALUES (?, ?, 'EDIT_DECISION', ?, ?, ?, 'engineer', ?)
            """,
            (
                str(uuid.uuid4()),
                requirement_id,
                json.dumps(old_value, ensure_ascii=False),
                json.dumps(
                    {
                        "final_decision": final_decision,
                        "engineer_reason": engineer_reason,
                    },
                    ensure_ascii=False,
                ),
                engineer_reason,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM requirement_matrix_node_states WHERE id = ?",
            (state_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("matrix_state_not_persisted")
    return dict(row)


__all__ = [
    "clone_requirement_matrix_node_states",
    "REQUIREMENT_MATRIX_MUTABLE_STATE_SCHEMA_VERSION",
    "REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION",
    "list_requirement_matrix_node_states",
    "lock_requirement_matrix_sets_in_connection",
    "requirement_matrix_mutable_state_sha256",
    "requirement_matrix_mutable_state_sha256_in_connection",
    "requirement_matrix_state_sha256",
    "requirement_matrix_state_sha256_in_connection",
    "save_requirement_matrix_node_decision",
    "set_requirement_matrix_node_disposition",
]
