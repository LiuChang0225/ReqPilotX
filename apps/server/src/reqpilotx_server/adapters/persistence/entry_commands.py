"""Atomic receipts for the current single-owner project/source entry API."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class EntryCommandConflict(ValueError):
    pass


class EntryCommandGone(ValueError):
    pass


def validate_command_id(command_id: str | None) -> str | None:
    if command_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", command_id
    ):
        raise ValueError("Invalid entry command_id.")
    return command_id


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def read_receipt(
    conn, *, kind: str, scope: str, command_id: str, request_sha256: str | None = None
) -> dict | None:
    row = conn.execute(
        """SELECT * FROM entry_command_receipts
        WHERE kind = ? AND scope_id = ? AND command_id = ?""",
        (kind, scope, command_id),
    ).fetchone()
    if row is None:
        return None
    if request_sha256 is not None and row["request_sha256"] != request_sha256:
        raise EntryCommandConflict("entry_command_id_conflict")
    table = "projects" if kind == "project.create" else "sor_documents"
    resource = conn.execute(
        f"SELECT * FROM {table} WHERE id = ?", (row["resource_id"],)
    ).fetchone()
    if resource is None:
        raise EntryCommandGone("entry_command_result_deleted")
    if kind == "document.import" and resource["project_id"] != scope:
        raise EntryCommandGone("entry_command_result_deleted")
    return json.loads(row["result_json"])


def claim_receipt(
    conn, *, kind: str, scope: str, command_id: str | None, request_sha256: str
) -> dict | None:
    """The unique insert waits for concurrent commit/rollback on PostgreSQL.

    The caller's transaction also creates the resource and completes the receipt;
    an incomplete receipt is never committed. SQLite serializes its writers.
    """
    validate_command_id(command_id)
    if command_id is None:
        return None
    inserted = conn.execute(
        """INSERT INTO entry_command_receipts
        (kind, scope_id, command_id, request_sha256, resource_id, result_json)
        VALUES (?, ?, ?, ?, '', '') ON CONFLICT (kind, scope_id, command_id) DO NOTHING""",
        (kind, scope, command_id, request_sha256),
    )
    if inserted.rowcount == 1:
        return None
    return read_receipt(
        conn,
        kind=kind,
        scope=scope,
        command_id=command_id,
        request_sha256=request_sha256,
    )


def complete_receipt(
    conn, *, kind: str, scope: str, command_id: str | None, result: dict
) -> None:
    if command_id is not None:
        conn.execute(
            """UPDATE entry_command_receipts SET resource_id = ?, result_json = ?
            WHERE kind = ? AND scope_id = ? AND command_id = ?""",
            (
                result["id"],
                json.dumps(result, ensure_ascii=False, default=str),
                kind,
                scope,
                command_id,
            ),
        )
