"""Opt-in regression against a disposable local PostgreSQL schema."""

from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from starlette.datastructures import UploadFile

from reqpilotx_server import db
from reqpilotx_server.adapters.persistence.entry_commands import EntryCommandConflict
from reqpilotx_server.adapters.storage.local_object_store import LocalObjectStore
from reqpilotx_server.application import ingestion, runtime
from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend


@unittest.skipUnless(
    os.environ.get("REQPILOT_ENTRY_POSTGRES_URL"),
    "requires disposable local PostgreSQL",
)
class PostgresEntryCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        params = conninfo_to_dict(os.environ["REQPILOT_ENTRY_POSTGRES_URL"])
        host = params.get("host", "")
        if host not in {"", "localhost", "127.0.0.1", "::1"} and not host.startswith(
            "/"
        ):
            raise ValueError(
                "Entry regression only permits a disposable local PostgreSQL server."
            )
        cls.schema = "entry_test_" + uuid4().hex
        cls.admin = cls.stack.enter_context(
            psycopg.connect(make_conninfo(**params), autocommit=True)
        )
        cls.admin.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
        )
        cls.stack.callback(
            lambda: cls.admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
            )
        )
        dsn = make_conninfo(**{**params, "options": "-c search_path=" + cls.schema})
        cls.database = PostgresDatabase(dsn, min_pool_size=1, max_pool_size=6)
        cls.stack.callback(cls.database.close)
        migrations = (
            Path(__file__).resolve().parents[2]
            / "apps/server/src/reqpilotx_server/resources/migrations/postgresql"
        )
        cls.applied = cls.database.apply_migrations(migrations)
        cls.root = Path(
            cls.stack.enter_context(
                TemporaryDirectory(prefix="entry-postgres-objects-")
            )
        )
        settings = replace(
            runtime.RUNTIME_SETTINGS,
            database_backend=DatabaseBackend.POSTGRESQL,
            scratch_root=cls.root / "scratch",
        )
        cls.stack.enter_context(patch.object(db, "_RUNTIME_SETTINGS", settings))
        cls.stack.enter_context(patch.object(db, "_POSTGRES_DATABASE", cls.database))
        cls.stack.enter_context(patch.object(runtime, "RUNTIME_SETTINGS", settings))
        cls.store = LocalObjectStore(cls.root / "objects")
        cls.stack.enter_context(
            patch.object(
                runtime.RUNTIME_STATE,
                "container",
                SimpleNamespace(object_store=cls.store),
            )
        )

    def setUp(self):
        self.project = db.create_project("Postgres synthetic", "Synthetic")

    def upload(
        self, *, command_id=None, data=b"Synthetic source.\n", name="source.txt"
    ):
        upload = UploadFile(filename=name, file=io.BytesIO(data))
        return asyncio.run(
            ingestion.upload_sor_document(
                self.project["id"], upload, command_id=command_id
            )
        )

    def test_migrations_are_repeatable_and_unique_constraint_is_persistent(self):
        migrations = (
            Path(__file__).resolve().parents[2]
            / "apps/server/src/reqpilotx_server/resources/migrations/postgresql"
        )
        self.assertEqual(self.applied, self.database.apply_migrations(migrations))
        first = db.create_project("P", "C", command_id=uuid4().hex)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM entry_command_receipts WHERE resource_id = ?",
                (first["id"],),
            ).fetchone()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.database.connection() as conn:
                conn.execute(
                    """INSERT INTO entry_command_receipts(kind,scope_id,command_id,request_sha256,resource_id,result_json)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    tuple(
                        row[key]
                        for key in (
                            "kind",
                            "scope_id",
                            "command_id",
                            "request_sha256",
                            "resource_id",
                            "result_json",
                        )
                    ),
                )

    def test_concurrent_create_and_conflicting_payloads(self):
        command = uuid4().hex
        barrier = Barrier(4)

        def create(_):
            barrier.wait()
            return db.create_project("P", "C", command_id=command)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        with self.assertRaises(EntryCommandConflict):
            db.create_project("Changed", "C", command_id=command)
        self.assertEqual(
            db.get_entry_command_result(
                kind="project.create", scope="", command_id=command
            ),
            results[0],
        )

    def test_concurrent_pdf_import_and_lost_reply_keep_one_document_and_object(self):
        # Real PDF preflight; no document model or OCR is invoked.
        from pypdf import PdfWriter

        stream = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(stream)
        payload = stream.getvalue()
        command = uuid4().hex
        barrier = Barrier(4)

        def upload(_):
            barrier.wait()
            return self.upload(command_id=command, data=payload, name="source.pdf")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(upload, range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(db.list_sor_documents(self.project["id"])), 1)
        result = self.upload(command_id=command, data=payload, name="source.pdf")
        self.assertEqual(result, results[0])
        self.assertEqual(
            ingestion.get_document_import_command(self.project["id"], command), result
        )
        row = db.get_sor_document(result["id"])
        with self.store.open(row["storage_key"]) as source:
            self.assertEqual(source.read(), payload)

    def test_failed_writer_shared_key_and_committed_unknown_writer_do_not_delete_bytes(
        self,
    ):
        original = db.add_sor_document
        saved = []

        def competing_commit_then_fail(**kwargs):
            saved.append(
                original(**{**kwargs, "command_id": "competing-" + uuid4().hex})
            )
            raise RuntimeError("losing write")

        with patch.object(
            db, "add_sor_document", side_effect=competing_commit_then_fail
        ):
            with self.assertRaisesRegex(RuntimeError, "losing write"):
                self.upload(command_id="loser")
        self.assertIsNotNone(self.store.stat(saved[0]["storage_key"]))
        command = uuid4().hex

        def commit_then_fail(**kwargs):
            original(**kwargs)
            raise RuntimeError("lost acknowledgement")

        with patch.object(db, "add_sor_document", side_effect=commit_then_fail):
            with self.assertRaisesRegex(RuntimeError, "lost acknowledgement"):
                self.upload(command_id=command)
        recovered = ingestion.get_document_import_command(self.project["id"], command)
        self.assertEqual(self.upload(command_id=command), recovered)
        self.assertIsNotNone(self.store.stat(saved[0]["storage_key"]))

    def test_failed_receipt_completion_rolls_back_both_records(self):
        command = uuid4().hex
        with patch.object(
            db, "complete_receipt", side_effect=RuntimeError("failure before commit")
        ):
            with self.assertRaisesRegex(RuntimeError, "failure before commit"):
                db.create_project("P", "C", command_id=command)
        self.assertIsNone(
            db.get_entry_command_result(
                kind="project.create", scope="", command_id=command
            )
        )
        result = db.create_project("P", "C", command_id=command)
        self.assertEqual(
            db.get_entry_command_result(
                kind="project.create", scope="", command_id=command
            ),
            result,
        )

    def test_explicit_delete_cannot_remove_a_concurrent_imports_shared_source(self):
        first = self.upload(command_id=uuid4().hex)
        original_release = db.release_stored_object_if_unreferenced
        imported = []

        def release_then_import(key):
            released = original_release(key)
            if released:
                # Deterministically occupy the window after metadata commit
                # and before the old deleter physically deletes the bytes.
                imported.append(self.upload(command_id=uuid4().hex))
            return released

        from reqpilotx_server.application.extraction import review_support

        with (
            patch.object(db, "release_stored_object_if_unreferenced", side_effect=release_then_import),
            patch.object(review_support, "_llm_requirement_review_repository", return_value=Mock()),
            patch.object(self.store, "delete", wraps=self.store.delete) as delete_object,
        ):
            ingestion.delete_sor_document(self.project["id"], first["id"])
        delete_object.assert_not_called()
        self.assertIsNone(db.get_sor_document(first["id"]))
        if not imported:
            imported.append(self.upload(command_id=uuid4().hex))
        row = db.get_sor_document(imported[0]["id"])
        self.assertIsNotNone(self.store.stat(row["storage_key"]))
        with self.store.open(row["storage_key"]) as source:
            self.assertEqual(source.read(), b"Synthetic source.\n")

    def test_concurrent_conflicting_payloads_have_one_committed_winner(self):
        command = uuid4().hex
        barrier = Barrier(2)

        def create(name):
            barrier.wait()
            try:
                return db.create_project(name, "C", command_id=command)
            except EntryCommandConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["A", "B"]))
        self.assertEqual(results.count("conflict"), 1)
        saved = next(result for result in results if result != "conflict")
        self.assertEqual(
            db.get_entry_command_result(
                kind="project.create", scope="", command_id=command
            ),
            saved,
        )


if __name__ == "__main__":
    unittest.main()
