"""SQLite-only adapter for isolated tests and one-time migration verification."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.adapters.persistence.sql_llm_requirement_reviews import (
    SqlLlmRequirementReviewRepository,
    _CONFIRMATION_EVENT_INDEX_SCHEMA,
    _CONFIRMATION_EVENT_SCHEMA,
    _EVENT_SCHEMA,
    _INDEX_SCHEMA,
    _SESSION_SCHEMA,
)


class SQLiteLlmRequirementReviewRepository(SqlLlmRequirementReviewRepository):
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
                    "pass a sqlite3.Connection for an in-memory repository"
                )
            self._database_path = str(Path(path))
        with self._connect() as connection:
            with self._transaction(connection):
                connection.execute(_SESSION_SCHEMA)
                connection.execute(_EVENT_SCHEMA)
                connection.execute(_CONFIRMATION_EVENT_SCHEMA)
                connection.execute(_INDEX_SCHEMA)
                connection.execute(_CONFIRMATION_EVENT_INDEX_SCHEMA)

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


__all__ = ["SQLiteLlmRequirementReviewRepository"]
