from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from pydantic import ValidationError

from app import db
from app.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    canonical_entry_classification_payload_bytes,
    canonical_entry_classification_payload_sha256,
)


_SNAPSHOT_LOCK = RLock()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_LINEAGE_FIELDS = (
    "project_id",
    "sor_document_id",
    "source_sha256",
    "working_tree_id",
    "working_tree_sha256",
    "effective_structure_id",
    "semantic_snapshot_id",
    "policy_version",
)
_ENVELOPE_SCHEMA_VERSION = "entry-classification-persistence-envelope/v1"
_ENVELOPE_FIELDS = (
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
_COMMAND_RECEIPT_ENVELOPE_VERSION = (
    "entry-classification-command-receipt-envelope/v1"
)
_COMMAND_RECEIPT_ENVELOPE_FIELDS = (
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


class EntryClassificationStaleWrite(ValueError):
    pass


class EntryClassificationCommandConflict(ValueError):
    pass


class EntryClassificationSnapshotCorrupted(ValueError):
    pass


def _lock_entry_classification_lineage(
    conn: Any,
    snapshot: EntryClassificationSnapshotV1,
) -> None:
    if not db.is_postgresql_runtime():
        conn.execute("BEGIN IMMEDIATE")
        return
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(?))",
        ("\x1f".join(("entry-classification-snapshot/v1", *_lineage_values(snapshot))),),
    )


def get_entry_classification_snapshot_or_initial(
    initial_snapshot: EntryClassificationSnapshotV1,
) -> EntryClassificationSnapshotV1:
    """Return the exact persisted head or the supplied deterministic revision zero.

    Merely viewing a classification list never inserts a database row.
    """

    _validate_payload_identity(initial_snapshot)
    with db.get_conn() as conn:
        row = _select_head(conn, initial_snapshot)
    return initial_snapshot if row is None else _decode_snapshot(row)


def get_entry_classification_snapshot_by_id(
    snapshot_id: str,
) -> EntryClassificationSnapshotV1 | None:
    """Load and cryptographically verify one immutable snapshot receipt."""

    normalized_snapshot_id = str(snapshot_id or "").strip()
    if (
        not normalized_snapshot_id
        or len(normalized_snapshot_id) > 200
        or any(ord(character) < 32 for character in normalized_snapshot_id)
    ):
        raise ValueError("classification snapshot ID is invalid")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM entry_classification_snapshots WHERE id = ? LIMIT 1",
            (normalized_snapshot_id,),
        ).fetchone()
    return None if row is None else _decode_snapshot(row)


def get_latest_entry_classification_snapshot_for_document(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    working_tree_id: str,
    working_tree_sha256: str,
    effective_structure_id: str,
    semantic_snapshot_id: str,
    policy_version: str,
) -> EntryClassificationSnapshotV1 | None:
    """Read only the exact current WorkingTree classification lineage."""

    normalized_project_id = str(project_id or "").strip()
    normalized_document_id = str(document_id or "").strip()
    normalized_source_sha256 = str(source_sha256 or "").strip()
    normalized_working_tree_id = str(working_tree_id or "").strip()
    normalized_working_tree_sha256 = str(working_tree_sha256 or "").strip()
    normalized_effective_structure_id = str(effective_structure_id or "").strip()
    normalized_semantic_snapshot_id = str(semantic_snapshot_id or "").strip()
    normalized_policy_version = str(policy_version or "").strip()
    if (
        not normalized_project_id
        or not normalized_document_id
        or not _SHA256_RE.fullmatch(normalized_source_sha256)
        or not normalized_working_tree_id
        or not _SHA256_RE.fullmatch(normalized_working_tree_sha256)
        or not normalized_effective_structure_id
        or len(normalized_semantic_snapshot_id) > 200
        or not normalized_policy_version
        or len(normalized_policy_version) > 200
        or any(
            ord(character) < 32
            for character in (
                normalized_semantic_snapshot_id + normalized_policy_version
            )
        )
    ):
        raise ValueError("classification document binding is invalid")
    with db.get_conn() as conn:
        row = conn.execute(
            """
            SELECT classification.*
            FROM entry_classification_snapshots AS classification
            JOIN sor_documents AS document
              ON document.id = classification.sor_document_id
             AND document.project_id = classification.project_id
            WHERE classification.project_id = ?
              AND classification.sor_document_id = ?
              AND classification.source_sha256 = document.source_sha256
              AND classification.source_sha256 = ?
              AND classification.working_tree_id = ?
              AND classification.working_tree_sha256 = ?
              AND classification.effective_structure_id = ?
              AND classification.semantic_snapshot_id = ?
              AND classification.policy_version = ?
            ORDER BY classification.created_at DESC,
                     classification.revision DESC,
                     classification.id DESC
            LIMIT 1
            """,
            (
                normalized_project_id,
                normalized_document_id,
                normalized_source_sha256,
                normalized_working_tree_id,
                normalized_working_tree_sha256,
                normalized_effective_structure_id,
                normalized_semantic_snapshot_id,
                normalized_policy_version,
            ),
        ).fetchone()
    return None if row is None else _decode_snapshot(row)


def entry_classification_lineage_values(
    snapshot: EntryClassificationSnapshotV1,
) -> tuple[str, ...]:
    """Expose the canonical lineage tuple for atomic publication CAS."""

    _validate_payload_identity(snapshot)
    return _lineage_values(snapshot)


def count_entry_classification_snapshots(
    lineage: EntryClassificationSnapshotV1,
) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS snapshot_count
            FROM entry_classification_snapshots
            WHERE {_lineage_predicate()}
            """,
            _lineage_values(lineage),
        ).fetchone()
    return int(row["snapshot_count"] if row is not None else 0)


def count_entry_classification_command_receipts(
    lineage: EntryClassificationSnapshotV1,
) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS receipt_count
            FROM entry_classification_command_receipts
            WHERE {_lineage_predicate()}
            """,
            _lineage_values(lineage),
        ).fetchone()
    return int(row["receipt_count"] if row is not None else 0)


def get_entry_classification_command_result(
    lineage: EntryClassificationSnapshotV1,
    *,
    command_id: str,
    command_sha256: str,
) -> EntryClassificationSnapshotV1 | None:
    """Resolve an exact prior command before attempting a new transition."""

    _validate_payload_identity(lineage)
    normalized_command_id = _validate_command_identity(
        command_id,
        command_sha256,
    )
    with db.get_conn() as conn:
        return _command_result_in_connection(
            conn,
            lineage=lineage,
            command_id=normalized_command_id,
            command_sha256=command_sha256,
        )


def is_entry_classification_command_receipt(
    lineage: EntryClassificationSnapshotV1,
    *,
    command_id: str,
    command_sha256: str,
) -> bool:
    """Return whether an already-validated command is a no-op receipt."""

    normalized_command_id = _validate_command_identity(
        command_id,
        command_sha256,
    )
    with db.get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM entry_classification_command_receipts
            WHERE {_lineage_predicate()} AND command_id = ?
            LIMIT 1
            """,
            (*_lineage_values(lineage), normalized_command_id),
        ).fetchone()
    if row is None:
        return False
    if str(row["command_sha256"] or "") != command_sha256:
        raise EntryClassificationCommandConflict(
            "command_id was already used with a different command hash"
        )
    _decode_command_receipt(row, lineage=lineage)
    return True


def record_entry_classification_command_receipt(
    *,
    current_snapshot: EntryClassificationSnapshotV1,
    command_id: str,
    command_sha256: str,
    expected_revision: int,
    expected_snapshot_sha256: str,
) -> tuple[EntryClassificationSnapshotV1, bool]:
    """Record an idempotent no-op command without a business revision."""

    normalized_command_id = _validate_command_identity(
        command_id,
        command_sha256,
    )
    if isinstance(expected_revision, bool) or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    if not _SHA256_RE.fullmatch(str(expected_snapshot_sha256 or "")):
        raise ValueError("expected_snapshot_sha256 must be a lowercase SHA-256")
    _validate_payload_identity(current_snapshot)
    with _SNAPSHOT_LOCK, db.get_conn() as conn:
        _lock_entry_classification_lineage(conn, current_snapshot)
        replay = _command_result_in_connection(
            conn,
            lineage=current_snapshot,
            command_id=normalized_command_id,
            command_sha256=command_sha256,
        )
        if replay is not None:
            return replay, False
        persisted_head = _select_head(conn, current_snapshot)
        if persisted_head is None:
            if current_snapshot.revision != 0:
                raise EntryClassificationStaleWrite(
                    "classification lineage has no persisted base"
                )
            current = current_snapshot
        else:
            current = _decode_snapshot(persisted_head)
            if (
                current.snapshot_id != current_snapshot.snapshot_id
                or current.payload_sha256 != current_snapshot.payload_sha256
            ):
                raise EntryClassificationStaleWrite(
                    "classification base is not the current persisted head"
                )
        if (
            current.revision != expected_revision
            or current.payload_sha256 != expected_snapshot_sha256
        ):
            raise EntryClassificationStaleWrite(
                "classification snapshot changed; refresh before retrying"
            )
        receipt_values = {
            "id": str(uuid.uuid4()),
            "project_id": current.project_id,
            "sor_document_id": current.sor_document_id,
            "source_sha256": current.source_sha256,
            "working_tree_id": current.working_tree_id,
            "working_tree_sha256": current.working_tree_sha256,
            "effective_structure_id": current.effective_structure_id,
            "semantic_snapshot_id": current.semantic_snapshot_id,
            "policy_version": current.policy_version,
            "command_id": normalized_command_id,
            "command_sha256": command_sha256,
            "result_snapshot_id": current.snapshot_id,
            "result_snapshot_sha256": current.payload_sha256,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_values["envelope_sha256"] = (
            _canonical_command_receipt_envelope_sha256(receipt_values)
        )
        conn.execute(
            """
            INSERT INTO entry_classification_command_receipts (
                id, project_id, sor_document_id, source_sha256,
                working_tree_id, working_tree_sha256, effective_structure_id,
                semantic_snapshot_id, policy_version, command_id,
                command_sha256, result_snapshot_id, result_snapshot_sha256,
                created_at, envelope_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                receipt_values[field]
                for field in (
                    *_COMMAND_RECEIPT_ENVELOPE_FIELDS,
                    "envelope_sha256",
                )
            ),
        )
    return current, True


def append_entry_classification_snapshot(
    *,
    base_snapshot: EntryClassificationSnapshotV1,
    next_snapshot: EntryClassificationSnapshotV1,
    command_id: str,
    command_sha256: str,
    expected_revision: int,
    expected_snapshot_sha256: str,
) -> tuple[EntryClassificationSnapshotV1, bool]:
    """Append one exact-lineage revision with replay-safe optimistic locking."""

    normalized_command_id = _validate_command_identity(
        command_id,
        command_sha256,
    )
    if isinstance(expected_revision, bool) or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    if not _SHA256_RE.fullmatch(str(expected_snapshot_sha256 or "")):
        raise ValueError("expected_snapshot_sha256 must be a lowercase SHA-256")
    _assert_same_lineage(base_snapshot, next_snapshot)
    _validate_payload_identity(base_snapshot)
    _validate_payload_identity(next_snapshot)

    with _SNAPSHOT_LOCK, db.get_conn() as conn:
        _lock_entry_classification_lineage(conn, next_snapshot)
        replay = _command_result_in_connection(
            conn,
            lineage=base_snapshot,
            command_id=normalized_command_id,
            command_sha256=command_sha256,
        )
        if replay is not None:
            return replay, False

        current_row = _select_head(conn, next_snapshot)
        if current_row is None:
            if base_snapshot.revision != 0:
                raise EntryClassificationStaleWrite(
                    "classification lineage has no persisted base"
                )
            current = base_snapshot
        else:
            current = _decode_snapshot(current_row)
            if (
                current.snapshot_id != base_snapshot.snapshot_id
                or current.payload_sha256 != base_snapshot.payload_sha256
            ):
                raise EntryClassificationStaleWrite(
                    "classification base is not the current persisted head"
                )
        if (
            current.revision != expected_revision
            or current.payload_sha256 != expected_snapshot_sha256
        ):
            raise EntryClassificationStaleWrite(
                "classification snapshot changed; refresh before retrying"
            )
        if (
            next_snapshot.revision != current.revision + 1
            or next_snapshot.supersedes_id != current.snapshot_id
        ):
            raise EntryClassificationStaleWrite(
                "classification revision does not extend the current head"
            )

        payload_bytes = canonical_entry_classification_payload_bytes(next_snapshot)
        envelope_values = {
            "id": next_snapshot.snapshot_id,
            "project_id": next_snapshot.project_id,
            "sor_document_id": next_snapshot.sor_document_id,
            "source_sha256": next_snapshot.source_sha256,
            "working_tree_id": next_snapshot.working_tree_id,
            "working_tree_sha256": next_snapshot.working_tree_sha256,
            "effective_structure_id": next_snapshot.effective_structure_id,
            "semantic_snapshot_id": next_snapshot.semantic_snapshot_id,
            "policy_version": next_snapshot.policy_version,
            "revision": next_snapshot.revision,
            "status": next_snapshot.status.value,
            "supersedes_id": next_snapshot.supersedes_id,
            "command_id": normalized_command_id,
            "command_sha256": command_sha256,
            "payload_sha256": next_snapshot.payload_sha256,
            "created_at": next_snapshot.created_at,
            "accepted_at": next_snapshot.accepted_at,
            "accepted_by": next_snapshot.accepted_by,
        }
        envelope_sha256 = _canonical_envelope_sha256(envelope_values)
        conn.execute(
            """
            INSERT INTO entry_classification_snapshots (
                id, project_id, sor_document_id, source_sha256,
                working_tree_id, working_tree_sha256, effective_structure_id,
                semantic_snapshot_id, policy_version, revision, status,
                supersedes_id, command_id, command_sha256, payload_json,
                payload_sha256, envelope_sha256, created_at, accepted_at,
                accepted_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_snapshot.snapshot_id,
                *_lineage_values(next_snapshot),
                next_snapshot.revision,
                next_snapshot.status.value,
                next_snapshot.supersedes_id,
                normalized_command_id,
                command_sha256,
                payload_bytes.decode("utf-8"),
                next_snapshot.payload_sha256,
                envelope_sha256,
                next_snapshot.created_at,
                next_snapshot.accepted_at,
                next_snapshot.accepted_by,
            ),
        )
        inserted = conn.execute(
            "SELECT * FROM entry_classification_snapshots WHERE id = ?",
            (next_snapshot.snapshot_id,),
        ).fetchone()
    if inserted is None:
        raise RuntimeError("entry classification snapshot was not persisted")
    return _decode_snapshot(inserted), True


def _select_head(conn: Any, lineage: EntryClassificationSnapshotV1):
    return conn.execute(
        f"""
        SELECT *
        FROM entry_classification_snapshots
        WHERE {_lineage_predicate()}
        ORDER BY revision DESC
        LIMIT 1
        """,
        _lineage_values(lineage),
    ).fetchone()


def _command_result_in_connection(
    conn: Any,
    *,
    lineage: EntryClassificationSnapshotV1,
    command_id: str,
    command_sha256: str,
) -> EntryClassificationSnapshotV1 | None:
    snapshot_row = conn.execute(
        f"""
        SELECT *
        FROM entry_classification_snapshots
        WHERE {_lineage_predicate()} AND command_id = ?
        LIMIT 1
        """,
        (*_lineage_values(lineage), command_id),
    ).fetchone()
    receipt_row = conn.execute(
        f"""
        SELECT *
        FROM entry_classification_command_receipts
        WHERE {_lineage_predicate()} AND command_id = ?
        LIMIT 1
        """,
        (*_lineage_values(lineage), command_id),
    ).fetchone()
    if snapshot_row is not None and receipt_row is not None:
        raise EntryClassificationSnapshotCorrupted(
            "classification command exists as both a revision and a receipt"
        )
    row = snapshot_row if snapshot_row is not None else receipt_row
    if row is None:
        return None
    if str(row["command_sha256"] or "") != command_sha256:
        raise EntryClassificationCommandConflict(
            "command_id was already used with a different command hash"
        )
    if snapshot_row is not None:
        return _decode_snapshot(snapshot_row)
    receipt = _decode_command_receipt(receipt_row, lineage=lineage)
    result_snapshot_id = str(receipt["result_snapshot_id"] or "")
    result_snapshot_sha256 = str(receipt["result_snapshot_sha256"] or "")
    if (
        result_snapshot_id == lineage.snapshot_id
        and result_snapshot_sha256 == lineage.payload_sha256
    ):
        return lineage
    result_row = conn.execute(
        f"""
        SELECT *
        FROM entry_classification_snapshots
        WHERE {_lineage_predicate()} AND id = ?
        LIMIT 1
        """,
        (*_lineage_values(lineage), result_snapshot_id),
    ).fetchone()
    if result_row is None:
        raise EntryClassificationSnapshotCorrupted(
            "classification command receipt result is missing"
        )
    result = _decode_snapshot(result_row)
    if result.payload_sha256 != result_snapshot_sha256:
        raise EntryClassificationSnapshotCorrupted(
            "classification command receipt result hash does not match"
        )
    return result


def _decode_command_receipt(
    row: Any,
    *,
    lineage: EntryClassificationSnapshotV1,
) -> dict[str, str]:
    values = {
        field: str(row[field] or "")
        for field in _COMMAND_RECEIPT_ENVELOPE_FIELDS
    }
    envelope_sha256 = str(row["envelope_sha256"] or "")
    if (
        not _SHA256_RE.fullmatch(envelope_sha256)
        or _canonical_command_receipt_envelope_sha256(values)
        != envelope_sha256
        or tuple(values[field] for field in _LINEAGE_FIELDS)
        != _lineage_values(lineage)
        or not _SHA256_RE.fullmatch(values["command_sha256"])
        or not _SHA256_RE.fullmatch(values["result_snapshot_sha256"])
        or not values["id"]
        or not values["command_id"]
        or not values["result_snapshot_id"]
        or not values["created_at"]
    ):
        raise EntryClassificationSnapshotCorrupted(
            "stored classification command receipt is invalid"
        )
    return values


def _decode_snapshot(row: Any) -> EntryClassificationSnapshotV1:
    envelope_sha256 = str(row["envelope_sha256"] or "")
    envelope_values = {field: row[field] for field in _ENVELOPE_FIELDS}
    if (
        not _SHA256_RE.fullmatch(envelope_sha256)
        or _canonical_envelope_sha256(envelope_values) != envelope_sha256
    ):
        raise EntryClassificationSnapshotCorrupted(
            "stored classification persistence envelope does not match"
        )
    payload_json = str(row["payload_json"] or "")
    payload_sha256 = str(row["payload_sha256"] or "")
    actual_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if actual_sha256 != payload_sha256:
        raise EntryClassificationSnapshotCorrupted(
            "stored classification payload hash does not match"
        )
    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise TypeError("classification payload must be an object")
        snapshot = EntryClassificationSnapshotV1.model_validate(
            {**payload, "payload_sha256": payload_sha256}
        )
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise EntryClassificationSnapshotCorrupted(
            "stored classification payload is invalid"
        ) from exc
    if canonical_entry_classification_payload_bytes(snapshot).decode("utf-8") != payload_json:
        raise EntryClassificationSnapshotCorrupted(
            "stored classification payload is not canonical"
        )
    _validate_payload_identity(snapshot)
    expected_row_values = (
        snapshot.snapshot_id,
        *_lineage_values(snapshot),
        snapshot.revision,
        snapshot.status.value,
        snapshot.supersedes_id,
        snapshot.created_at,
        snapshot.accepted_at,
        snapshot.accepted_by,
    )
    actual_row_values = (
        str(row["id"] or ""),
        str(row["project_id"] or ""),
        str(row["sor_document_id"] or ""),
        str(row["source_sha256"] or ""),
        str(row["working_tree_id"] or ""),
        str(row["working_tree_sha256"] or ""),
        str(row["effective_structure_id"] or ""),
        str(row["semantic_snapshot_id"] or ""),
        str(row["policy_version"] or ""),
        int(row["revision"] or 0),
        str(row["status"] or ""),
        str(row["supersedes_id"] or ""),
        str(row["created_at"] or ""),
        str(row["accepted_at"] or ""),
        str(row["accepted_by"] or ""),
    )
    if actual_row_values != expected_row_values:
        raise EntryClassificationSnapshotCorrupted(
            "stored classification envelope disagrees with its payload"
        )
    return snapshot


def _validate_payload_identity(snapshot: EntryClassificationSnapshotV1) -> None:
    if canonical_entry_classification_payload_sha256(snapshot) != snapshot.payload_sha256:
        raise EntryClassificationSnapshotCorrupted(
            "classification payload SHA-256 is invalid"
        )


def _canonical_envelope_sha256(values: dict[str, Any]) -> str:
    envelope = {
        "schema_version": _ENVELOPE_SCHEMA_VERSION,
        **{
            field: (
                int(values[field] or 0)
                if field == "revision"
                else str(values[field] or "")
            )
            for field in _ENVELOPE_FIELDS
        },
    }
    return hashlib.sha256(
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_command_receipt_envelope_sha256(
    values: dict[str, Any],
) -> str:
    envelope = {
        "schema_version": _COMMAND_RECEIPT_ENVELOPE_VERSION,
        **{
            field: str(values[field] or "")
            for field in _COMMAND_RECEIPT_ENVELOPE_FIELDS
        },
    }
    return hashlib.sha256(
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_command_identity(command_id: str, command_sha256: str) -> str:
    normalized_command_id = str(command_id or "").strip()
    if not _COMMAND_ID_RE.fullmatch(normalized_command_id):
        raise ValueError("command_id must be a safe bounded identifier")
    if not _SHA256_RE.fullmatch(str(command_sha256 or "")):
        raise ValueError("command_sha256 must be a lowercase SHA-256")
    return normalized_command_id


def _assert_same_lineage(
    first: EntryClassificationSnapshotV1,
    second: EntryClassificationSnapshotV1,
) -> None:
    if _lineage_values(first) != _lineage_values(second):
        raise ValueError("classification revisions cannot cross lineages")


def _lineage_predicate() -> str:
    return """
        project_id = ? AND sor_document_id = ? AND source_sha256 = ?
        AND working_tree_id = ? AND working_tree_sha256 = ?
        AND effective_structure_id = ? AND semantic_snapshot_id = ?
        AND policy_version = ?
    """


def _lineage_values(
    snapshot: EntryClassificationSnapshotV1,
) -> tuple[str, ...]:
    return (
        snapshot.project_id,
        snapshot.sor_document_id,
        snapshot.source_sha256,
        snapshot.working_tree_id,
        snapshot.working_tree_sha256,
        snapshot.effective_structure_id,
        snapshot.semantic_snapshot_id,
        snapshot.policy_version,
    )


__all__ = [
    "EntryClassificationCommandConflict",
    "EntryClassificationSnapshotCorrupted",
    "EntryClassificationStaleWrite",
    "_lock_entry_classification_lineage",
    "append_entry_classification_snapshot",
    "count_entry_classification_command_receipts",
    "count_entry_classification_snapshots",
    "get_entry_classification_command_result",
    "get_entry_classification_snapshot_by_id",
    "get_entry_classification_snapshot_or_initial",
    "get_latest_entry_classification_snapshot_for_document",
    "entry_classification_lineage_values",
    "is_entry_classification_command_receipt",
    "record_entry_classification_command_receipt",
]
