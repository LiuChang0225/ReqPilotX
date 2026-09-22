"""SQLite-only enrichment adapter for isolated tests and migration checks."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from reqpilotx_server.adapters.persistence.sql_requirement_enrichment import (
    SqlRequirementEnrichmentRepository,
    _ATOMIZATION_CHECKPOINT_SCHEMA,
    _ATOMIZATION_SCHEMA,
    _ATOMIZATION_SCOPE_ATTEMPT_SCHEMA,
    _ATOMIZATION_SCOPE_SCHEMA,
    _PREVIEW_INDEX,
    _PUBLICATION_BINDING_PREVIEW_V2_INDEX,
    _PUBLICATION_BINDING_PREVIEW_V2_SCHEMA,
    _PUBLICATION_BINDING_V2_SCHEMA,
    _PUBLICATION_PREVIEW_SCHEMA,
    _PUBLICATION_ROW_BINDING_V2_SCHEMA,
    _PUBLICATION_SCHEMA,
    _REFINEMENT_EVENT_SCHEMA,
    _REFINEMENT_INDEX,
    _REFINEMENT_SCHEMA,
    _RUN_INDEX,
    _SCOPE_INDEX,
    _SCOPE_ATTEMPT_INDEX,
    _SCOPE_ATTEMPT_COMMAND_INDEX,
    _SCOPE_ATTEMPT_RUNNING_INDEX,
)
from reqpilotx_server.ports.requirement_enrichment_repository import (
    RequirementEnrichmentRepositoryError,
)


class SQLiteRequirementEnrichmentRepository(SqlRequirementEnrichmentRepository):
    """Bind the shared SQL repository to an explicit SQLite connection."""

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
                raise ValueError(
                    "pass sqlite3.Connection for an in-memory repository"
                )
            self._database_path = str(Path(path))
        with self._connect() as connection:
            _migrate_atomization_run_schema(connection)
            with self._transaction(connection):
                connection.execute(_ATOMIZATION_CHECKPOINT_SCHEMA)
                connection.execute(_ATOMIZATION_SCHEMA)
                connection.execute(_ATOMIZATION_SCOPE_SCHEMA)
                connection.execute(_ATOMIZATION_SCOPE_ATTEMPT_SCHEMA)
                connection.execute(_REFINEMENT_SCHEMA)
                connection.execute(_REFINEMENT_EVENT_SCHEMA)
                connection.execute(_PUBLICATION_PREVIEW_SCHEMA)
                connection.execute(_PUBLICATION_SCHEMA)
                connection.execute(_PUBLICATION_BINDING_PREVIEW_V2_SCHEMA)
                connection.execute(_PUBLICATION_BINDING_V2_SCHEMA)
                connection.execute(_PUBLICATION_ROW_BINDING_V2_SCHEMA)
                connection.execute(_RUN_INDEX)
                connection.execute(_SCOPE_INDEX)
                connection.execute(_SCOPE_ATTEMPT_INDEX)
                connection.execute(_SCOPE_ATTEMPT_COMMAND_INDEX)
                connection.execute(_SCOPE_ATTEMPT_RUNNING_INDEX)
                connection.execute(_REFINEMENT_INDEX)
                connection.execute(_PREVIEW_INDEX)
                connection.execute(_PUBLICATION_BINDING_PREVIEW_V2_INDEX)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            with self._lock:
                self._connection.row_factory = sqlite3.Row
                self._connection.execute("PRAGMA foreign_keys = ON")
                yield self._connection
            return
        assert self._database_path is not None
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                f"PRAGMA busy_timeout = {int(self._timeout_seconds * 1000)}"
            )
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()


def _migrate_atomization_run_schema(connection: sqlite3.Connection) -> None:
    """Preserve old test data while moving it to the current contract."""

    table = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'requirement_atomization_runs'
        """
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(requirement_atomization_runs)"
        ).fetchall()
    }
    if {"schema_version", "atomization_revision"}.issubset(columns):
        return
    if connection.in_transaction:
        raise RequirementEnrichmentRepositoryError(
            "legacy atomization schema migration requires an idle connection"
        )

    migration_table = "requirement_atomization_runs_v4_migration"
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(requirement_atomization_runs)"
            ).fetchall()
        }
        if {"schema_version", "atomization_revision"}.issubset(columns):
            connection.commit()
            return
        connection.execute(f"DROP TABLE IF EXISTS {migration_table}")
        connection.execute(
            _ATOMIZATION_SCHEMA.replace(
                "requirement_atomization_runs",
                migration_table,
                1,
            )
        )
        rows = connection.execute(
            "SELECT * FROM requirement_atomization_runs"
        ).fetchall()
        for row in rows:
            payload_json = str(row["payload_json"])
            schema_version = (
                str(row["schema_version"])
                if "schema_version" in columns
                else _stored_atomization_schema_version(payload_json)
            )
            atomization_revision = (
                int(row["atomization_revision"])
                if "atomization_revision" in columns
                else _stored_atomization_revision(payload_json)
            )
            connection.execute(
                f"""
                INSERT INTO {migration_table} (
                    atomization_run_id, schema_version, project_id, document_id,
                    session_id, review_revision, atomization_revision,
                    lineage_sha256, payload_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["atomization_run_id"]),
                    schema_version,
                    str(row["project_id"]),
                    str(row["document_id"]),
                    str(row["session_id"]),
                    int(row["review_revision"]),
                    atomization_revision,
                    str(row["lineage_sha256"]),
                    str(row["payload_sha256"]),
                    str(row["payload_json"]),
                    str(row["created_at_utc"]),
                ),
            )
        connection.execute("DROP TABLE requirement_atomization_runs")
        connection.execute(
            f"ALTER TABLE {migration_table} RENAME TO requirement_atomization_runs"
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RequirementEnrichmentRepositoryError(
                "atomization schema migration failed foreign-key validation"
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _stored_atomization_schema_version(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return "legacy-invalid"
    if not isinstance(payload, dict):
        return "legacy-invalid"
    schema_version = str(payload.get("schema_version") or "").strip()
    return schema_version if schema_version else "legacy-invalid"


def _stored_atomization_revision(payload_json: str) -> int:
    try:
        payload = json.loads(payload_json)
        revision = payload.get("revision", 0) if isinstance(payload, dict) else 0
        return (
            revision
            if isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
            else 0
        )
    except (json.JSONDecodeError, TypeError):
        return 0


__all__ = ["SQLiteRequirementEnrichmentRepository"]
