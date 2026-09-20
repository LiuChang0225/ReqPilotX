from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any


_PRAGMA_TABLE_INFO_RE = re.compile(
    r"^\s*PRAGMA\s+table_info\((?P<table>[A-Za-z_][A-Za-z0-9_]*)\)\s*;?\s*$",
    re.IGNORECASE,
)
_ROWID_RE = re.compile(r"\browid\b", re.IGNORECASE)
_FILE_PATH_RE = re.compile(r"\bfile_path\b", re.IGNORECASE)
_PAYLOAD_PATH_RE = re.compile(r"\bpayload_path\b", re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(
    r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b",
    re.IGNORECASE,
)


class CompatRow(Mapping[str, Any]):
    """Mapping row that also supports sqlite-style positional indexing."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)
        self._columns = tuple(self._values)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[self._columns[key]]
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def keys(self):
        return self._values.keys()


class PostgresCompatCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def fetchone(self) -> CompatRow | None:
        row = self._cursor.fetchone()
        return _compat_row(row)

    def fetchall(self) -> list[CompatRow]:
        return [
            converted
            for row in self._cursor.fetchall()
            if (converted := _compat_row(row)) is not None
        ]

    def __iter__(self) -> Iterator[CompatRow]:
        for row in self._cursor:
            converted = _compat_row(row)
            if converted is not None:
                yield converted


class PostgresCompatConnection:
    """Small DB-API compatibility layer for the existing repository surface.

    It exists only to keep one business behavior implementation while the
    repository modules are split by domain. It does not emulate SQLite storage
    or schema mutation.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def in_transaction(self) -> bool:
        try:
            from psycopg.pq import TransactionStatus

            return (
                self._connection.info.transaction_status
                is not TransactionStatus.IDLE
            )
        except (ImportError, AttributeError):
            return False

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | None = None,
    ) -> PostgresCompatCursor:
        statement = str(sql)
        pragma = _PRAGMA_TABLE_INFO_RE.match(statement)
        if pragma:
            cursor = self._connection.execute(
                """
                SELECT
                    ordinal_position - 1 AS cid,
                    column_name AS name,
                    data_type AS type,
                    CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
                    column_default AS dflt_value,
                    0 AS pk
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (pragma.group("table"),),
            )
            return PostgresCompatCursor(cursor)

        translated = translate_sql_for_postgres(statement)
        cursor = self._connection.execute(translated, tuple(parameters or ()))
        return PostgresCompatCursor(cursor)

    def executemany(
        self,
        sql: str,
        parameters: Iterable[Sequence[Any]],
    ) -> PostgresCompatCursor:
        cursor = self._connection.cursor()
        cursor.executemany(
            translate_sql_for_postgres(str(sql)),
            [tuple(values) for values in parameters],
        )
        return PostgresCompatCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        # Pool ownership remains with PostgresDatabase.
        return None


def translate_sql_for_postgres(sql: str) -> str:
    normalized = sql.strip()
    if normalized.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    translated = _question_marks_to_psycopg(sql)
    translated = _ROWID_RE.sub("_row_order", translated)
    translated = _FILE_PATH_RE.sub("storage_key", translated)
    translated = _PAYLOAD_PATH_RE.sub("payload_storage_key", translated)
    if _INSERT_OR_IGNORE_RE.match(translated):
        translated = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", translated, count=1)
        stripped = translated.rstrip().rstrip(";")
        if re.search(r"\bRETURNING\b", stripped, re.IGNORECASE):
            head, returning = re.split(
                r"\bRETURNING\b",
                stripped,
                maxsplit=1,
                flags=re.IGNORECASE,
            )
            translated = f"{head} ON CONFLICT DO NOTHING RETURNING {returning}"
        else:
            translated = f"{stripped} ON CONFLICT DO NOTHING"
    return translated


def _question_marks_to_psycopg(sql: str) -> str:
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _compat_row(row: Any | None) -> CompatRow | None:
    if row is None:
        return None
    if isinstance(row, CompatRow):
        return row
    if isinstance(row, Mapping):
        return CompatRow(row)
    raise TypeError("PostgreSQL runtime requires mapping rows.")


__all__ = [
    "CompatRow",
    "PostgresCompatConnection",
    "PostgresCompatCursor",
    "translate_sql_for_postgres",
]
