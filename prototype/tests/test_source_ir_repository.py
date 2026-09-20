from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.persistence.sqlite_extraction import SQLiteSourceIRRepository
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.ports.extraction_repository import (
    SourceIRConflictError,
    SourceIRRepositoryPort,
)


class SQLiteSourceIRRepositoryTests(unittest.TestCase):
    def test_append_get_and_idempotent_replay_roundtrip_exact_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "source-ir.sqlite3"
            repository = SQLiteSourceIRRepository(database)
            snapshot = _snapshot(raw_text="系统应满足验收要求。")

            self.assertIsInstance(repository, SourceIRRepositoryPort)
            self.assertTrue(repository.append(snapshot))
            self.assertFalse(repository.append(snapshot))
            self.assertEqual(repository.get(snapshot.source_ir_id), snapshot)
            self.assertIsNone(repository.get("sir-does-not-exist"))

            # sqlite3.Connection's context manager commits/rolls back but does
            # not close the handle on Python 3.13.
            with closing(sqlite3.connect(database)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM source_ir_snapshots"
                ).fetchone()[0]
                stored_json = connection.execute(
                    "SELECT payload_json FROM source_ir_snapshots"
                ).fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(
                SourceIRSnapshot.model_validate(json.loads(stored_json)),
                snapshot,
            )

    def test_same_identity_with_different_payload_is_a_conflict(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSourceIRRepository(connection)
        first = _snapshot(raw_text="原始事实")
        conflicting = _snapshot(raw_text="被改写的事实")

        self.assertTrue(repository.append(first))
        with self.assertRaisesRegex(SourceIRConflictError, first.source_ir_id):
            repository.append(conflicting)
        self.assertEqual(repository.get(first.source_ir_id), first)

    def test_same_facts_replayed_at_a_later_time_are_idempotent(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSourceIRRepository(connection)
        first = _snapshot(created_at=datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc))
        replay = _snapshot(created_at=datetime(2026, 7, 21, 5, 0, tzinfo=timezone.utc))

        self.assertEqual(first.source_ir_id, replay.source_ir_id)
        self.assertTrue(repository.append(first))
        self.assertFalse(repository.append(replay))
        self.assertEqual(repository.get(first.source_ir_id), first)

    def test_failed_attempt_is_rejected_without_occupying_canonical_identity(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSourceIRRepository(connection)
        failed = _snapshot(status=SourceIRStatus.FAILED)
        complete = _snapshot(status=SourceIRStatus.COMPLETE)

        self.assertEqual(failed.source_ir_id, complete.source_ir_id)
        with self.assertRaisesRegex(ValueError, "FAILED.*canonical"):
            repository.append(failed)
        self.assertIsNone(repository.get(failed.source_ir_id))
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM source_ir_snapshots").fetchone()[0],
            0,
        )

        self.assertTrue(repository.append(complete))
        self.assertEqual(repository.get(complete.source_ir_id), complete)

    def test_list_by_document_is_filtered_and_deterministic(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSourceIRRepository(connection)
        later = _snapshot(
            adapter_version="2.0.0",
            created_at=datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc),
        )
        earlier = _snapshot(
            adapter_version="1.0.0",
            created_at=datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc),
        )
        other = _snapshot(source_sha256="b" * 64)

        repository.append(later)
        repository.append(other)
        repository.append(earlier)

        self.assertEqual(
            repository.list_by_document_id(earlier.document_id),
            (earlier, later),
        )
        self.assertEqual(repository.list_by_document_id("doc-missing"), ())

    def test_existing_caller_transaction_is_not_committed(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSourceIRRepository(connection)
        snapshot = _snapshot()

        connection.execute("BEGIN")
        self.assertTrue(repository.append(snapshot))
        self.assertTrue(connection.in_transaction)
        connection.rollback()

        self.assertIsNone(repository.get(snapshot.source_ir_id))

    def test_concurrent_identical_appends_insert_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "source-ir.sqlite3"
            SQLiteSourceIRRepository(database)
            snapshot = _snapshot()
            barrier = threading.Barrier(3)
            results: list[bool] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()

            def append_snapshot() -> None:
                try:
                    repository = SQLiteSourceIRRepository(database)
                    barrier.wait(timeout=5)
                    result = repository.append(snapshot)
                    with result_lock:
                        results.append(result)
                except BaseException as exc:  # pragma: no cover - asserted below
                    with result_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=append_snapshot) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(
                SQLiteSourceIRRepository(database).list_by_document_id(snapshot.document_id),
                (snapshot,),
            )

    def test_schema_has_no_blob_or_source_file_column(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        SQLiteSourceIRRepository(connection)

        columns = connection.execute(
            "PRAGMA table_info(source_ir_snapshots)"
        ).fetchall()
        names_and_types = {(str(row[1]), str(row[2]).upper()) for row in columns}
        self.assertNotIn("BLOB", {column_type for _, column_type in names_and_types})
        self.assertFalse(
            {"source_bytes", "asset_bytes", "source_file", "file_content"}
            & {name for name, _ in names_and_types}
        )


def _snapshot(
    *,
    source_sha256: str = "a" * 64,
    adapter_version: str = "1.0.0",
    raw_text: str = "系统应满足验收要求。",
    created_at: datetime = datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc),
    status: SourceIRStatus = SourceIRStatus.COMPLETE,
) -> SourceIRSnapshot:
    adapter_id = "native-pdf"
    document_id = stable_document_id(source_sha256)
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
    )
    node = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            kind=SourceNodeKind.PARAGRAPH,
            native_locator=locator,
        ),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=0,
        raw_text=raw_text,
        native_locator=locator,
    )
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=status,
        nodes=(node,),
        created_at=created_at,
    )


if __name__ == "__main__":
    unittest.main()
