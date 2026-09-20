from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a text-free, read-only SQLite cutover inventory. "
            "This command never writes the source database."
        )
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = build_inventory(args.source)
        write_new_json(args.out, report)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        print(f"inventory failed: {type(exc).__name__}: {exc}")
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "table_count": report["table_count"],
                "row_count": report["row_count"],
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


def build_inventory(source: Path) -> dict[str, Any]:
    database = Path(source).expanduser().resolve(strict=True)
    if not database.is_file() or database.is_symlink():
        raise ValueError("source must be a regular SQLite database file.")
    source_sha256 = _file_sha256(database)
    uri = f"{database.as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        quick_check = [
            str(row[0])
            for row in connection.execute("PRAGMA quick_check").fetchall()
        ]
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        tables = [
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        table_reports = [
            _table_inventory(connection, table_name) for table_name in tables
        ]
    finally:
        connection.close()
    payload: dict[str, Any] = {
        "schema_version": "reqpilotx-sqlite-cutover-inventory/v1",
        "source_sha256": source_sha256,
        "source_size": database.stat().st_size,
        "quick_check": quick_check,
        "foreign_key_violation_count": len(foreign_key_violations),
        "table_count": len(table_reports),
        "row_count": sum(int(item["row_count"]) for item in table_reports),
        "tables": table_reports,
    }
    canonical = _canonical_json(payload)
    payload["report_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def _table_inventory(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, Any]:
    quoted = '"' + table_name.replace('"', '""') + '"'
    row_count = int(
        connection.execute(f"SELECT COUNT(*) AS count FROM {quoted}").fetchone()[0]
    )
    columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
    primary_key_columns = [
        str(row["name"])
        for row in sorted(columns, key=lambda item: int(item["pk"]))
        if int(row["pk"]) > 0
    ]
    primary_key_sha256 = hashlib.sha256()
    if primary_key_columns:
        select_columns = ", ".join(
            '"' + column.replace('"', '""') + '"' for column in primary_key_columns
        )
        order_columns = ", ".join(
            '"' + column.replace('"', '""') + '"' for column in primary_key_columns
        )
        for row in connection.execute(
            f"SELECT {select_columns} FROM {quoted} ORDER BY {order_columns}"
        ):
            primary_key_sha256.update(
                _canonical_json(list(row)).encode("utf-8")
            )
            primary_key_sha256.update(b"\n")
    return {
        "table": table_name,
        "row_count": row_count,
        "primary_key_columns": primary_key_columns,
        "primary_key_set_sha256": primary_key_sha256.hexdigest(),
    }


def write_new_json(target: Path, payload: dict[str, Any]) -> None:
    destination = Path(target).expanduser()
    if not destination.is_absolute():
        raise ValueError("out must be an absolute path.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination):
        raise ValueError("out already exists; inventories are immutable.")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
