"""Append-only persistence for text-free semantic provider run metadata."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.domain.extraction.semantic_provider import SemanticProviderRunRecord
from app.ports.semantic_run_repository import SemanticProviderRunConflictError


_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_provider_runs (
    provider_run_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    response_sha256 TEXT,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL
)
"""


class SQLiteSemanticProviderRunRepository:
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
        self._lock = threading.RLock()
        if isinstance(database, sqlite3.Connection):
            self._connection = database
        else:
            path = os.fspath(database)
            if not path.strip():
                raise ValueError("database path must not be blank")
            if path == ":memory:":
                raise ValueError("pass a sqlite3.Connection for an in-memory repository")
            self._database_path = str(Path(path))
        with self._connect() as connection:
            with self._transaction(connection):
                connection.execute(_SCHEMA)

    def append(self, run: SemanticProviderRunRecord) -> bool:
        if not isinstance(run, SemanticProviderRunRecord):
            raise TypeError("run must be a SemanticProviderRunRecord")
        payload_json = json.dumps(
            run.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        with self._connect() as connection:
            with self._transaction(connection):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO semantic_provider_runs (
                        provider_run_id, provider_id, model_id, operation, status,
                        started_at_utc, request_sha256, response_sha256,
                        payload_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.provider_run_id,
                        run.provider_id,
                        run.model_id,
                        run.operation,
                        run.status.value,
                        run.started_at.isoformat(),
                        run.request_sha256,
                        run.response_sha256,
                        payload_sha256,
                        payload_json,
                    ),
                )
                if cursor.rowcount == 1:
                    return True
                row = connection.execute(
                    "SELECT payload_sha256, payload_json FROM semantic_provider_runs WHERE provider_run_id = ?",
                    (run.provider_run_id,),
                ).fetchone()
                if row is not None and row[0] == payload_sha256 and row[1] == payload_json:
                    return False
                if row is not None and self._deserialize_stored_run(
                    payload_sha256=str(row[0]),
                    payload_json=str(row[1]),
                ) == run:
                    return False
                raise SemanticProviderRunConflictError(
                    "provider_run_id already contains different audit metadata"
                )

    def get(self, provider_run_id: str) -> SemanticProviderRunRecord | None:
        if not isinstance(provider_run_id, str) or not provider_run_id.strip():
            raise ValueError("provider_run_id must not be blank")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_sha256, payload_json FROM semantic_provider_runs WHERE provider_run_id = ?",
                (provider_run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._deserialize_stored_run(
            payload_sha256=str(row[0]),
            payload_json=str(row[1]),
        )

    @staticmethod
    def _deserialize_stored_run(
        *,
        payload_sha256: str,
        payload_json: str,
    ) -> SemanticProviderRunRecord:
        if hashlib.sha256(payload_json.encode()).hexdigest() != payload_sha256:
            raise RuntimeError("stored semantic provider run payload hash mismatch")
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise RuntimeError("stored semantic provider run payload is invalid")
        if payload.get("schema_version") == "semantic-provider-run/v1":
            payload = {**payload, "schema_version": "semantic-provider-run/v2"}
        return SemanticProviderRunRecord.model_validate(payload)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            with self._lock:
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
    def _transaction(self, connection: sqlite3.Connection) -> Iterator[None]:
        if connection.in_transaction:
            name = f"semantic_run_repo_{id(self):x}"
            connection.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException:
                connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                connection.execute(f"RELEASE SAVEPOINT {name}")
                raise
            else:
                connection.execute(f"RELEASE SAVEPOINT {name}")
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


__all__ = ["SQLiteSemanticProviderRunRepository"]
