"""Storage schema compatibility for immutable native delivery snapshots."""
import sqlite3


def ensure_delivery_versions_schema(connection):
    # PostgreSQL owns this table through migration 0037; only isolated SQLite
    # fixtures need its lightweight equivalent at first use.
    if isinstance(connection, sqlite3.Connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS source_item_delivery_versions (
          project_id TEXT NOT NULL, sor_document_id TEXT NOT NULL, revision INTEGER NOT NULL,
          command_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, artifact_json TEXT NOT NULL,
          artifact_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(project_id,sor_document_id,revision), UNIQUE(project_id,sor_document_id,command_id))""")
