"""Append-only SQLite persistence for validated SourceIR snapshots.

This adapter owns only its dedicated ``source_ir_snapshots`` table. It does
not depend on, migrate, or write the product's existing business tables.
Source files and asset bytes remain in their owning stores; the repository
persists only the SourceIR JSON contract and indexed identity metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
from typing import Iterator

from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceIRStatus
from reqpilotx_server.ports.extraction_repository import SourceIRConflictError


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS source_ir_snapshots (
        source_ir_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
        schema_version TEXT NOT NULL,
        adapter_id TEXT NOT NULL,
        adapter_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('COMPLETE', 'PARTIAL')),
        created_at_utc TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
        payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_source_ir_snapshots_document
    ON source_ir_snapshots(document_id, created_at_utc, source_ir_id)
    """,
)


class SQLiteSourceIRRepository:
    """SQLite implementation of the immutable SourceIR repository port.

    Pass either an explicit database path or a caller-owned ``Connection``.
    Path-backed operations use short-lived connections and are safe to call
    concurrently. A caller-owned connection is never closed and an existing
    caller transaction is never committed by this adapter.
    """

    def __init__(
        self,
        database: sqlite3.Connection | str | os.PathLike[str],
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._timeout_seconds = float(timeout_seconds)
        self._connection: sqlite3.Connection | None = None
        self._database_path: str | None = None
        self._external_lock = threading.RLock()

        if isinstance(database, sqlite3.Connection):
            self._connection = database
        else:
            database_path = os.fspath(database)
            if not database_path.strip():
                raise ValueError("database path must not be blank")
            if database_path == ":memory:":
                raise ValueError(
                    "pass a sqlite3.Connection for an in-memory SourceIR repository"
                )
            self._database_path = str(Path(database_path))

        with self._connect() as connection:
            with self._transaction(connection, immediate=True):
                self._ensure_schema(connection)

    def append(self, snapshot: SourceIRSnapshot) -> bool:
        if not isinstance(snapshot, SourceIRSnapshot):
            raise TypeError("snapshot must be a SourceIRSnapshot")
        if snapshot.status is SourceIRStatus.FAILED:
            raise ValueError(
                "FAILED SourceIR snapshots are attempt outcomes and cannot enter "
                "the canonical append-only repository"
            )

        payload_json = _canonical_snapshot_json(snapshot)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        created_at_utc = snapshot.created_at.astimezone(timezone.utc).isoformat()

        with self._connect() as connection:
            with self._transaction(connection, immediate=True):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO source_ir_snapshots (
                        source_ir_id, document_id, source_sha256, schema_version,
                        adapter_id, adapter_version, status, created_at_utc,
                        payload_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.source_ir_id,
                        snapshot.document_id,
                        snapshot.source_sha256,
                        snapshot.schema_version,
                        snapshot.adapter_id,
                        snapshot.adapter_version,
                        snapshot.status.value,
                        created_at_utc,
                        payload_sha256,
                        payload_json,
                    ),
                )
                if cursor.rowcount == 1:
                    return True

                existing = connection.execute(
                    """
                    SELECT payload_sha256, payload_json
                    FROM source_ir_snapshots
                    WHERE source_ir_id = ?
                    """,
                    (snapshot.source_ir_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError(
                        "SourceIR insert was ignored without an existing identity"
                    )
                if existing[0] == payload_sha256 and existing[1] == payload_json:
                    return False
                # ``created_at`` is observation metadata, not part of the
                # deterministic fact identity. A replay of the same source and
                # adapter may occur later and must stay idempotent; every other
                # payload difference still exposes an adapter-version conflict.
                existing_snapshot = SourceIRSnapshot.model_validate_json(
                    str(existing[1])
                )
                if _canonical_snapshot_facts_json(
                    existing_snapshot
                ) == _canonical_snapshot_facts_json(snapshot):
                    return False
                raise SourceIRConflictError(
                    f"source_ir_id {snapshot.source_ir_id!r} already has a different payload"
                )

    def get(self, source_ir_id: str) -> SourceIRSnapshot | None:
        source_ir_id = _require_non_blank(source_ir_id, "source_ir_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_ir_id, document_id, payload_sha256, payload_json
                FROM source_ir_snapshots
                WHERE source_ir_id = ?
                """,
                (source_ir_id,),
            ).fetchone()
        if row is None:
            return None
        return _decode_snapshot(row)

    def list_by_document_id(self, document_id: str) -> tuple[SourceIRSnapshot, ...]:
        document_id = _require_non_blank(document_id, "document_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_ir_id, document_id, payload_sha256, payload_json
                FROM source_ir_snapshots
                WHERE document_id = ?
                ORDER BY created_at_utc ASC, source_ir_id ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(_decode_snapshot(row) for row in rows)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            # sqlite3 connections are not necessarily thread-safe. Serializing
            # access also makes the caller-owned transaction boundary explicit.
            with self._external_lock:
                yield self._connection
            return

        assert self._database_path is not None
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(self._timeout_seconds * 1000)}")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(
        self,
        connection: sqlite3.Connection,
        *,
        immediate: bool,
    ) -> Iterator[None]:
        if connection.in_transaction:
            savepoint = f"source_ir_repo_{id(self):x}"
            connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return

        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)


def _canonical_snapshot_json(snapshot: SourceIRSnapshot) -> str:
    return json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_snapshot_facts_json(snapshot: SourceIRSnapshot) -> str:
    return json.dumps(
        snapshot.model_dump(mode="json", exclude={"created_at"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_snapshot(row: sqlite3.Row | tuple[object, ...]) -> SourceIRSnapshot:
    source_ir_id = str(row[0])
    document_id = str(row[1])
    expected_payload_sha256 = str(row[2])
    payload_json = str(row[3])
    actual_payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if actual_payload_sha256 != expected_payload_sha256:
        raise RuntimeError(f"stored SourceIR payload hash mismatch for {source_ir_id!r}")
    snapshot = SourceIRSnapshot.model_validate_json(payload_json)
    if snapshot.source_ir_id != source_ir_id or snapshot.document_id != document_id:
        raise RuntimeError(f"stored SourceIR identity metadata mismatch for {source_ir_id!r}")
    return snapshot


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


__all__ = ["SQLiteSourceIRRepository"]
