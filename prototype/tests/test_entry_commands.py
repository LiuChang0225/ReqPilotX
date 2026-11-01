"""Offline entry API regression: receipts, concurrent writers and lost replies."""

from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile

from app import db
from reqpilotx_server.adapters.persistence.entry_commands import EntryCommandConflict
from app.api.routers import ingestion as ingestion_router, projects as projects_router
from app.application import ingestion, runtime
from app.infrastructure.runtime_settings import DatabaseBackend


class EntryCommandTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(
            self.stack.enter_context(TemporaryDirectory(prefix="entry-commands-"))
        )
        self.stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.stack.enter_context(
            patch.object(runtime, "UPLOADS_DIR", self.root / "uploads")
        )
        db.init_db()
        app = FastAPI()
        from app.application.errors import ApplicationError
        from fastapi.responses import JSONResponse

        @app.exception_handler(ApplicationError)
        async def application_error(_, exc):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        app.include_router(projects_router.build())
        app.include_router(ingestion_router.build())
        self.client = TestClient(app)
        self.project = db.create_project("Entry tests", "Synthetic")

    def create(self, command="create-one", name="Project"):
        return self.client.post(
            "/api/projects",
            json={"name": name, "customer_name": "Synthetic", "command_id": command},
        )

    def upload(
        self,
        command="import-one",
        *,
        data=b"The controller shall start.\n",
        name="source.txt",
        project_id=None,
    ):
        path = f"/api/projects/{project_id or self.project['id']}/sor-documents"
        return self.client.post(
            path,
            data={"command_id": command},
            files={"file": (name, data, "text/plain")},
        )

    def test_create_replay_and_query_are_identical_after_reinitialization(self):
        first = self.create().json()
        db.init_db()
        self.assertEqual(self.create(name=" Project ").json(), first)
        self.assertEqual(
            self.client.get("/api/projects/creation-commands/create-one").json(), first
        )
        self.assertEqual(len(db.list_projects()), 2)

    def test_create_same_id_different_payload_conflicts(self):
        self.assertEqual(self.create().status_code, 200)
        self.assertEqual(self.create(name="Changed").status_code, 409)
        self.assertEqual(len(db.list_projects()), 2)

    def test_distinct_commands_and_legacy_requests_remain_distinct(self):
        a, b = self.create("one").json(), self.create("two").json()
        self.assertNotEqual(a["id"], b["id"])
        a = self.client.post(
            "/api/projects", json={"name": "Legacy", "customer_name": "Synthetic"}
        ).json()
        b = self.client.post(
            "/api/projects", json={"name": "Legacy", "customer_name": "Synthetic"}
        ).json()
        self.assertNotEqual(a["id"], b["id"])

    def test_invalid_commands_and_blank_project_are_rejected_before_writes(self):
        for command in ("", "with spaces", "x" * 201):
            self.assertEqual(self.create(command).status_code, 422)
            self.assertEqual(self.upload(command).status_code, 422)
        self.assertEqual(self.create(name=" ").status_code, 422)
        self.assertEqual(len(db.list_projects()), 1)
        self.assertEqual(db.list_sor_documents(self.project["id"]), [])

    def test_unknown_commands_are_not_reported_as_saved(self):
        self.assertEqual(
            self.client.get("/api/projects/creation-commands/unknown").status_code, 404
        )
        self.assertEqual(
            self.client.get(
                f"/api/projects/{self.project['id']}/sor-documents/import-commands/unknown"
            ).status_code,
            404,
        )

    def test_import_replay_uses_same_document_and_removes_attempt_staging(self):
        first = self.upload().json()
        db.init_db()
        self.assertEqual(self.upload().json(), first)
        self.assertEqual(
            self.client.get(
                f"/api/projects/{self.project['id']}/sor-documents/import-commands/import-one"
            ).json(),
            first,
        )
        self.assertEqual(len(db.list_sor_documents(self.project["id"])), 1)
        self.assertEqual(len(list((self.root / "uploads").iterdir())), 1)

    def test_import_content_and_filename_conflicts_do_not_mutate_source(self):
        first = self.upload().json()
        self.assertEqual(self.upload(data=b"Changed bytes.\n").status_code, 409)
        self.assertEqual(self.upload(name="changed.txt").status_code, 409)
        document = db.get_sor_document(first["id"])
        self.assertEqual(
            Path(document["file_path"]).read_bytes(), b"The controller shall start.\n"
        )
        self.assertEqual(len(list((self.root / "uploads").iterdir())), 1)

    def test_import_command_scope_is_project_bound(self):
        first = self.upload().json()
        other = db.create_project("Other", "Synthetic")["id"]
        path = f"/api/projects/{other}/sor-documents/import-commands/import-one"
        self.assertEqual(self.client.get(path).status_code, 404)
        second = self.upload(project_id=other).json()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            self.client.get(
                "/api/projects/missing/sor-documents/import-commands/import-one"
            ).status_code,
            404,
        )

    def test_deleted_result_cannot_be_resurrected_by_replay(self):
        first = self.upload().json()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM sor_documents WHERE id = ?", (first["id"],))
        self.assertEqual(self.upload().status_code, 410)
        self.assertEqual(
            self.client.get(
                f"/api/projects/{self.project['id']}/sor-documents/import-commands/import-one"
            ).status_code,
            410,
        )

    def test_same_project_command_concurrent_writers_return_one_result(self):
        barrier = Barrier(4)

        def create(_):
            barrier.wait()
            return db.create_project("Concurrent", "Synthetic", command_id="concurrent")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(4)))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(len(db.list_projects()), 2)

    def test_conflicting_concurrent_writers_have_one_winner(self):
        barrier = Barrier(2)

        def create(name):
            barrier.wait()
            try:
                return db.create_project(
                    name, "Synthetic", command_id="concurrent-conflict"
                )
            except EntryCommandConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["A", "B"]))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len(db.list_projects()), 2)

    def test_same_import_command_concurrent_writers_share_source_and_result(self):
        barrier = Barrier(3)

        def upload(_):
            barrier.wait()
            file = UploadFile(
                filename="source.txt", file=io.BytesIO(b"Concurrent source.\n")
            )
            return asyncio.run(
                ingestion.upload_sor_document(
                    self.project["id"], file, command_id="concurrent-import"
                )
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(upload, range(3)))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(len(db.list_sor_documents(self.project["id"])), 1)
        self.assertEqual(len(list((self.root / "uploads").iterdir())), 1)

    def test_import_legacy_and_distinct_commands_keep_intentional_duplicates(self):
        one, two = self.upload("one").json(), self.upload("two").json()
        self.assertNotEqual(one["id"], two["id"])
        path = f"/api/projects/{self.project['id']}/sor-documents"
        one = self.client.post(
            path, files={"file": ("source.txt", b"Legacy source.\n", "text/plain")}
        ).json()
        two = self.client.post(
            path, files={"file": ("source.txt", b"Legacy source.\n", "text/plain")}
        ).json()
        self.assertNotEqual(one["id"], two["id"])

    def test_lost_project_commit_acknowledgement_replays_without_duplicate(self):
        original = db.get_conn

        @contextmanager
        def lost_ack():
            with original() as conn:
                yield conn
            raise RuntimeError("commit acknowledgement lost")

        with patch.object(db, "get_conn", lost_ack):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement lost"):
                self.create()
        self.assertEqual(self.create().status_code, 200)
        self.assertEqual(len(db.list_projects()), 2)

    def test_resource_failure_rolls_back_claim_and_can_retry(self):
        with patch.object(
            db, "complete_receipt", side_effect=RuntimeError("receipt failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "receipt failure"):
                db.create_project("Project", "Synthetic", command_id="create-one")
        self.assertIsNone(
            db.get_entry_command_result(
                kind="project.create", scope="", command_id="create-one"
            )
        )
        self.assertEqual(self.create().status_code, 200)
        self.assertEqual(len(db.list_projects()), 2)

    def test_lost_commit_reply_is_queryable_and_does_not_delete_saved_source(self):
        original = db.add_sor_document

        def commit_then_fail(**kwargs):
            original(**kwargs)
            raise RuntimeError("commit acknowledgement lost")

        with patch.object(db, "add_sor_document", side_effect=commit_then_fail):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement lost"):
                self.upload()
        saved = ingestion.get_document_import_command(self.project["id"], "import-one")
        document = db.get_sor_document(saved["id"])
        self.assertTrue(Path(document["file_path"]).is_file())
        self.assertEqual(self.upload().json(), saved)
        self.assertEqual(len(db.list_sor_documents(self.project["id"])), 1)

    def test_failed_shared_object_publisher_never_deletes_another_reference(self):
        store = Mock()
        store.stat.return_value = None
        container = SimpleNamespace(object_store=store)
        settings = replace(
            runtime.RUNTIME_SETTINGS, database_backend=DatabaseBackend.POSTGRESQL
        )
        # The competing successful request references the same shared key before
        # this request's DB write fails. No read-then-delete compensation is safe.
        with (
            patch.object(runtime, "RUNTIME_SETTINGS", settings),
            patch.object(runtime.RUNTIME_STATE, "container", container),
            patch.object(
                db, "add_sor_document", side_effect=RuntimeError("losing writer")
            ),
        ):
            upload = UploadFile(
                filename="source.txt",
                file=io.BytesIO(b"Synthetic source.\n"),
                headers=Headers({"content-type": "text/plain"}),
            )
            with self.assertRaisesRegex(RuntimeError, "losing writer"):
                asyncio.run(
                    ingestion.upload_sor_document(
                        self.project["id"], upload, command_id="shared"
                    )
                )
        store.put.assert_called_once()
        store.delete.assert_not_called()
        self.assertEqual(list((settings.scratch_root / "ingest/sor").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
