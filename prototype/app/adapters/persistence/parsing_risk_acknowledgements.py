from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_parsing_risk_acknowledgement(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    parse_artifact_id: str,
    parse_identity_sha256: str,
) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM parsing_risk_acknowledgements
            WHERE project_id = ?
              AND sor_document_id = ?
              AND source_sha256 = ?
              AND parse_artifact_id = ?
              AND parse_identity_sha256 = ?
            LIMIT 1
            """,
            (
                project_id,
                document_id,
                source_sha256,
                parse_artifact_id,
                parse_identity_sha256,
            ),
        ).fetchone()
    return dict(row) if row is not None else None


def record_parsing_risk_acknowledgement(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    parse_artifact_id: str,
    parse_identity_sha256: str,
    severe_warning_count: int,
) -> dict[str, Any]:
    identity = "\x1f".join(
        (
            "parsing-risk-acknowledgement/v1",
            project_id,
            document_id,
            source_sha256,
            parse_artifact_id,
            parse_identity_sha256,
        )
    )
    receipt_id = f"parse-risk-ack-{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
    acknowledged_at = _now_iso()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO parsing_risk_acknowledgements (
                id,
                project_id,
                sor_document_id,
                source_sha256,
                parse_artifact_id,
                parse_identity_sha256,
                severe_warning_count,
                acknowledged_at,
                actor_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'USER')
            ON CONFLICT (
                sor_document_id,
                source_sha256,
                parse_artifact_id,
                parse_identity_sha256
            ) DO NOTHING
            """,
            (
                receipt_id,
                project_id,
                document_id,
                source_sha256,
                parse_artifact_id,
                parse_identity_sha256,
                max(0, int(severe_warning_count)),
                acknowledged_at,
            ),
        )
        row = conn.execute(
            """
            SELECT *
            FROM parsing_risk_acknowledgements
            WHERE project_id = ?
              AND sor_document_id = ?
              AND source_sha256 = ?
              AND parse_artifact_id = ?
              AND parse_identity_sha256 = ?
            LIMIT 1
            """,
            (
                project_id,
                document_id,
                source_sha256,
                parse_artifact_id,
                parse_identity_sha256,
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError("Parsing risk acknowledgement was not persisted.")
    return dict(row)


__all__ = [
    "get_parsing_risk_acknowledgement",
    "record_parsing_risk_acknowledgement",
]
