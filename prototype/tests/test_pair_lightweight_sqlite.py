"""RX01 lightweight SQLite local mode: durable public entry paths without test mode."""

from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import db
from app.api.routers import ingestion as ingestion_router
from app.api.routers import projects as projects_router
from app.application import runtime
from app.application.errors import ApplicationError
from reqpilotx_server.infrastructure.runtime_settings import (
    DatabaseBackend,
    DeploymentMode,
    RuntimeConfigurationError,
    RuntimeSettings,
)


def _local_settings(root: Path) -> RuntimeSettings:
    return RuntimeSettings.from_environment(
        environment={
            "REQPILOT_ENV": "local",
            "REQPILOT_DATABASE_BACKEND": "sqlite-local",
            "REQPILOT_STORAGE_DIR": str(root / "storage"),
            "REQPILOT_OBJECT_ROOT": str(root / "storage"),
            "REQPILOT_SCRATCH_ROOT": str(root / "scratch"),
            "REQPILOT_ALLOWED_HOSTS": "127.0.0.1,localhost",
        },
        prototype_dir=root,
    )


def _synthetic_pdf(path: Path) -> bytes:
    import fitz

    document = fitz.open()
    try:
        page = document.new_page(width=595, height=842)
        page.insert_text(
            (72, 72),
            "1 System scope\nThe controller shall keep the synthetic latch closed.",
        )
        document.save(path, garbage=4, deflate=True, no_new_id=True)
    finally:
        document.close()
    return path.read_bytes()


class LightweightSqliteSettingsTests(unittest.TestCase):
    def test_local_defaults_to_sqlite_and_never_becomes_test_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rx01-settings-") as temporary:
            root = Path(temporary)
            settings = RuntimeSettings.from_environment(
                environment={"REQPILOT_STORAGE_DIR": str(root / "storage")},
                prototype_dir=root,
            )
            self.assertEqual(settings.deployment_mode, DeploymentMode.LOCAL)
            self.assertEqual(settings.database_backend, DatabaseBackend.SQLITE_LOCAL)
            self.assertTrue(settings.is_sqlite)
            self.assertTrue(settings.is_local_sqlite)
            self.assertFalse(settings.is_test)
            self.assertEqual(settings.database_url, "")
            self.assertNotIn("testserver", settings.allowed_hosts)
            self.assertIn("127.0.0.1", settings.allowed_hosts)

    def test_explicit_postgres_route_and_cloud_validation_remain_available(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rx01-settings-") as temporary:
            root = Path(temporary)
            explicit = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_ENV": "local",
                    "REQPILOT_DATABASE_URL": "postgresql://db.example/reqpilotx",
                    "REQPILOT_STORAGE_DIR": str(root / "storage"),
                },
                prototype_dir=root,
            )
            self.assertEqual(explicit.database_backend, DatabaseBackend.POSTGRESQL)
            explicit_without_url = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_ENV": "local",
                    "REQPILOT_DATABASE_BACKEND": "postgresql",
                    "REQPILOT_STORAGE_DIR": str(root / "storage"),
                },
                prototype_dir=root,
            )
            self.assertEqual(
                explicit_without_url.database_backend,
                DatabaseBackend.POSTGRESQL,
            )
            self.assertEqual(
                explicit_without_url.database_url,
                "postgresql://localhost/reqpilotx",
            )
            cloud = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_ENV": "cloud",
                    "REQPILOT_DATABASE_URL": "postgresql://db.example/reqpilotx",
                    "REQPILOT_OBJECT_STORE": "oss",
                    "REQPILOT_OSS_REGION": "synthetic-region",
                    "REQPILOT_OSS_BUCKET": "synthetic-bucket",
                    "REQPILOT_ALLOWED_HOSTS": "reqpilotx.example",
                },
                prototype_dir=root,
            )
            self.assertEqual(cloud.database_backend, DatabaseBackend.POSTGRESQL)
            with self.assertRaises(RuntimeConfigurationError):
                RuntimeSettings.from_environment(
                    environment={
                        "REQPILOT_ENV": "local",
                        "REQPILOT_DATABASE_BACKEND": "sqlite-local",
                        "REQPILOT_STORAGE_DIR": str(root / "storage"),
                        "REQPILOT_DATABASE_URL": "postgresql://db.example/reqpilotx",
                    },
                    prototype_dir=root,
                )

    def test_legacy_fixture_preparation_is_test_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rx01-test-only-") as temporary:
            root = Path(temporary)
            db.configure_runtime(_local_settings(root))
            with self.assertRaises(RuntimeError):
                db.prepare_test_legacy_sqlite_fixture(root / "legacy.db")
            self.assertFalse((root / "legacy.db").exists())

    def test_init_db_refuses_to_create_beside_a_historical_postgres_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rx01-legacy-root-") as temporary:
            root = Path(temporary)
            storage = root / "storage"
            settings = _local_settings(root)
            with ExitStack() as stack:
                stack.enter_context(patch.object(db, "STORAGE_DIR", storage))
                stack.enter_context(patch.object(db, "DB_PATH", storage / "app.db"))
                db.configure_runtime(settings)
                (root / "postgres").mkdir()
                (root / "postgres" / "PG_VERSION").write_text("17\n", encoding="utf-8")
                (root / "customer-data").write_text("keep", encoding="utf-8")
                with self.assertRaises(db.LegacyPostgresDataRootError):
                    db.init_db()
                self.assertFalse((storage / "app.db").exists())
                self.assertEqual(
                    (root / "postgres" / "PG_VERSION").read_text(encoding="utf-8"),
                    "17\n",
                )
                self.assertEqual(
                    (root / "customer-data").read_text(encoding="utf-8"),
                    "keep",
                )
            db.close_runtime_database()

    def test_test_mode_is_a_distinct_backend(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rx01-settings-") as temporary:
            settings = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_TEST_MODE": "1",
                    "REQPILOT_STORAGE_DIR": str(Path(temporary) / "storage"),
                },
                prototype_dir=Path(temporary),
            )
            self.assertEqual(settings.database_backend, DatabaseBackend.SQLITE_TEST)
            self.assertTrue(settings.is_sqlite)
            self.assertFalse(settings.is_local_sqlite)
            self.assertTrue(settings.is_test)


class OrdinaryLocalApiStartupSubprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            tempfile.mkdtemp(prefix="rx01-api-startup-")
        )
        self.addCleanup(shutil.rmtree, self.root, True)

    def _environment(self) -> dict[str, str]:
        repo = Path(__file__).resolve().parents[2]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("REQPILOT_", "REQPILOTX_"))
        }
        environment.update(
            {
                "REQPILOT_ENV": "local",
                "REQPILOT_DATABASE_BACKEND": "sqlite-local",
                "REQPILOT_STORAGE_DIR": str(self.root / "storage"),
                "REQPILOT_OBJECT_ROOT": str(self.root / "storage"),
                "REQPILOT_SCRATCH_ROOT": str(self.root / "scratch"),
                "REQPILOT_FRONTEND_DIR": str(repo / "apps/web/dist"),
                "REQPILOT_ALLOWED_HOSTS": "127.0.0.1,localhost",
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(repo / "apps/server/src"),
                        str(repo / "packages/engine/src"),
                        str(repo / "prototype"),
                    )
                ),
            }
        )
        return environment

    def _run(self) -> subprocess.CompletedProcess[str]:
        script = """
from reqpilotx_server import db
from reqpilotx_server.application import runtime
runtime.startup()
print('STARTUP_OK', db.__file__)
runtime.shutdown()
"""
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            env=self._environment(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_ordinary_local_api_startup_initializes_fresh_root(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STARTUP_OK", result.stdout)
        database = self.root / "storage" / "app.db"
        self.assertTrue(database.is_file())
        conn = sqlite3.connect(database)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT version FROM reqpilotx_local_schema_migrations"
                ).fetchall(),
                [("0001_lightweight_local_schema",)],
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='llm_requirement_review_sessions'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_ordinary_local_api_startup_refuses_unknown_db_before_writes(self) -> None:
        storage = self.root / "storage"
        storage.mkdir(parents=True)
        database = storage / "app.db"
        conn = sqlite3.connect(database)
        try:
            conn.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('one', 'keep')")
            conn.commit()
        finally:
            conn.close()
        before = database.read_bytes()
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQLiteStoreAdoptionError", result.stderr + result.stdout)
        self.assertEqual(database.read_bytes(), before)
        conn = sqlite3.connect(database)
        try:
            self.assertEqual(
                conn.execute("SELECT id, value FROM unrelated").fetchall(),
                [("one", "keep")],
            )
            self.assertEqual(
                str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "delete",
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='reqpilotx_local_schema_migrations'"
                ).fetchone()
            )
        finally:
            conn.close()


class LightweightSqlitePublicApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(
            self.stack.enter_context(tempfile.TemporaryDirectory(prefix="rx01-local-"))
        )
        self.settings = _local_settings(self.root)
        self.stack.enter_context(patch.object(db, "STORAGE_DIR", self.root / "storage"))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.root / "storage" / "app.db"))
        self.stack.enter_context(patch.object(runtime, "RUNTIME_SETTINGS", self.settings))
        self.stack.enter_context(patch.object(runtime, "STORAGE_DIR", self.root / "storage"))
        self.stack.enter_context(patch.object(runtime, "UPLOADS_DIR", self.root / "storage" / "uploads"))
        self.stack.enter_context(
            patch.object(runtime, "KNOWLEDGE_DIR", self.root / "storage" / "knowledge")
        )
        self.stack.enter_context(patch.object(runtime.RUNTIME_STATE, "files", None))
        db.configure_runtime(self.settings)
        db.init_db()
        self.client = self._client()

    def tearDown(self) -> None:
        db.close_runtime_database()

    def _client(self) -> TestClient:
        app = FastAPI()

        @app.exception_handler(ApplicationError)
        async def application_error(_, exc):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        app.include_router(projects_router.build())
        app.include_router(ingestion_router.build())
        return TestClient(app)

    def test_project_import_read_and_restart_preserve_original_bytes_and_commands(self) -> None:
        source = _synthetic_pdf(self.root / "source.pdf")
        digest = hashlib.sha256(source).hexdigest()
        created = self.client.post(
            "/api/projects",
            json={"name": "Local project", "customer_name": "Synthetic", "command_id": "p-one"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        project = created.json()
        self.assertEqual(
            self.client.post(
                "/api/projects",
                json={"name": "Local project", "customer_name": "Synthetic", "command_id": "p-one"},
            ).json(),
            project,
        )
        path = f"/api/projects/{project['id']}/sor-documents"
        uploaded = self.client.post(
            path,
            data={"command_id": "d-one"},
            files={"file": ("source.pdf", source, "application/pdf")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        document = uploaded.json()
        self.assertEqual(document["source_sha256"], digest)
        self.assertEqual(document["source_size"], len(source))
        self.assertNotIn("file_path", document)
        stored_path = db.get_sor_document(document["id"])["file_path"]
        self.assertTrue(Path(stored_path).resolve().is_relative_to((self.root / "storage").resolve()))
        self.assertEqual(Path(stored_path).read_bytes(), source)
        replay = self.client.post(
            path,
            data={"command_id": "d-one"},
            files={"file": ("source.pdf", source, "application/pdf")},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), document)
        conflict = self.client.post(
            path,
            data={"command_id": "d-one"},
            files={"file": ("changed.pdf", source + b"\n% changed", "application/pdf")},
        )
        self.assertEqual(conflict.status_code, 409)
        downloaded = self.client.get(path + f"/{document['id']}/file")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), digest)

        self.client.close()
        db.close_runtime_database()
        db.configure_runtime(self.settings)
        db.init_db()
        self.client = self._client()
        restarted = self.client.get("/api/projects")
        self.assertEqual(restarted.status_code, 200)
        self.assertEqual([item["id"] for item in restarted.json()], [project["id"]])
        listed = self.client.get(path)
        self.assertEqual([item["id"] for item in listed.json()], [document["id"]])
        self.assertEqual(
            hashlib.sha256(
                self.client.get(path + f"/{document['id']}/file").content
            ).hexdigest(),
            digest,
        )

    def test_local_source_path_outside_owned_storage_is_rejected(self) -> None:
        project = db.create_project("Path guard", "Synthetic")
        document = db.add_sor_document(
            project_id=project["id"],
            file_name="outside.pdf",
            file_type="pdf",
            file_path=str(self.root / "outside.pdf"),
            extracted_text="",
            source_sha256="a" * 64,
            source_size=1,
        )
        (self.root / "outside.pdf").write_bytes(b"outside")
        with self.assertRaises(RuntimeError):
            runtime._runtime_record_path(document)

    def _init_against_path(self, database_path: Path) -> None:
        with patch.object(db, "DB_PATH", database_path):
            db.init_db()

    def test_existing_unversioned_sqlite_is_rejected_without_mutation(self) -> None:
        database = self.root / "unknown.db"
        conn = sqlite3.connect(database)
        try:
            conn.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('one', 'keep')")
            conn.commit()
        finally:
            conn.close()
        before = database.read_bytes()
        with self.assertRaises(db.SQLiteStoreAdoptionError):
            self._init_against_path(database)
        self.assertEqual(database.read_bytes(), before)
        conn = sqlite3.connect(database)
        try:
            self.assertEqual(
                conn.execute("SELECT id, value FROM unrelated").fetchall(),
                [("one", "keep")],
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='reqpilotx_local_schema_migrations'"
                ).fetchone()
            )
            self.assertEqual(
                str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "delete",
            )
        finally:
            conn.close()

    def test_empty_preexisting_destination_initializes(self) -> None:
        database = self.root / "empty.db"
        database.touch()
        self._init_against_path(database)
        conn = sqlite3.connect(database)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT version FROM reqpilotx_local_schema_migrations"
                ).fetchall(),
                [("0001_lightweight_local_schema",)],
            )
        finally:
            conn.close()

    def test_future_history_in_live_wal_is_rejected_read_only(self) -> None:
        database = self.root / "wal-future.db"
        self._init_against_path(database)
        writer = sqlite3.connect(database, timeout=5.0)
        try:
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute(
                "INSERT INTO reqpilotx_local_schema_migrations "
                "(version, name, sha256, applied_at) VALUES (?, ?, ?, ?)",
                ("9999_future_schema", "9999_future_schema.sql", "f" * 64, "future"),
            )
            writer.commit()
            wal = database.with_name(database.name + "-wal")
            self.assertGreater(wal.stat().st_size, 0)
            with self.assertRaises(db.SQLiteUnknownAppliedMigrationError):
                self._init_against_path(database)
            self.assertGreater(wal.stat().st_size, 0)
        finally:
            writer.close()

    def test_local_database_enforces_foreign_keys_and_migration_identity(self) -> None:
        with db.get_conn() as conn:
            self.assertEqual(int(conn.execute("PRAGMA foreign_keys").fetchone()[0]), 1)
            self.assertEqual(
                conn.execute(
                    "SELECT version FROM reqpilotx_local_schema_migrations"
                ).fetchone()["version"],
                "0001_lightweight_local_schema",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO sor_documents
                    (id, project_id, file_name, file_type, file_path, extracted_text, created_at)
                    VALUES ('orphan', 'missing-project', 'x.pdf', 'pdf', '/tmp/x.pdf', '', 'now')
                    """
                )

    def test_unknown_future_schema_is_rejected_before_changes(self) -> None:
        project = db.create_project("Future guard", "Synthetic")
        with db.get_conn() as conn:
            tables_before = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            conn.execute(
                """
                INSERT INTO reqpilotx_local_schema_migrations
                (version, name, sha256, applied_at)
                VALUES ('9999_future_schema', '9999_future_schema.sql', ?, 'future')
                """,
                ("f" * 64,),
            )
        with self.assertRaises(db.SQLiteUnknownAppliedMigrationError):
            db.init_db()
        with db.get_conn() as conn:
            tables_after = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertEqual(tables_after, tables_before)
        self.assertEqual(db.get_project(project["id"])["name"], "Future guard")

    def test_applied_sqlite_migration_checksum_change_is_rejected(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE reqpilotx_local_schema_migrations SET sha256 = ? WHERE version = ?",
                ("a" * 64, "0001_lightweight_local_schema"),
            )
        with self.assertRaises(db.SQLiteMigrationDriftError):
            db.init_db()

    def test_failed_migration_rolls_back_schema_and_preserves_business_data(self) -> None:
        project = db.create_project("Rollback guard", "Synthetic")
        migrations = self.root / "failing-migrations"
        migrations.mkdir()
        shutil.copy(
            db.sqlite_migration_dir() / "0001_lightweight_local_schema.sql",
            migrations / "0001_lightweight_local_schema.sql",
        )
        (migrations / "0002_failing_probe.sql").write_text(
            """
            CREATE TABLE rx01_failed_probe (id TEXT PRIMARY KEY);
            INSERT INTO rx01_failed_probe (id) VALUES ('partial');
            INSERT INTO rx01_missing_table (id) VALUES ('boom');
            """,
            encoding="utf-8",
        )
        with patch.object(db, "sqlite_migration_dir", return_value=migrations):
            with self.assertRaises(sqlite3.OperationalError):
                db.init_db()
        with db.get_conn() as conn:
            versions = [
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM reqpilotx_local_schema_migrations ORDER BY version"
                )
            ]
            failed_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'rx01_failed_probe'"
            ).fetchone()
        self.assertEqual(versions, ["0001_lightweight_local_schema"])
        self.assertIsNone(failed_table)
        self.assertEqual(db.get_project(project["id"])["name"], "Rollback guard")

    def test_successful_new_migration_commits_once_and_reopens(self) -> None:
        migrations = self.root / "successful-migrations"
        migrations.mkdir()
        shutil.copy(
            db.sqlite_migration_dir() / "0001_lightweight_local_schema.sql",
            migrations / "0001_lightweight_local_schema.sql",
        )
        (migrations / "0002_successful_probe.sql").write_text(
            "CREATE TABLE rx01_successful_probe (id TEXT PRIMARY KEY);",
            encoding="utf-8",
        )
        with patch.object(db, "sqlite_migration_dir", return_value=migrations):
            db.init_db()
            db.init_db()
        with db.get_conn() as conn:
            versions = [
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM reqpilotx_local_schema_migrations ORDER BY version"
                )
            ]
            probe = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'rx01_successful_probe'"
            ).fetchone()
        self.assertEqual(
            versions,
            ["0001_lightweight_local_schema", "0002_successful_probe"],
        )
        self.assertIsNotNone(probe)


if __name__ == "__main__":
    unittest.main()
