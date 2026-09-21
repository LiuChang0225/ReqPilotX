from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from reqpilotx_server.adapters.persistence.sql_requirement_enrichment import (
    SqlRequirementEnrichmentRepository,
)
from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.infrastructure.postgres_compat import PostgresCompatConnection


class PostgresRequirementEnrichmentRepository(
    SqlRequirementEnrichmentRepository
):
    """PostgreSQL runtime adapter for atomization/refinement publication."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database
        self._timeout_seconds = 30.0
        self._connection = None
        self._database_path = None
        self._lock = threading.RLock()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        with self._database.connection() as connection:
            yield PostgresCompatConnection(connection)


__all__ = ["PostgresRequirementEnrichmentRepository"]
