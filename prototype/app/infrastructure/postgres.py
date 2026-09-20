from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PostgresUnavailableError(RuntimeError):
    """Raised when the target PostgreSQL runtime cannot be initialized."""


class MigrationDriftError(RuntimeError):
    """Raised when an already-applied migration no longer matches its checksum."""


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: str
    name: str
    sha256: str


class PostgresDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 8,
    ) -> None:
        if not str(database_url or "").strip():
            raise ValueError("database_url must not be blank.")
        if min_pool_size < 0 or max_pool_size < 1 or min_pool_size > max_pool_size:
            raise ValueError("Invalid PostgreSQL pool bounds.")
        self.database_url = str(database_url).strip()
        try:
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency boundary
            raise PostgresUnavailableError(
                "Install psycopg[binary,pool] to use PostgreSQL."
            ) from exc
        try:
            self._pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=min_pool_size,
                max_size=max_pool_size,
                kwargs={"row_factory": dict_row},
                open=False,
            )
            self._pool.open(wait=True)
        except Exception as exc:
            raise PostgresUnavailableError(
                "PostgreSQL is unavailable; verify REQPILOT_DATABASE_URL and the "
                "local/RDS service."
            ) from exc

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with self._pool.connection() as connection:
            yield connection

    def healthcheck(self) -> None:
        with self.connection() as connection:
            row = connection.execute("SELECT 1 AS ready").fetchone()
            if row is None or int(row["ready"]) != 1:
                raise PostgresUnavailableError("PostgreSQL healthcheck failed.")

    def apply_migrations(self, migration_dir: Path) -> tuple[AppliedMigration, ...]:
        root = Path(migration_dir)
        migrations = tuple(sorted(root.glob("*.sql")))
        if not migrations:
            raise FileNotFoundError(f"No PostgreSQL migrations found in {root}.")
        applied: list[AppliedMigration] = []
        with self.connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("reqpilotx-schema-migrations",),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            for path in migrations:
                version, name = _migration_identity(path)
                sql = path.read_text(encoding="utf-8")
                sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                existing = connection.execute(
                    "SELECT version, name, sha256 FROM schema_migrations WHERE version = %s",
                    (version,),
                ).fetchone()
                if existing is not None:
                    if str(existing["sha256"]) != sha256:
                        raise MigrationDriftError(
                            f"Migration {version} checksum changed after application."
                        )
                    applied.append(
                        AppliedMigration(
                            version=version,
                            name=str(existing["name"]),
                            sha256=sha256,
                        )
                    )
                    continue
                connection.execute(sql)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, sha256)
                    VALUES (%s, %s, %s)
                    """,
                    (version, name, sha256),
                )
                applied.append(
                    AppliedMigration(version=version, name=name, sha256=sha256)
                )
        return tuple(applied)

    def close(self) -> None:
        self._pool.close()


def _migration_identity(path: Path) -> tuple[str, str]:
    stem = path.stem
    version, separator, name = stem.partition("_")
    if not separator or not version.isdigit() or not name:
        raise ValueError(
            f"Migration names must use '<number>_<name>.sql': {path.name}"
        )
    return version, name


__all__ = [
    "AppliedMigration",
    "MigrationDriftError",
    "PostgresDatabase",
    "PostgresUnavailableError",
]
