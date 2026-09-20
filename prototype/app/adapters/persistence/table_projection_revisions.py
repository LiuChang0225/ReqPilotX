from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from app import db


_REVISION_LOCK = RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: Any) -> None:
    if db.is_postgresql_runtime():
        return
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
                sor_document_id,
                source_sha256,
                structure_snapshot_id,
                logical_table_id,
                revision
            )
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_table_projection_revisions_scope
        ON table_projection_revisions (
            sor_document_id,
            source_sha256,
            structure_snapshot_id,
            logical_table_id,
            revision
        )
        """
    )


def _lock_lineage(
    conn: Any,
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    logical_table_id: str,
) -> None:
    """Serialize one projection lineage in both supported databases."""

    if not db.is_postgresql_runtime():
        conn.execute("BEGIN IMMEDIATE")
        return
    lock_key = "\x1f".join(
        (
            "table-projection-revision/v1",
            project_id,
            document_id,
            source_sha256,
            structure_snapshot_id,
            logical_table_id,
        )
    )
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(?))",
        (lock_key,),
    )


def list_effective_table_projection_revisions(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
) -> list[dict[str, Any]]:
    """Return the append-only head for each logical table in one exact lineage."""

    with db.get_conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM table_projection_revisions
            WHERE project_id = ?
              AND sor_document_id = ?
              AND source_sha256 = ?
              AND structure_snapshot_id = ?
            ORDER BY logical_table_id ASC, revision DESC
            """,
            (
                project_id,
                document_id,
                source_sha256,
                structure_snapshot_id,
            ),
        ).fetchall()
    heads: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        logical_table_id = str(item.get("logical_table_id") or "")
        if logical_table_id and logical_table_id not in heads:
            heads[logical_table_id] = item
    return list(heads.values())


def get_table_projection_revision_head(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    logical_table_id: str,
) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT *
            FROM table_projection_revisions
            WHERE project_id = ?
              AND sor_document_id = ?
              AND source_sha256 = ?
              AND structure_snapshot_id = ?
              AND logical_table_id = ?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (
                project_id,
                document_id,
                source_sha256,
                structure_snapshot_id,
                logical_table_id,
            ),
        ).fetchone()
    return dict(row) if row is not None else None


def get_table_projection_revision_by_projection_id(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    projection_id: str,
) -> dict[str, Any] | None:
    """Resolve a prior projection identity inside one exact source lineage."""

    if not str(projection_id or "").strip():
        return None
    with db.get_conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT *
            FROM table_projection_revisions
            WHERE project_id = ?
              AND sor_document_id = ?
              AND source_sha256 = ?
              AND structure_snapshot_id = ?
              AND projection_id = ?
            ORDER BY revision DESC
            LIMIT 1
            """,
            (
                project_id,
                document_id,
                source_sha256,
                structure_snapshot_id,
                projection_id,
            ),
        ).fetchone()
    return dict(row) if row is not None else None


def get_table_projection_revision_by_id(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    revision_id: str,
) -> dict[str, Any] | None:
    """Resolve one persisted predecessor without crossing its lineage."""

    if not str(revision_id or "").strip():
        return None
    with db.get_conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT *
            FROM table_projection_revisions
            WHERE id = ?
              AND project_id = ?
              AND sor_document_id = ?
              AND source_sha256 = ?
              AND structure_snapshot_id = ?
            LIMIT 1
            """,
            (
                revision_id,
                project_id,
                document_id,
                source_sha256,
                structure_snapshot_id,
            ),
        ).fetchone()
    return dict(row) if row is not None else None


def get_max_persisted_projection_revision(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    logical_table_id: str,
) -> int:
    """Return the highest content revision, including revisions before restore."""

    with db.get_conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT projection_json
            FROM table_projection_revisions
            WHERE project_id = ?
              AND sor_document_id = ?
              AND source_sha256 = ?
              AND structure_snapshot_id = ?
              AND logical_table_id = ?
              AND projection_json <> ''
            """,
            (
                project_id,
                document_id,
                source_sha256,
                structure_snapshot_id,
                logical_table_id,
            ),
        ).fetchall()
    highest = 0
    for row in rows:
        try:
            payload = json.loads(str(row["projection_json"] or ""))
            revision = int(payload.get("projection_revision") or 0)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "stored table projection revision is invalid"
            ) from exc
        if revision < 0:
            raise ValueError("stored table projection revision is invalid")
        highest = max(highest, revision)
    return highest


def append_table_projection_revision(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    logical_table_id: str,
    expected_revision: int,
    action: str,
    projection: dict[str, Any] | None,
    evidence_bindings: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], bool]:
    """Append one optimistic manual projection revision.

    Repeating the exact same command at the same head is idempotent.  A stale
    expected revision is rejected by the caller-visible ``ValueError``.
    """

    normalized_action = str(action or "").strip().upper()
    if normalized_action not in {
        "SPLIT_ROWS",
        "MERGE_PREVIOUS",
        "MERGE_NEXT",
        "KEEP_WHOLE",
        "RESTORE_AUTO",
    }:
        raise ValueError("unsupported table projection revision action")
    projection_payload = dict(projection or {})
    evidence_payload = list(evidence_bindings or [])
    if normalized_action == "RESTORE_AUTO":
        if projection_payload or evidence_payload:
            raise ValueError(
                "RESTORE_AUTO must not persist a replacement projection"
            )
    elif not projection_payload:
        raise ValueError(
            "an active table projection revision requires a projection"
        )
    projection_id = str(projection_payload.get("projection_id") or "")
    projection_sha256 = str(
        projection_payload.get("projection_sha256") or ""
    )
    projection_json = (
        json.dumps(
            projection_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if projection_payload
        else ""
    )
    evidence_json = json.dumps(
        evidence_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with _REVISION_LOCK:
        with db.get_conn() as conn:
            _ensure_schema(conn)
            _lock_lineage(
                conn,
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                structure_snapshot_id=structure_snapshot_id,
                logical_table_id=logical_table_id,
            )
            current = conn.execute(
                """
                SELECT *
                FROM table_projection_revisions
                WHERE project_id = ?
                  AND sor_document_id = ?
                  AND source_sha256 = ?
                  AND structure_snapshot_id = ?
                  AND logical_table_id = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (
                    project_id,
                    document_id,
                    source_sha256,
                    structure_snapshot_id,
                    logical_table_id,
                ),
            ).fetchone()
            current_revision = int(
                (current["revision"] if current is not None else 0) or 0
            )
            if current is not None and all(
                (
                    str(current["action"] or "") == normalized_action,
                    str(current["projection_json"] or "") == projection_json,
                    str(current["evidence_bindings_json"] or "")
                    == evidence_json,
                )
            ):
                return dict(current), False
            if current_revision != int(expected_revision):
                raise ValueError(
                    "table projection revision changed; refresh before retrying"
                )
            revision_id = str(uuid.uuid4())
            next_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO table_projection_revisions (
                    id, project_id, sor_document_id, source_sha256,
                    structure_snapshot_id, logical_table_id, revision, action,
                    projection_id, projection_sha256, projection_json,
                    evidence_bindings_json, supersedes_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    project_id,
                    document_id,
                    source_sha256,
                    structure_snapshot_id,
                    logical_table_id,
                    next_revision,
                    normalized_action,
                    projection_id,
                    projection_sha256,
                    projection_json,
                    evidence_json,
                    str(current["id"] or "") if current is not None else "",
                    _now_iso(),
                ),
            )
            inserted = conn.execute(
                "SELECT * FROM table_projection_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
    if inserted is None:
        raise RuntimeError("table projection revision was not persisted")
    return dict(inserted), True


def decode_projection_revision(
    row: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    projection_json = str(row.get("projection_json") or "")
    evidence_json = str(row.get("evidence_bindings_json") or "[]")
    projection = json.loads(projection_json) if projection_json else None
    evidence = json.loads(evidence_json)
    if projection is not None and not isinstance(projection, dict):
        raise ValueError("stored table projection revision is invalid")
    if not isinstance(evidence, list):
        raise ValueError("stored table projection evidence is invalid")
    return projection, [
        dict(item) for item in evidence if isinstance(item, dict)
    ]


__all__ = [
    "append_table_projection_revision",
    "decode_projection_revision",
    "get_max_persisted_projection_revision",
    "get_table_projection_revision_by_id",
    "get_table_projection_revision_by_projection_id",
    "get_table_projection_revision_head",
    "list_effective_table_projection_revisions",
]
