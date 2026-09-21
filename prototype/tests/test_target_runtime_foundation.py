from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook
from starlette.datastructures import Headers, UploadFile

from app.api.app_factory import create_app, create_http_app
from app.adapters.storage.local_object_store import LocalObjectStore
from app.adapters.storage.oss_object_store import AliyunOssObjectStore
from app.adapters.persistence.postgres_jobs import PostgresJobQueue
from app.application import knowledge as application_knowledge
from app.application.errors import ApplicationError
from app.application.extraction import document_jobs
from app.application.cutover.sqlite_to_postgres import (
    CutoverRejected,
    build_cutover_plan,
)
from app.application.knowledge_ingestion import (
    extract_knowledge_document_content,
)
from app.application import runtime as application_runtime
from app.application.storage.runtime_files import RuntimeObjectFiles
from app.infrastructure.runtime_settings import (
    AgentRuntimeBackend,
    DatabaseBackend,
    DeploymentMode,
    ObjectStoreBackend,
    NativeCapabilityMode,
    ProcessRole,
    RuntimeConfigurationError,
    RuntimeSettings,
)
from app.infrastructure.postgres_compat import translate_sql_for_postgres
from app.domain.extraction.processing_profile import (
    DocumentProcessingProfileV1,
    ResolvedProviderBindingsV1,
    build_local_rules_profile,
    build_resolved_processing_plan,
)
from app.ports.job_queue import DurableJob, JobLeaseLostError, JobStatus
from app.worker.handlers import build_job_handlers
from app.worker.__main__ import _official_local_worker_id
from app.worker.runtime import WorkerRuntime
from scripts.sqlite_cutover_inventory import build_inventory


class RuntimeSettingsTests(unittest.TestCase):
    def test_native_capability_release_configuration_is_host_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "registry"
            settings = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_TEST_MODE": "1",
                    "REQPILOT_STORAGE_DIR": temporary,
                    "REQPILOT_NATIVE_CAPABILITY_MODE": "registered-release",
                    "REQPILOT_NATIVE_CAPABILITY_REGISTRY": str(registry),
                    "REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256": "a" * 64,
                },
                prototype_dir=root,
            )
        self.assertEqual(
            settings.native_capability_mode,
            NativeCapabilityMode.REGISTERED_RELEASE,
        )
        self.assertEqual(settings.native_capability_registry_root, registry.resolve())
        self.assertEqual(settings.native_capability_bundle_sha256, "a" * 64)

    def test_agent_none_does_not_require_a_capability_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_TEST_MODE": "1",
                    "REQPILOT_STORAGE_DIR": temporary,
                    "REQPILOT_AGENT_RUNTIME": "none",
                },
                prototype_dir=Path(temporary),
            )
        self.assertEqual(settings.agent_runtime_backend, AgentRuntimeBackend.NONE)
        self.assertEqual(settings.native_capability_bundle_sha256, "")

    def test_native_capability_configuration_rejects_bad_identity_and_cloud_source(self) -> None:
        with self.assertRaisesRegex(
            RuntimeConfigurationError,
            "REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256",
        ):
            RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_TEST_MODE": "1",
                    "REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256": "not-a-sha",
                }
            )
        with self.assertRaisesRegex(RuntimeConfigurationError, "development-source"):
            RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_ENV": "cloud",
                    "REQPILOT_DATABASE_URL": "postgresql://db/reqpilotx",
                    "REQPILOT_OBJECT_STORE": "oss",
                    "REQPILOT_ALLOWED_HOSTS": "reqpilot.example.com",
                    "REQPILOT_ALLOWED_ORIGINS": "https://reqpilot.example.com",
                    "REQPILOT_OSS_REGION": "cn-hangzhou",
                    "REQPILOT_OSS_BUCKET": "private-bucket",
                    "REQPILOT_NATIVE_CAPABILITY_MODE": "development-source",
                }
            )

    def test_test_mode_never_requires_postgres_or_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_TEST_MODE": "1",
                    "REQPILOT_STORAGE_DIR": temporary,
                },
                prototype_dir=Path(temporary),
            )
        self.assertEqual(settings.deployment_mode, DeploymentMode.TEST)
        self.assertEqual(settings.database_backend, DatabaseBackend.SQLITE_TEST)
        self.assertEqual(settings.object_store_backend, ObjectStoreBackend.LOCAL)
        self.assertEqual(settings.local_worker_count, 1)
        self.assertIn("testserver", settings.allowed_hosts)

    def test_local_mode_defaults_to_native_postgres_and_local_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = RuntimeSettings.from_environment(
                environment={"REQPILOT_STORAGE_DIR": temporary},
                prototype_dir=Path(temporary),
            )
        self.assertEqual(settings.deployment_mode, DeploymentMode.LOCAL)
        self.assertEqual(settings.database_backend, DatabaseBackend.POSTGRESQL)
        self.assertEqual(settings.database_url, "postgresql://localhost/reqpilotx")
        self.assertEqual(settings.object_store_backend, ObjectStoreBackend.LOCAL)
        self.assertEqual(settings.local_worker_count, 2)
        self.assertEqual(
            settings.object_root,
            Path(temporary).resolve() / "runtime-data" / "objects",
        )
        self.assertEqual(settings.agent_runtime_backend, AgentRuntimeBackend.NONE)
        self.assertFalse(settings.agent_customer_data_enabled)

    def test_codex_agent_runtime_requires_local_absolute_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_bin = root / "codex"
            codex_bin.touch()
            codex_home = root / "codex-home"
            codex_home.mkdir()
            settings = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_AGENT_RUNTIME": "codex-app-server",
                    "REQPILOT_CODEX_BIN": str(codex_bin),
                    "REQPILOT_CODEX_HOME": str(codex_home),
                    "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED": "true",
                },
                prototype_dir=root,
            )
            self.assertEqual(
                settings.agent_runtime_backend,
                AgentRuntimeBackend.CODEX_APP_SERVER,
            )
            self.assertEqual(settings.codex_bin, codex_bin)
            self.assertEqual(settings.codex_home, codex_home)
            self.assertTrue(settings.agent_customer_data_enabled)

            with self.assertRaisesRegex(
                RuntimeConfigurationError,
                "currently local-only",
            ):
                RuntimeSettings.from_environment(
                    environment={
                        "REQPILOT_ENV": "cloud",
                        "REQPILOT_DATABASE_URL": "postgresql://db/reqpilotx",
                        "REQPILOT_ALLOWED_HOSTS": "reqpilot.example.com",
                        "REQPILOT_ALLOWED_ORIGINS": "https://reqpilot.example.com",
                        "REQPILOT_OSS_REGION": "cn-hangzhou",
                        "REQPILOT_OSS_BUCKET": "private-bucket",
                        "REQPILOT_AGENT_RUNTIME": "codex-app-server",
                        "REQPILOT_CODEX_BIN": str(codex_bin),
                        "REQPILOT_CODEX_HOME": str(codex_home),
                    },
                    prototype_dir=root,
                )

    def test_codex_agent_runtime_rejects_missing_paths(self) -> None:
        with self.assertRaisesRegex(
            RuntimeConfigurationError,
            "existing file",
        ):
            RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_AGENT_RUNTIME": "codex-app-server",
                    "REQPILOT_CODEX_BIN": "/missing/codex",
                    "REQPILOT_CODEX_HOME": "/missing/codex-home",
                }
            )

    def test_agent_customer_data_flag_rejects_ambiguous_values(self) -> None:
        with self.assertRaisesRegex(
            RuntimeConfigurationError,
            "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED",
        ):
            RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED": "enabled-ish",
                }
            )

    def test_local_worker_count_is_configurable_but_hard_capped_at_two(self) -> None:
        settings = RuntimeSettings.from_environment(
            environment={"REQPILOT_LOCAL_WORKER_COUNT": "1"}
        )
        self.assertEqual(settings.local_worker_count, 1)
        with self.assertRaisesRegex(
            RuntimeConfigurationError,
            "REQPILOT_LOCAL_WORKER_COUNT",
        ):
            RuntimeSettings.from_environment(
                environment={"REQPILOT_LOCAL_WORKER_COUNT": "3"}
            )

    def test_official_local_slot_two_requires_the_no_llm_extraction_lane(self) -> None:
        environment = {
            "REQPILOT_LOCAL_WORKER_SLOT": "2",
            "REQPILOT_LOCAL_WORKER_COUNT": "2",
            "REQPILOT_LOCAL_RUNTIME_INSTANCE_ID": "runtime-123",
        }

        first_worker_id = _official_local_worker_id(
            accepted_kinds=("document.extract",),
            claim_profile="local-extraction-no-llm",
            environment=environment,
        )
        second_worker_id = _official_local_worker_id(
            accepted_kinds=("document.extract",),
            claim_profile="local-extraction-no-llm",
            environment=environment,
        )

        self.assertTrue(first_worker_id.startswith("local-runtime-123-slot-2-"))
        self.assertTrue(second_worker_id.startswith("local-runtime-123-slot-2-"))
        self.assertNotEqual(first_worker_id, second_worker_id)
        with self.assertRaisesRegex(SystemExit, "unsafe durable claim lane"):
            _official_local_worker_id(
                accepted_kinds=(),
                claim_profile="general",
                environment=environment,
            )

    def test_official_local_slot_one_cannot_be_narrowed(self) -> None:
        environment = {
            "REQPILOT_LOCAL_WORKER_SLOT": "1",
            "REQPILOT_LOCAL_WORKER_COUNT": "2",
            "REQPILOT_LOCAL_RUNTIME_INSTANCE_ID": "runtime-123",
        }

        with self.assertRaisesRegex(SystemExit, "unsafe durable claim lane"):
            _official_local_worker_id(
                accepted_kinds=("document.extract",),
                claim_profile="local-extraction-no-llm",
                environment=environment,
            )

    def test_cloud_requires_explicit_host_and_oss_identity(self) -> None:
        with self.assertRaisesRegex(
            RuntimeConfigurationError,
            "REQPILOT_ALLOWED_HOSTS",
        ):
            RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_ENV": "cloud",
                    "REQPILOT_DATABASE_URL": "postgresql://db/reqpilotx",
                    "REQPILOT_OSS_REGION": "cn-hangzhou",
                    "REQPILOT_OSS_BUCKET": "private-bucket",
                }
            )
        settings = RuntimeSettings.from_environment(
            environment={
                "REQPILOT_ENV": "cloud",
                "REQPILOT_PROCESS_ROLE": "worker",
                "REQPILOT_DATABASE_URL": "postgresql://db/reqpilotx",
                "REQPILOT_ALLOWED_HOSTS": "reqpilot.example.com",
                "REQPILOT_ALLOWED_ORIGINS": "https://reqpilot.example.com",
                "REQPILOT_OSS_REGION": "cn-hangzhou",
                "REQPILOT_OSS_BUCKET": "private-bucket",
            }
        )
        self.assertEqual(settings.process_role, ProcessRole.WORKER)
        self.assertEqual(settings.object_store_backend, ObjectStoreBackend.OSS)
        self.assertEqual(
            settings.scratch_root,
            (Path(tempfile.gettempdir()) / "reqpilotx").resolve(),
        )

    def test_wildcard_trust_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeConfigurationError, "Wildcard"):
            RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_ALLOWED_HOSTS": "*",
                }
            )

    def test_local_startup_does_not_hide_postgres_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_ENV": "local",
                    "REQPILOT_DATABASE_URL": "postgresql://localhost/reqpilotx",
                },
                prototype_dir=Path(temporary),
            )
            with (
                patch.object(application_runtime, "RUNTIME_SETTINGS", settings),
                patch.object(
                    application_runtime,
                    "load_ai_copilot_config",
                    return_value=SimpleNamespace(),
                ),
                patch.object(
                    application_runtime,
                    "build_runtime_container",
                    side_effect=RuntimeError("postgres unavailable"),
                ),
                patch.object(application_runtime, "_record_startup_state"),
            ):
                with self.assertRaisesRegex(RuntimeError, "postgres unavailable"):
                    application_runtime.startup()


class LocalLauncherContractTests(unittest.TestCase):
    def test_start_script_refreshes_changed_or_incomplete_runtime_dependencies(
        self,
    ) -> None:
        start_source = (Path(__file__).resolve().parents[1] / "start.sh").read_text(
            encoding="utf-8"
        )
        dependency_check = start_source.index("REQUIREMENTS_HASH=")
        migration = start_source.index("scripts/runtime_admin.py migrate")

        self.assertLess(dependency_check, migration)
        self.assertIn(".reqpilotx-requirements.sha256", start_source)
        self.assertIn(
            "import alibabacloud_oss_v2, fastapi, psycopg, uvicorn",
            start_source,
        )
        self.assertIn(
            '"$INSTALLED_REQUIREMENTS_HASH" != "$REQUIREMENTS_HASH"',
            start_source,
        )
        self.assertIn(".venv/bin/pip install -r requirements.txt", start_source)

    def test_local_launchers_reconcile_workers_by_repository_and_port(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        start_source = (root / "start.sh").read_text(encoding="utf-8")
        stop_source = (root / "stop.sh").read_text(encoding="utf-8")
        status_source = (root / "status.sh").read_text(encoding="utf-8")

        self.assertIn('WORKER_COUNT="${REQPILOT_LOCAL_WORKER_COUNT:-2}"', start_source)
        self.assertIn("worker_${PORT}_$1.pid", start_source)
        self.assertIn('start_stable_worker "$slot"', start_source)
        self.assertIn('REQPILOT_LOCAL_RUNTIME_PORT="$PORT"', start_source)
        self.assertIn(
            'export REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT"',
            start_source,
        )
        self.assertGreaterEqual(
            start_source.count('REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT"'),
            7,
        )
        self.assertIn("local-extraction-no-llm", start_source)
        self.assertIn("--drain-restart", start_source)
        self.assertIn("RUNTIME_SOURCE_REVISION", start_source)
        self.assertIn("pool-status", start_source)
        self.assertIn("WORKER_POOL_MISSING_SLOTS: none", start_source)
        existing_api_block = start_source.split('EXISTING_API_HEALTHY="true"', 1)[
            1
        ].split("exit 0", 1)[0]
        self.assertNotIn("start_stable_worker", existing_api_block)
        self.assertNotIn("scripts/runtime_admin.py migrate", existing_api_block)
        readiness_failure = start_source.index('if [[ "$READY" != "true" ]]')
        failed_start_cleanup = start_source.index(
            "cleanup_local_workers",
            readiness_failure,
        )
        failed_worker_screen_cleanup = start_source.index(
            'screen -S "$(worker_screen_name "$slot")" -X quit',
            readiness_failure,
        )
        self.assertLess(failed_start_cleanup, failed_worker_screen_cleanup)

        worker_stop = stop_source.index('python3 "$WORKER_GUARD" cleanup')
        worker_screen_cleanup = stop_source.index(
            'screen -S "reqpilotx_worker_${PORT}_${slot}" -X quit'
        )
        self.assertLess(worker_stop, worker_screen_cleanup)
        self.assertIn('python3 "$WORKER_GUARD" drain', stop_source)
        self.assertIn("WORKER_DRAIN_FILE", stop_source)
        self.assertIn("pool-status", status_source)
        self.assertIn("RUNTIME_SOURCE: current", status_source)
        self.assertIn("worker_${PORT}_${slot}.log", status_source)
        self.assertIn("WORKER_GUARD_STATUS", status_source)


class LocalObjectStoreTests(unittest.TestCase):
    def test_put_open_stat_and_delete_are_hash_verified(self) -> None:
        payload = b"source-bound-object"
        sha256 = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalObjectStore(Path(temporary))
            stored = store.put(
                key="uploads/ab/source.pdf",
                source=io.BytesIO(payload),
                expected_sha256=sha256,
                media_type="application/pdf",
            )
            self.assertEqual(stored.sha256, sha256)
            with store.open(stored.key) as stream:
                self.assertEqual(stream.read(), payload)
            self.assertEqual(store.stat(stored.key).sha256, sha256)
            self.assertTrue(store.delete(stored.key))
            self.assertFalse(store.delete(stored.key))

    def test_hash_mismatch_and_path_escape_do_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalObjectStore(Path(temporary))
            with self.assertRaisesRegex(ValueError, "hash"):
                store.put(
                    key="uploads/source.pdf",
                    source=io.BytesIO(b"wrong"),
                    expected_sha256="0" * 64,
                    media_type="application/pdf",
                )
            with self.assertRaisesRegex(ValueError, "safe relative"):
                store.open("../outside")

    def test_runtime_files_reject_direct_paths_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.bin"
            source.write_bytes(b"legacy")
            files = RuntimeObjectFiles(
                LocalObjectStore(root / "objects"),
                root / "materialized",
            )
            with self.assertRaisesRegex(ValueError, "storage identity"):
                files.resolve_record({"file_path": str(source)})

            test_files = RuntimeObjectFiles(
                LocalObjectStore(root / "test-objects"),
                root / "test-materialized",
                allow_legacy_paths=True,
            )
            self.assertEqual(
                test_files.resolve_record({"file_path": str(source)}),
                source,
            )


class KnowledgeIngestionBoundaryTests(unittest.TestCase):
    def test_formal_worker_content_extraction_supports_txt_and_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text_path = root / "reference.txt"
            text_path.write_bytes(b"\xef\xbb\xbfFirst line\nSecond line")
            text_content = extract_knowledge_document_content(
                text_path,
                expected_sha256=hashlib.sha256(text_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(text_content.file_type, "TXT")
            self.assertEqual(text_content.extracted_text, "First line\nSecond line")

            workbook_path = root / "reference.xlsx"
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.append(["Requirement", "Priority"])
            worksheet.append(["Keep audit evidence", "High"])
            workbook.save(workbook_path)
            workbook.close()
            workbook_content = extract_knowledge_document_content(
                workbook_path,
                expected_sha256=hashlib.sha256(workbook_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(workbook_content.file_type, "XLSX")
            self.assertIn("| Requirement | Priority |", workbook_content.extracted_text)
            self.assertIn(
                "| Keep audit evidence | High |", workbook_content.extracted_text
            )

    def test_durable_knowledge_worker_parses_before_indexing(self) -> None:
        class Context:
            def __init__(self) -> None:
                self.stages: list[str] = []

            def heartbeat(self, progress=None):
                self.stages.append(str((progress or {}).get("stage") or ""))

            def cancellation_requested(self) -> bool:
                return False

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "reference.txt"
            source.write_text("Durable knowledge", encoding="utf-8")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            document = {
                "id": "knowledge-1",
                "project_id": "project-1",
                "file_name": source.name,
                "file_type": "TXT",
                "storage_key": "knowledge/project-1/reference.txt",
                "extracted_text": "",
                "source_sha256": source_sha256,
                "retrieval_syncs": [],
            }
            updated = {**document, "extracted_text": "Durable knowledge"}
            context = Context()
            with (
                patch.object(
                    application_knowledge.db,
                    "get_knowledge_document",
                    return_value=document,
                ),
                patch.object(
                    application_runtime,
                    "_runtime_record_path",
                    return_value=source,
                ),
                patch.object(
                    application_knowledge.db,
                    "update_knowledge_document_content",
                    return_value=updated,
                ) as update_content,
                patch.object(
                    application_knowledge,
                    "_sync_knowledge_document_targets",
                    return_value=updated,
                ) as sync_targets,
            ):
                result = application_knowledge.run_durable_knowledge_sync(
                    SimpleNamespace(
                        payload={
                            "project_id": "project-1",
                            "document_id": "knowledge-1",
                            "source_sha256": source_sha256,
                            "profile_ids": ["aliyun"],
                        }
                    ),
                    context,
                )

        self.assertEqual(context.stages, ["parsing", "indexing"])
        update_content.assert_called_once_with(
            "knowledge-1",
            file_type="TXT",
            extracted_text="Durable knowledge",
        )
        sync_targets.assert_called_once_with(
            "project-1",
            updated,
            profile_ids=["aliyun"],
        )
        self.assertEqual(result["extracted_text"], "Durable knowledge")

    def test_production_knowledge_upload_only_preflights_and_enqueues(self) -> None:
        captured: dict[str, object] = {}

        def add_document(**kwargs):
            captured.update(kwargs)
            return {
                "id": "knowledge-1",
                **kwargs,
                "storage_key": kwargs["file_path"],
                "retrieval_sync_status": "PENDING",
                "retrieval_sync_error": "",
                "retrieval_synced_at": "",
                "created_at": "2026-07-28T00:00:00+00:00",
            }

        with tempfile.TemporaryDirectory() as temporary:
            upload = UploadFile(
                filename="reference.txt",
                file=io.BytesIO(b"Production source"),
                headers=Headers({"content-type": "text/plain"}),
            )
            runtime_settings = SimpleNamespace(
                database_backend=DatabaseBackend.POSTGRESQL,
            )
            with (
                patch.object(
                    application_runtime,
                    "RUNTIME_SETTINGS",
                    runtime_settings,
                ),
                patch(
                    "app.application.projects._require_project",
                ),
                patch(
                    "app.application.ingestion._upload_staging_dir",
                    return_value=Path(temporary),
                ),
                patch(
                    "app.application.ingestion._prepare_uploaded_document",
                    side_effect=AssertionError(
                        "production must not load the isolated test parser"
                    ),
                ),
                patch(
                    "app.application.ingestion._publish_staged_object",
                    return_value=True,
                ),
                patch.object(
                    application_knowledge,
                    "_find_duplicate_knowledge_document",
                    return_value=None,
                ),
                patch.object(
                    application_knowledge.db,
                    "add_knowledge_document",
                    side_effect=add_document,
                ),
                patch.object(
                    application_knowledge,
                    "require_knowledge_upload_capability",
                ),
                patch.object(
                    application_knowledge,
                    "_project_knowledge_sync_profile_ids",
                    return_value=["aliyun"],
                ),
                patch.object(
                    application_knowledge,
                    "_enqueue_durable_knowledge_sync",
                    return_value={"job_id": "knowledge-sync-1", "status": "PENDING"},
                ),
                patch.object(
                    application_knowledge.db,
                    "list_knowledge_document_syncs",
                    return_value=[],
                ),
                patch.object(
                    application_knowledge.db,
                    "bump_project_knowledge_base_version",
                    return_value={"knowledge_base_version": 1},
                ),
            ):
                result = asyncio.run(
                    application_knowledge.upload_knowledge_document(
                        "project-1",
                        file=upload,
                        doc_type="REFERENCE",
                        doc_version="",
                        status="ACTIVE",
                        effective_from="",
                        effective_to="",
                    )
                )

        self.assertEqual(captured["file_type"], "TXT")
        self.assertEqual(captured["extracted_text"], "")
        self.assertRegex(str(captured["source_sha256"]), r"^[0-9a-f]{64}$")
        self.assertEqual(result["sync_job"]["job_id"], "knowledge-sync-1")


class _BoundedReadSource(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.largest_read = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("object-store uploads must use bounded reads")
        self.largest_read = max(self.largest_read, size)
        return super().read(size)


class _FakeOssBody:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False
        self.block_sizes: list[int] = []

    def iter_bytes(self, *, block_size: int):
        self.block_sizes.append(block_size)
        for offset in range(0, len(self.payload), 3):
            yield self.payload[offset : offset + 3]

    def close(self) -> None:
        self.closed = True


class _FakeOssClient:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.last_body_was_stream = False
        self.last_body: _FakeOssBody | None = None

    def put_object(self, request: dict[str, object]):
        source = request["body"]
        self.last_body_was_stream = hasattr(source, "read") and not isinstance(
            source,
            bytes,
        )
        chunks: list[bytes] = []
        while chunk := source.read(4):
            chunks.append(chunk)
        payload = b"".join(chunks)
        self.objects[str(request["key"])] = {
            "payload": payload,
            "content_type": request["content_type"],
            "metadata": request["metadata"],
        }
        return SimpleNamespace(etag="fake-etag")

    def get_object(self, request: dict[str, object]):
        record = self.objects[str(request["key"])]
        body = _FakeOssBody(record["payload"])
        self.last_body = body
        return SimpleNamespace(body=body)

    def head_object(self, request: dict[str, object]):
        record = self.objects.get(str(request["key"]))
        if record is None:
            error = RuntimeError("missing")
            error.status_code = 404
            raise error
        return SimpleNamespace(
            content_length=len(record["payload"]),
            content_type=record["content_type"],
            metadata=record["metadata"],
            etag="fake-etag",
        )

    def delete_object(self, request: dict[str, object]):
        self.objects.pop(str(request["key"]))


class AliyunOssObjectStoreTests(unittest.TestCase):
    def test_streams_uploads_and_downloads_with_hash_metadata(self) -> None:
        payload = b"aliyun-oss-object" * 100_000
        sha256 = hashlib.sha256(payload).hexdigest()
        source = _BoundedReadSource(payload)
        client = _FakeOssClient()
        store = AliyunOssObjectStore(
            region="cn-hangzhou",
            bucket="private-bucket",
            prefix="reqpilotx/test",
            client=client,
        )

        stored = store.put(
            key="uploads/source.pdf",
            source=source,
            expected_sha256=sha256,
            media_type="application/pdf",
        )

        self.assertTrue(client.last_body_was_stream)
        self.assertLessEqual(source.largest_read, 1024 * 1024)
        self.assertEqual(stored.size, len(payload))
        self.assertEqual(store.stat(stored.key).sha256, sha256)
        with store.open(stored.key) as stream:
            self.assertEqual(stream.read(7), payload[:7])
            self.assertEqual(stream.read(), payload[7:])
        self.assertTrue(client.last_body.closed)
        self.assertEqual(client.last_body.block_sizes, [1024 * 1024])
        self.assertTrue(store.delete(stored.key))
        self.assertFalse(store.delete(stored.key))


class PostgresCompatibilityTests(unittest.TestCase):
    def test_claim_serializes_selection_and_excludes_conflicting_active_lanes(
        self,
    ) -> None:
        class Cursor:
            @staticmethod
            def fetchone():
                return None

        class Connection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple]] = []

            def execute(self, sql, parameters=()):
                self.calls.append((str(sql), tuple(parameters or ())))
                return Cursor()

        class Database:
            def __init__(self, connection) -> None:
                self._connection = connection

            @contextmanager
            def connection(self):
                yield self._connection

        connection = Connection()
        queue = PostgresJobQueue(Database(connection))

        claimed = queue.claim(
            worker_id="worker-a",
            lease_seconds=120,
            accepted_kinds=("document.extract", "document.restore"),
            observed_connection_revision=28,
        )

        self.assertIsNone(claimed)
        claim_sql = connection.calls[-1][0]
        self.assertTrue(
            any("pg_advisory_xact_lock" in sql for sql, _ in connection.calls)
        )
        self.assertIn("active.lease_expires_at >= now()", claim_sql)
        self.assertIn("document.extract", claim_sql)
        self.assertIn("document.restore", claim_sql)
        self.assertIn("structure.optimize", claim_sql)
        self.assertIn("queued.kind = 'knowledge.sync'", claim_sql)
        self.assertIn("active.kind = 'knowledge.sync'", claim_sql)
        self.assertIn("local-extraction-no-llm", claim_sql)
        self.assertIn(
            "queued.payload #>> '{processing_plan,llm_provider}' = 'none'",
            claim_sql,
        )
        self.assertIn("ORDER BY queued.priority DESC, queued.created_at ASC", claim_sql)
        self.assertIn("execution_requirements", claim_sql)
        self.assertIn(
            "document-extraction-execution-requirements/v1",
            claim_sql,
        )
        self.assertIn("required_connection_revision", claim_sql)
        self.assertIn("provider_bindings", claim_sql)
        self.assertIn(28, connection.calls[-1][1])

    def test_runtime_worker_fleet_migration_is_private_and_heartbeat_indexed(
        self,
    ) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0029_runtime_worker_fleet.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS runtime_worker_fleet", migration)
        self.assertIn("accepted_kinds TEXT[]", migration)
        self.assertIn("connection_revision BIGINT", migration)
        self.assertIn("last_heartbeat_at TIMESTAMPTZ", migration)
        self.assertIn("DRAINING", migration)
        self.assertNotIn(" pid ", f" {migration.lower()} ")

    def test_drain_release_returns_unstarted_delivery_without_spending_attempt(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "id": "job-drain",
            "kind": "document.extract",
            "aggregate_type": "sor-document",
            "aggregate_id": "document-a",
            "idempotency_key": "drain-a",
            "payload": {},
            "status": "PENDING",
            "priority": 10,
            "attempts": 0,
            "max_attempts": 3,
            "generation": 1,
            "cancel_requested": False,
            "lease_owner": "",
            "lease_expires_at": None,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
            "progress": {},
            "result": None,
            "error_code": "",
            "error_message": "",
        }

        class Cursor:
            @staticmethod
            def fetchone():
                return row

        class Connection:
            def __init__(self) -> None:
                self.sql = ""
                self.parameters: tuple = ()

            def execute(self, sql, parameters=()):
                self.sql = str(sql)
                self.parameters = tuple(parameters or ())
                return Cursor()

        class Database:
            @contextmanager
            def connection(self):
                yield connection

        connection = Connection()
        queue = PostgresJobQueue(Database())

        released = queue.release_claim(
            job_id="job-drain",
            worker_id="worker-a",
        )

        self.assertEqual(released.status, JobStatus.PENDING)
        self.assertIn("GREATEST(attempts - 1, 0)", connection.sql)
        self.assertIn("lease_owner = ''", connection.sql)
        self.assertIn("cancel_requested THEN 'CANCELLED'", connection.sql)
        self.assertIn("lease_expires_at = NULL", connection.sql)
        self.assertIn("WHEN attempts <= 1 THEN NULL", connection.sql)
        self.assertEqual(connection.parameters, ("job-drain", "worker-a"))

    def test_drain_release_finalizes_a_concurrent_cancel_without_requeue(self) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "id": "job-cancelled-drain",
            "kind": "document.extract",
            "aggregate_type": "sor-document",
            "aggregate_id": "document-a",
            "idempotency_key": "cancelled-drain",
            "payload": {},
            "status": "CANCELLED",
            "priority": 10,
            "attempts": 0,
            "max_attempts": 3,
            "generation": 1,
            "cancel_requested": True,
            "lease_owner": "",
            "lease_expires_at": None,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
            "progress": {},
            "result": None,
            "error_code": "",
            "error_message": "",
        }

        class Cursor:
            @staticmethod
            def fetchone():
                return row

        class Connection:
            def __init__(self) -> None:
                self.sql = ""

            def execute(self, sql, _parameters=()):
                self.sql = str(sql)
                return Cursor()

        connection = Connection()

        class Database:
            @contextmanager
            def connection(self):
                yield connection

        released = PostgresJobQueue(Database()).release_claim(
            job_id="job-cancelled-drain",
            worker_id="worker-a",
        )

        self.assertEqual(released.status, JobStatus.CANCELLED)
        self.assertTrue(released.cancel_requested)
        self.assertEqual(released.attempts, 0)
        self.assertIn("WHEN cancel_requested THEN 'CANCELLED'", connection.sql)
        released_status_sql = connection.sql.split("SET status", 1)[1].split(
            "WHERE", 1
        )[0]
        self.assertNotIn("'CANCEL_REQUESTED'", released_status_sql)

    def test_queue_diagnostics_uses_claim_order_and_live_leases(self) -> None:
        class Cursor:
            @staticmethod
            def fetchone():
                return {"position": 3, "running_count": 1}

        class Connection:
            def __init__(self) -> None:
                self.sql = ""
                self.parameters: tuple = ()

            def execute(self, sql, parameters=()):
                self.sql = str(sql)
                self.parameters = tuple(parameters or ())
                return Cursor()

        class Database:
            def __init__(self, connection) -> None:
                self._connection = connection

            @contextmanager
            def connection(self):
                yield self._connection

        connection = Connection()
        queue = PostgresJobQueue(Database(connection))

        diagnostics = queue.queue_diagnostics("job-target")

        self.assertEqual(
            diagnostics,
            {"position": 3, "ahead_count": 2, "running_count": 1},
        )
        self.assertEqual(connection.parameters, ("job-target",))
        self.assertIn("queued.available_at <= now()", connection.sql)
        self.assertIn("queued.priority > target.priority", connection.sql)
        self.assertIn("queued.created_at < target.created_at", connection.sql)
        self.assertIn("lease_expires_at >= now()", connection.sql)
        self.assertIn(
            "target.payload #>> '{processing_plan,llm_provider}'", connection.sql
        )
        self.assertIn(
            "queued.payload #>> '{processing_plan,llm_provider}'", connection.sql
        )
        self.assertIn("lease_owner NOT LIKE 'local-%%-slot-2-%%'", connection.sql)

    def test_worker_fleet_registration_heartbeat_and_unregister_are_durable(
        self,
    ) -> None:
        class Cursor:
            @staticmethod
            def fetchone():
                return None

        class Connection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple]] = []

            def execute(self, sql, parameters=()):
                self.calls.append((str(sql), tuple(parameters or ())))
                return Cursor()

        connection = Connection()

        class Database:
            @contextmanager
            def connection(self):
                yield connection

        queue = PostgresJobQueue(Database())
        identity = {
            "worker_id": "private-worker-1",
            "slot": "1",
            "claim_profile": "general",
            "accepted_kinds": ("document.extract", "project.export"),
            "connection_revision": 28,
            "source_revision": "a" * 64,
            "runtime_instance_id": "runtime-123",
            "state": "IDLE",
        }

        queue.register_worker(**identity)
        queue.heartbeat_worker(**{**identity, "state": "ACTIVE"})
        queue.unregister_worker(worker_id="private-worker-1")

        self.assertEqual(len(connection.calls), 3)
        self.assertIn("INSERT INTO runtime_worker_fleet", connection.calls[0][0])
        self.assertIn("ON CONFLICT (worker_id) DO UPDATE", connection.calls[0][0])
        self.assertIn("last_heartbeat_at = now()", connection.calls[1][0])
        self.assertIn("DELETE FROM runtime_worker_fleet", connection.calls[2][0])
        self.assertEqual(connection.calls[2][1], ("private-worker-1",))

    def test_worker_fleet_status_is_public_and_revision_aware(self) -> None:
        rows = [
            {
                "worker_id": "must-not-leak-1",
                "slot": "1",
                "claim_profile": "general",
                "accepted_kinds": ["document.extract", "project.export"],
                "connection_revision": 28,
                "source_revision": "a" * 64,
                "runtime_instance_id": "runtime-current",
                "state": "IDLE",
                "fresh": True,
                "last_seen_age_seconds": 1,
            },
            {
                "worker_id": "must-not-leak-2",
                "slot": "2",
                "claim_profile": "general",
                "accepted_kinds": ["document.extract"],
                "connection_revision": 27,
                "source_revision": "b" * 64,
                "runtime_instance_id": "runtime-old",
                "state": "ACTIVE",
                "fresh": True,
                "last_seen_age_seconds": 4,
            },
            {
                "worker_id": "must-not-leak-3",
                "slot": "3",
                "claim_profile": "local-extraction-no-llm",
                "accepted_kinds": ["document.extract"],
                "connection_revision": 28,
                "source_revision": "a" * 64,
                "runtime_instance_id": "runtime-current",
                "state": "IDLE",
                "fresh": True,
                "last_seen_age_seconds": 2,
            },
        ]

        class Cursor:
            @staticmethod
            def fetchall():
                return rows

        class Connection:
            def execute(self, _sql, _parameters=()):
                return Cursor()

        class Database:
            @contextmanager
            def connection(self):
                yield Connection()

        status = PostgresJobQueue(Database()).worker_fleet_status(
            desired_revision=28,
            required_kind="document.extract",
            plan={
                "execution_requirements": {
                    "schema_version": ("document-extraction-execution-requirements/v1"),
                    "connection_revision_required": True,
                    "required_connection_revision": 28,
                },
                "processing_plan": {"llm_provider": "qwen3.8-max"},
                "source_revision": "a" * 64,
                "runtime_instance_id": "runtime-current",
            },
        )

        self.assertEqual(status["schema_version"], "runtime-worker-fleet-status/v1")
        self.assertEqual(status["state"], "READY")
        self.assertEqual(status["live_worker_count"], 3)
        self.assertEqual(status["eligible_worker_count"], 1)
        self.assertEqual(status["stale_revision_worker_count"], 1)
        self.assertEqual(status["workers"][0]["last_seen_age_seconds"], 1)
        serialized = json.dumps(status, sort_keys=True)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("worker_id", serialized)
        self.assertNotIn("pid", serialized.lower())
        self.assertTrue(status["workers"][0]["source_revision_match"])
        self.assertTrue(status["workers"][0]["runtime_instance_match"])
        self.assertTrue(status["workers"][0]["claim_profile_match"])
        self.assertFalse(status["workers"][2]["claim_profile_match"])

        explicitly_revision_bound = PostgresJobQueue(Database()).worker_fleet_status(
            desired_revision=28,
            required_kind="document.extract",
            require_connection_revision=True,
        )
        self.assertTrue(explicitly_revision_bound["revision_required"])
        self.assertEqual(explicitly_revision_bound["eligible_worker_count"], 2)
        self.assertTrue(explicitly_revision_bound["workers"][2]["claim_profile_match"])

        local_plan = PostgresJobQueue(Database()).worker_fleet_status(
            desired_revision=28,
            required_kind="document.extract",
            plan={
                "processing_plan": {"llm_provider": "none"},
                "execution_requirements": {
                    "schema_version": ("document-extraction-execution-requirements/v1"),
                    "connection_revision_required": False,
                    "required_connection_revision": None,
                },
                "source_revision": "a" * 64,
                "runtime_instance_id": "runtime-current",
            },
        )
        self.assertEqual(local_plan["eligible_worker_count"], 2)
        self.assertTrue(local_plan["workers"][2]["claim_profile_match"])

    def test_get_by_idempotency_reads_the_exact_retry_chain_node(self) -> None:
        class Cursor:
            @staticmethod
            def fetchone():
                return None

        class Connection:
            def __init__(self) -> None:
                self.sql = ""
                self.parameters: tuple = ()

            def execute(self, sql, parameters=()):
                self.sql = str(sql)
                self.parameters = tuple(parameters or ())
                return Cursor()

        connection = Connection()

        class Database:
            @contextmanager
            def connection(self):
                yield connection

        result = PostgresJobQueue(Database()).get_by_idempotency(
            kind="document.extract",
            idempotency_key="retry-chain-a",
        )

        self.assertIsNone(result)
        self.assertIn("WHERE kind = %s AND idempotency_key = %s", connection.sql)
        self.assertEqual(
            connection.parameters,
            ("document.extract", "retry-chain-a"),
        )

    def test_cancel_cas_does_not_rewrite_a_terminal_race(self) -> None:
        class Cursor:
            @staticmethod
            def fetchone():
                return None

        class Connection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple]] = []

            def execute(self, sql, parameters=()):
                self.calls.append((str(sql), tuple(parameters or ())))
                return Cursor()

        class Database:
            def __init__(self, connection) -> None:
                self._connection = connection

            @contextmanager
            def connection(self):
                yield self._connection

        connection = Connection()
        queue = PostgresJobQueue(Database(connection))

        cancelled = queue.request_cancel("job-target")

        self.assertIsNone(cancelled)
        update_sql = next(
            sql for sql, _ in connection.calls if "SET cancel_requested" in sql
        )
        self.assertIn(
            "status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')",
            update_sql,
        )
        self.assertEqual(connection.calls[-1][1], ("job-target",))

    def test_supersede_pending_never_cancels_a_claimed_delivery(self) -> None:
        class Cursor:
            @staticmethod
            def fetchone():
                return None

        class Connection:
            def __init__(self) -> None:
                self.sql = ""
                self.parameters: tuple = ()

            def execute(self, sql, parameters=()):
                self.sql = str(sql)
                self.parameters = tuple(parameters or ())
                return Cursor()

        connection = Connection()

        class Database:
            @contextmanager
            def connection(self):
                yield connection

        result = PostgresJobQueue(Database()).supersede_pending(
            "job-target",
            reason_code="connection_revision_superseded",
        )

        self.assertIsNone(result)
        self.assertIn("status = 'PENDING'", connection.sql)
        self.assertIn("lease_owner = ''", connection.sql)
        self.assertNotIn("status IN", connection.sql)
        self.assertEqual(
            connection.parameters,
            ("connection_revision_superseded", "job-target"),
        )

    def test_job_read_reaps_an_expired_exhausted_delivery(self) -> None:
        now = datetime.now(timezone.utc)

        class Cursor:
            def __init__(self, row=None) -> None:
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def __init__(self, *, cancelled: bool) -> None:
                self.calls: list[tuple[str, tuple]] = []
                self.row = {
                    "id": "job-1",
                    "kind": "document.assess",
                    "aggregate_type": "sor-document",
                    "aggregate_id": "document-1",
                    "idempotency_key": "analysis-1",
                    "payload": {},
                    "status": "CANCEL_REQUESTED" if cancelled else "RUNNING",
                    "priority": 0,
                    "attempts": 2,
                    "max_attempts": 2,
                    "generation": 2,
                    "cancel_requested": cancelled,
                    "lease_owner": "lost-worker",
                    "lease_expires_at": now,
                    "available_at": now,
                    "created_at": now,
                    "updated_at": now,
                    "progress": {},
                    "result": None,
                    "error_code": "",
                    "error_message": "",
                }

            def execute(self, sql, parameters=()):
                values = tuple(parameters or ())
                self.calls.append((str(sql), values))
                if "job_attempts_exhausted" in sql:
                    self.row.update(
                        {
                            "status": (
                                "CANCELLED"
                                if self.row["cancel_requested"]
                                else "FAILED"
                            ),
                            "generation": self.row["generation"] + 1,
                            "lease_owner": "",
                            "lease_expires_at": None,
                            "error_code": (
                                ""
                                if self.row["cancel_requested"]
                                else "job_attempts_exhausted"
                            ),
                            "error_message": (
                                ""
                                if self.row["cancel_requested"]
                                else "The worker lease expired after the final delivery attempt."
                            ),
                        }
                    )
                    return Cursor()
                return Cursor(dict(self.row))

        class Database:
            def __init__(self, connection) -> None:
                self._connection = connection

            @contextmanager
            def connection(self):
                yield self._connection

        for cancelled, expected_status in (
            (False, JobStatus.FAILED),
            (True, JobStatus.CANCELLED),
        ):
            with self.subTest(cancelled=cancelled):
                connection = Connection(cancelled=cancelled)
                queue = PostgresJobQueue(Database(connection))

                job = queue.get("job-1")

                self.assertIsNotNone(job)
                self.assertEqual(job.status, expected_status)
                self.assertEqual(job.lease_owner, "")
                self.assertIsNone(job.lease_expires_at)
                self.assertEqual(
                    job.error_code,
                    "" if cancelled else "job_attempts_exhausted",
                )
                self.assertIn("attempts >= max_attempts", connection.calls[0][0])
                self.assertEqual(connection.calls[0][1], ("job-1", "job-1", "", ""))

    def test_placeholders_and_runtime_object_columns_are_translated(self) -> None:
        statement = translate_sql_for_postgres(
            """
            SELECT file_path, payload_path
            FROM knowledge_documents
            WHERE id = ? AND note = 'literal ?'
            """
        )
        self.assertIn("storage_key", statement)
        self.assertIn("payload_storage_key", statement)
        self.assertIn("id = %s", statement)
        self.assertIn("'literal ?'", statement)

    def test_insert_or_ignore_keeps_returning_after_conflict_clause(self) -> None:
        statement = translate_sql_for_postgres(
            "INSERT OR IGNORE INTO projects (id) VALUES (?) RETURNING id"
        )
        self.assertEqual(
            statement,
            "INSERT INTO projects (id) VALUES (%s)  ON CONFLICT DO NOTHING RETURNING  id",
        )

    def test_begin_immediate_uses_postgres_transaction(self) -> None:
        self.assertEqual(translate_sql_for_postgres("BEGIN IMMEDIATE"), "BEGIN")


class AppFactoryBoundaryTests(unittest.TestCase):
    def test_cloud_host_and_origin_are_explicit(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "frontend" / "dist"
        settings = RuntimeSettings(
            deployment_mode=DeploymentMode.CLOUD,
            process_role=ProcessRole.API,
            database_backend=DatabaseBackend.POSTGRESQL,
            database_url="postgresql://db/reqpilotx",
            object_store_backend=ObjectStoreBackend.OSS,
            object_root=Path("/tmp/unused"),
            allowed_hosts=("reqpilot.example.com", "testserver"),
            allowed_origins=("https://reqpilot.example.com",),
            oss_region="cn-hangzhou",
            oss_bucket="private-bucket",
        )
        app = create_http_app(settings=settings, static_dir=static_dir)
        with TestClient(app) as client:
            accepted = client.get(
                "/not-found",
                headers={
                    "host": "reqpilot.example.com",
                    "origin": "https://reqpilot.example.com",
                },
            )
            rejected = client.get(
                "/not-found",
                headers={"host": "evil.example.com"},
            )
        self.assertEqual(accepted.status_code, 404)
        self.assertEqual(rejected.status_code, 400)

    def test_production_app_is_composed_from_named_domain_routers(self) -> None:
        app = create_app()
        self.assertEqual(
            app.state.domain_routers,
            (
                "runtime",
                "projects",
                "ingestion",
                "reading",
                "source-capture",
                "source-item-delivery",
                "source-item-images",
                "agent-workflow",
                "extraction",
                "review",
                "matrix",
                "assessment",
                "knowledge",
                "export",
            ),
        )
        api_routes = [
            route
            for route in app.routes
            if str(getattr(route, "path", "")).startswith("/api/")
        ]
        identities = {
            (
                str(route.path),
                method,
            )
            for route in api_routes
            for method in (route.methods or ())
        }
        self.assertTrue(api_routes)
        self.assertEqual(len(identities), len(api_routes))
        for route in api_routes:
            domain_tags = set(route.tags or ()) & set(app.state.domain_routers)
            self.assertEqual(
                len(domain_tags),
                1,
                msg=f"{route.path} must belong to exactly one named domain router",
            )
        self.assertTrue(
            all(
                any(tag in (route.tags or ()) for route in api_routes)
                for tag in app.state.domain_routers
            )
        )

    def test_main_and_worker_keep_the_composition_boundary(self) -> None:
        app_dir = (
            Path(__file__).resolve().parents[2]
            / "apps/server/src/reqpilotx_server"
        )
        main_source = (app_dir / "main.py").read_text(encoding="utf-8")
        worker_source = (app_dir / "worker" / "handlers.py").read_text(encoding="utf-8")
        extraction_router_source = (
            app_dir / "api" / "routers" / "extraction.py"
        ).read_text(encoding="utf-8")
        application_root = app_dir / "application"
        application_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(application_root.rglob("*.py"))
        )
        runtime_application_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(application_root.rglob("*.py"))
            if "cutover" not in path.parts
        )
        postgres_adapter_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(
                (app_dir / "adapters" / "persistence").glob("postgres_*.py")
            )
        )
        persistence_package_source = (
            app_dir / "adapters" / "persistence" / "__init__.py"
        ).read_text(encoding="utf-8")
        self.assertLessEqual(len(main_source.splitlines()), 40)
        self.assertNotIn("@app.", main_source)
        self.assertNotIn("from app import main", worker_source)
        self.assertNotIn("app.main", worker_source)
        self.assertNotIn("workspace", worker_source)
        self.assertFalse((application_root / "workspace.py").exists())
        self.assertFalse((application_root / "contracts.py").exists())
        self.assertNotIn("shadow", extraction_router_source.lower())
        self.assertNotIn("from fastapi", application_sources)
        self.assertNotIn("fastapi.", application_sources)
        self.assertNotIn("APIRouter", application_sources)
        self.assertNotIn("_router.", application_sources)
        self.assertNotIn("app.worker", application_sources)
        self.assertNotIn("import sqlite3", runtime_application_sources)
        self.assertNotIn(
            "app.adapters.persistence.sqlite",
            runtime_application_sources,
        )
        self.assertNotIn(
            "app.adapters.persistence.sqlite",
            postgres_adapter_sources,
        )
        self.assertNotIn("sqlite_", persistence_package_source)

    def test_worker_registry_binds_application_jobs_without_http_app(self) -> None:
        container = object()
        with patch("app.worker.handlers.runtime.configure_worker_runtime") as configure:
            handlers = build_job_handlers(container)
        configure.assert_called_once_with(container)
        self.assertEqual(
            set(handlers),
            {
                "agent.source-item.extract-loop",
                "agent.source-item.loop",
                "agent.source-item.direct-revision",
                "agent.source-item.images",
                "agent.source-item.classify",
                "agent.source-item.decompose",
                "agent.source-item.analyze",
                "agent.document-map",
                "agent.native-baseline.accept",
                "agent.parent-provision.recognize",
                "agent.source-item.scope-revision",
                "agent.source-item-text.baseline",
                "agent.source-requirement.baseline",
                "agent.structure.handoff",
                "agent.structure.propose",
                "document.extract",
                "structure.optimize",
                "document.restore",
                "document.assess",
                "knowledge.sync",
                "project.export",
            },
        )
        self.assertEqual(
            {handler.__module__ for handler in handlers.values()},
            {
                "reqpilotx_server.application.agent_workflow.document_map_jobs",
                "reqpilotx_server.application.agent_workflow.source_document_loop_jobs",
                "reqpilotx_server.application.agent_workflow.source_item_loop_jobs",
                "reqpilotx_server.application.agent_workflow.source_item_direct_revision",
                "reqpilotx_server.application.agent_workflow.source_item_image_jobs",
                "reqpilotx_server.application.agent_workflow.source_item_classification_jobs",
                "reqpilotx_server.application.agent_workflow.source_item_agent_jobs",
                "reqpilotx_server.application.agent_workflow.native_parent_provision_jobs",
                "reqpilotx_server.application.agent_workflow.native_structure_baseline_jobs",
                "reqpilotx_server.application.agent_workflow.native_structure_handoff_jobs",
                "reqpilotx_server.application.agent_workflow.source_requirement_jobs",
                "reqpilotx_server.application.agent_workflow.structure_proposal_jobs",
                "reqpilotx_server.application.assessment",
                "reqpilotx_server.application.exports",
                "reqpilotx_server.application.extraction.document_jobs",
                "reqpilotx_server.application.extraction.restoration",
                "reqpilotx_server.application.extraction.structure_optimization_jobs",
                "reqpilotx_server.application.knowledge",
            },
        )


class CutoverInventoryTests(unittest.TestCase):
    def test_inventory_is_read_only_and_contains_no_row_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "source.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE projects (id TEXT PRIMARY KEY, customer_text TEXT)"
                )
                connection.execute(
                    "INSERT INTO projects VALUES (?, ?)",
                    ("project-1", "confidential customer content"),
                )
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            report = build_inventory(database)
            after = hashlib.sha256(database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(report["row_count"], 1)
        self.assertNotIn("confidential customer content", str(report))
        self.assertRegex(report["report_sha256"], r"^[0-9a-f]{64}$")

    def test_cutover_plan_replaces_machine_paths_with_object_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_file = root / "customer-source.pdf"
            source_file.write_bytes(b"source-bound-pdf")
            source_sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()
            database = root / "source.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE projects (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        customer_name TEXT,
                        created_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE sor_documents (
                        id TEXT PRIMARY KEY,
                        project_id TEXT,
                        file_name TEXT,
                        file_type TEXT,
                        file_path TEXT,
                        extracted_text TEXT,
                        source_sha256 TEXT,
                        source_size INTEGER,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO projects VALUES (?, ?, ?, ?)",
                    ("project-1", "Project", "Confidential Customer", "2026-07-28"),
                )
                connection.execute(
                    "INSERT INTO sor_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "document-1",
                        "project-1",
                        source_file.name,
                        "pdf",
                        str(source_file),
                        "confidential body",
                        source_sha256,
                        source_file.stat().st_size,
                        "2026-07-28",
                        "2026-07-28",
                    ),
                )

            plan = build_cutover_plan(database)
            rewritten = plan.rewritten_rows["sor_documents"][0]
            summary = plan.public_summary()

        self.assertNotIn("file_path", rewritten)
        self.assertRegex(rewritten["storage_key"], r"^sor-documents/")
        self.assertEqual(len(plan.object_candidates), 1)
        self.assertNotIn("Confidential Customer", str(summary))
        self.assertNotIn("confidential body", str(summary))
        self.assertNotIn(str(source_file), str(summary))

    def test_cutover_plan_rejects_missing_business_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "source.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE sor_documents (
                        id TEXT PRIMARY KEY,
                        project_id TEXT,
                        file_path TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO sor_documents VALUES (?, ?, ?)",
                    ("document-1", "project-1", str(Path(temporary) / "missing.pdf")),
                )
            with self.assertRaisesRegex(CutoverRejected, "missing"):
                build_cutover_plan(database)


class _FakeQueue:
    def __init__(self, job: DurableJob) -> None:
        self.job = job
        self.claimed = False
        self.succeeded: dict | None = None
        self.succeed_error = False

    def claim(self, **_kwargs):
        if self.claimed:
            return None
        self.claimed = True
        return self.job

    def heartbeat(self, **_kwargs):
        return self.job

    def succeed(self, **kwargs):
        if self.succeed_error:
            raise OSError("transient completion transport failure")
        self.succeeded = kwargs
        return self.job

    def fail(self, **_kwargs):
        raise AssertionError("worker should not fail")


class WorkerRuntimeTests(unittest.TestCase):
    def test_worker_refuses_to_start_without_handlers(self) -> None:
        with self.assertRaisesRegex(ValueError, "At least one durable job handler"):
            WorkerRuntime(queue=object(), handlers={})

    def test_worker_dispatches_one_claimed_job(self) -> None:
        now = datetime.now(timezone.utc)
        job = DurableJob(
            job_id="job-1",
            kind="document.extract",
            aggregate_type="document",
            aggregate_id="document-1",
            idempotency_key="extract-v1",
            payload={"document_id": "document-1"},
            status=JobStatus.RUNNING,
            priority=0,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="worker-1",
            lease_expires_at=now,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="",
            error_message="",
        )
        queue = _FakeQueue(job)
        runtime = WorkerRuntime(
            queue=queue,
            handlers={
                "document.extract": (
                    lambda claimed, _context: {"job_id": claimed.job_id}
                )
            },
            worker_id="worker-1",
        )
        self.assertTrue(runtime.run_once())
        self.assertEqual(queue.succeeded["result"], {"job_id": "job-1"})

    def test_worker_reloads_only_at_idle_boundary_and_reports_fleet_state(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        job = DurableJob(
            job_id="job-revision-28",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-1",
            idempotency_key="extract-revision-28",
            payload={
                "execution_requirements": {
                    "schema_version": ("document-extraction-execution-requirements/v1"),
                    "connection_revision_required": True,
                    "required_connection_revision": 28,
                }
            },
            status=JobStatus.RUNNING,
            priority=0,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="worker-private",
            lease_expires_at=now + timedelta(minutes=1),
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="",
            error_message="",
        )
        events: list[tuple[str, object]] = []
        reload_count = 0

        def reload_config() -> int:
            nonlocal reload_count
            reload_count += 1
            events.append(("reload", reload_count))
            return 28

        class Queue(_FakeQueue):
            def register_worker(self, **kwargs):
                events.append(("register", dict(kwargs)))

            def heartbeat_worker(self, **kwargs):
                events.append(("fleet-heartbeat", dict(kwargs)))

            def unregister_worker(self, **kwargs):
                events.append(("unregister", dict(kwargs)))

            def claim(self, **kwargs):
                events.append(("claim", dict(kwargs)))
                return super().claim(**kwargs)

        queue = Queue(job)

        def handler(claimed: DurableJob, _context):
            events.append(("handler", claimed.job_id))
            self.assertEqual(reload_count, 2)
            return {"ok": True}

        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": handler},
            worker_id="worker-private",
            worker_slot="1",
            runtime_instance_id="runtime-123",
            source_revision="a" * 64,
            reload_runtime_config=reload_config,
            fleet_heartbeat_seconds=60.0,
        )

        runtime.start()
        try:
            self.assertTrue(runtime.run_once())
            runtime.request_drain()
        finally:
            runtime.close()

        claim = next(value for name, value in events if name == "claim")
        self.assertEqual(claim["observed_connection_revision"], 28)
        states = [value["state"] for name, value in events if name == "fleet-heartbeat"]
        self.assertIn("ACTIVE", states)
        self.assertIn("IDLE", states)
        self.assertEqual(states[-1], "DRAINING")
        self.assertEqual(events[-1][0], "unregister")
        handler_index = next(
            index for index, (name, _value) in enumerate(events) if name == "handler"
        )
        self.assertNotEqual(events[handler_index - 1][0], "reload")
        self.assertEqual(WorkerRuntime.FLEET_HEARTBEAT_SECONDS, 5.0)

    def test_drain_requested_during_maintenance_stops_before_claim(self) -> None:
        class Queue:
            claimed = False

            def claim(self, **_kwargs):
                self.claimed = True
                return None

        queue = Queue()
        draining = False

        def maintenance() -> None:
            nonlocal draining
            draining = True

        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": lambda *_args: {"ok": True}},
            worker_id="worker-drain",
            maintenance=maintenance,
        )

        self.assertFalse(runtime.run_once(should_stop=lambda: draining))
        self.assertFalse(queue.claimed)

    def test_drain_racing_with_claim_releases_job_without_running_handler(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        job = DurableJob(
            job_id="job-drain-race",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="drain-race",
            payload={"document_id": "document-a"},
            status=JobStatus.RUNNING,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="worker-drain",
            lease_expires_at=now + timedelta(minutes=1),
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="",
            error_message="",
        )
        draining = False

        class Queue:
            released: dict | None = None

            def claim(self, **_kwargs):
                nonlocal draining
                draining = True
                return job

            def release_claim(self, **kwargs):
                self.released = kwargs
                return job

        queue = Queue()
        handler_called = False

        def handler(*_args):
            nonlocal handler_called
            handler_called = True
            return {"ok": True}

        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": handler},
            worker_id="worker-drain",
        )

        self.assertFalse(runtime.run_once(should_stop=lambda: draining))
        self.assertFalse(handler_called)
        self.assertEqual(
            queue.released,
            {"job_id": "job-drain-race", "worker_id": "worker-drain"},
        )

    def test_worker_survives_completion_publication_failure(self) -> None:
        now = datetime.now(timezone.utc)
        job = DurableJob(
            job_id="job-1",
            kind="document.extract",
            aggregate_type="document",
            aggregate_id="document-1",
            idempotency_key="extract-v1",
            payload={"document_id": "document-1"},
            status=JobStatus.RUNNING,
            priority=0,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="worker-1",
            lease_expires_at=now,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="",
            error_message="",
        )
        queue = _FakeQueue(job)
        queue.succeed_error = True
        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": lambda *_args: {"ok": True}},
            worker_id="worker-1",
        )

        self.assertTrue(runtime.run_once())
        self.assertIsNone(queue.succeeded)

    def test_worker_loop_survives_transient_claim_failure(self) -> None:
        now = datetime.now(timezone.utc)
        job = DurableJob(
            job_id="job-after-claim-recovery",
            kind="document.extract",
            aggregate_type="document",
            aggregate_id="document-1",
            idempotency_key="extract-after-claim-recovery",
            payload={"document_id": "document-1"},
            status=JobStatus.RUNNING,
            priority=0,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="worker-1",
            lease_expires_at=now,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="",
            error_message="",
        )

        class _TransientClaimQueue(_FakeQueue):
            def __init__(self, claimed_job: DurableJob) -> None:
                super().__init__(claimed_job)
                self.claim_failures = 0

            def claim(self, **kwargs):
                if self.claim_failures == 0:
                    self.claim_failures += 1
                    raise OSError("transient claim transport failure")
                return super().claim(**kwargs)

        queue = _TransientClaimQueue(job)
        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": lambda *_args: {"ok": True}},
            worker_id="worker-1",
            poll_seconds=0.1,
        )

        with patch("app.worker.runtime.sleep", return_value=None):
            runtime.run_forever(should_stop=lambda: queue.succeeded is not None)

        self.assertEqual(queue.claim_failures, 1)
        self.assertEqual(queue.succeeded["result"], {"ok": True})

    def test_worker_loop_survives_failure_publication_failure(self) -> None:
        now = datetime.now(timezone.utc)

        def job(job_id: str) -> DurableJob:
            return DurableJob(
                job_id=job_id,
                kind="document.extract",
                aggregate_type="document",
                aggregate_id="document-1",
                idempotency_key=f"extract-{job_id}",
                payload={"document_id": "document-1"},
                status=JobStatus.RUNNING,
                priority=0,
                attempts=1,
                max_attempts=3,
                generation=1,
                cancel_requested=False,
                lease_owner="worker-1",
                lease_expires_at=now,
                available_at=now,
                created_at=now,
                updated_at=now,
                progress={},
                result=None,
                error_code="",
                error_message="",
            )

        for publication_error in (
            JobLeaseLostError("stale delivery"),
            OSError("transient failure publication transport failure"),
        ):
            with self.subTest(error_type=type(publication_error).__name__):
                jobs = [job("job-that-fails"), job("job-after-failure-recovery")]

                class _FailurePublicationQueue(_FakeQueue):
                    def __init__(self) -> None:
                        super().__init__(jobs[0])
                        self.remaining_jobs = list(jobs)
                        self.failure_publications = 0

                    def claim(self, **_kwargs):
                        return (
                            self.remaining_jobs.pop(0) if self.remaining_jobs else None
                        )

                    def fail(self, **_kwargs):
                        self.failure_publications += 1
                        raise publication_error

                def handler(claimed: DurableJob, _context):
                    if claimed.job_id == "job-that-fails":
                        raise ValueError("business handler failed")
                    return {"job_id": claimed.job_id}

                queue = _FailurePublicationQueue()
                runtime = WorkerRuntime(
                    queue=queue,
                    handlers={"document.extract": handler},
                    worker_id="worker-1",
                    poll_seconds=0.1,
                )

                runtime.run_forever(should_stop=lambda: queue.succeeded is not None)

                self.assertEqual(queue.failure_publications, 1)
                self.assertEqual(
                    queue.succeeded["result"],
                    {"job_id": "job-after-failure-recovery"},
                )


class DocumentExtractionRetryTests(unittest.TestCase):
    def test_remote_plan_is_rejected_if_settings_change_before_enqueue(self) -> None:
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256="a" * 64,
            profile=profile,
            document=profile.document,
            llm_provider="aliyun",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                llm_transport_provider="aliyun",
            ),
            connection_revision=26,
        )
        fleet = {
            "schema_version": "runtime-worker-fleet-status/v1",
            "state": "READY",
            "live_worker_count": 2,
            "eligible_worker_count": 2,
            "workers": [{"connection_revision": 27}],
        }

        with (
            patch.object(
                document_jobs,
                "_document_extraction_worker_fleet_status",
                return_value=fleet,
            ),
            patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=27,
            ),
            self.assertRaises(ApplicationError) as caught,
        ):
            document_jobs._require_document_extraction_worker_fleet(plan)

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["reason_code"],
            "configuration_changed_during_submission",
        )
        self.assertEqual(
            caught.exception.detail["execution_readiness"]["state"],
            "CONFIGURATION_SUPERSEDED",
        )

    def test_superseded_status_reports_current_worker_fleet_readiness(self) -> None:
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256="a" * 64,
            profile=profile,
            document=profile.document,
            llm_provider="aliyun",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                llm_transport_provider="aliyun",
            ),
            connection_revision=26,
        )
        fleet = {
            "schema_version": "runtime-worker-fleet-status/v1",
            "state": "READY",
            "live_worker_count": 2,
            "eligible_worker_count": 2,
            "workers": [
                {
                    "slot": "1",
                    "connection_revision": 27,
                    "fresh": True,
                    "revision_match": True,
                    "eligible": True,
                }
            ],
        }

        class Queue:
            @staticmethod
            def worker_fleet_status(**_kwargs):
                return fleet

        with (
            patch.object(application_runtime, "_durable_jobs", return_value=Queue()),
            patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=27,
            ),
        ):
            readiness, error_context = (
                document_jobs._document_extraction_execution_status(
                    {
                        "processing_plan": plan.model_dump(mode="json"),
                        "completed_pages": 0,
                    },
                    status="FAILED",
                    error_code=("document_extraction_connection_revision_mismatch"),
                )
            )

        self.assertEqual(readiness["state"], "CONFIGURATION_SUPERSEDED")
        self.assertEqual(readiness["worker_fleet_state"], "READY")
        self.assertEqual(readiness["recommended_action"], "RETRY_CURRENT_CONFIGURATION")
        self.assertEqual(error_context["worker_connection_revisions"], [27])

    def test_connection_failure_status_has_deduplicated_safe_error_context(
        self,
    ) -> None:
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256="a" * 64,
            profile=profile,
            document=profile.document,
            llm_provider="aliyun",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                llm_transport_provider="aliyun",
            ),
            connection_revision=27,
        )
        now = datetime.now(timezone.utc)
        failed = DurableJob(
            job_id="job-connection-mismatch",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="a" * 64,
            payload={
                "project_id": "project-a",
                "document_id": "document-a",
                "source_sha256": "a" * 64,
                "processing_plan": plan.model_dump(mode="json"),
                "execution_requirements": (
                    document_jobs._document_extraction_execution_requirements(plan)
                ),
            },
            status=JobStatus.FAILED,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="document_extraction_connection_revision_mismatch",
            error_message="synthetic mismatch",
        )
        fleet = {
            "schema_version": "runtime-worker-fleet-status/v1",
            "state": "SYNCING",
            "live_worker_count": 2,
            "eligible_worker_count": 0,
            "workers": [
                {
                    "slot": "1",
                    "connection_revision": 26,
                    "fresh": True,
                    "revision_match": False,
                    "eligible": False,
                    "last_seen_age_seconds": 1,
                }
            ],
        }

        class Queue:
            @staticmethod
            def worker_fleet_status(**_kwargs):
                return fleet

        with (
            patch.object(application_runtime, "_durable_jobs", return_value=Queue()),
            patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=27,
            ),
        ):
            snapshot = document_jobs._llm_extraction_snapshot_from_durable(failed)
            serialized = document_jobs._serialize_llm_extraction_job(snapshot)

        self.assertEqual(snapshot["error_cause_code"], "")
        self.assertEqual(serialized["error_cause_code"], "")
        self.assertEqual(
            serialized["execution_readiness"], snapshot["execution_readiness"]
        )
        self.assertEqual(serialized["error_context"], snapshot["error_context"])
        self.assertEqual(
            snapshot["execution_readiness"]["state"],
            "WAITING_FOR_WORKER_SYNC",
        )
        self.assertEqual(
            snapshot["error_context"],
            {
                "failure_phase": "PRE_EXECUTION",
                "pages_started": 0,
                "submitted_connection_revision": 27,
                "api_connection_revision": 27,
                "worker_connection_revisions": [26],
                "recommended_action": "WAIT_FOR_WORKER_SYNC",
            },
        )

    def test_stale_remote_pending_attempt_is_superseded_only_by_pending_cas(
        self,
    ) -> None:
        profile = build_local_rules_profile(project_id="project-a")

        def plan(revision: int):
            return build_resolved_processing_plan(
                project_id="project-a",
                document_id="document-a",
                source_sha256="a" * 64,
                profile=profile,
                document=profile.document,
                llm_provider="aliyun",
                provider_bindings=ResolvedProviderBindingsV1(
                    primary_parser_provider="local:pymupdf",
                    llm_transport_provider="aliyun",
                ),
                connection_revision=revision,
            )

        stale = plan(26)
        replacement = plan(27)
        current = {
            "job_id": "job-stale-pending",
            "status": "PENDING",
            "processing_plan": stale.model_dump(mode="json"),
        }

        class Queue:
            def __init__(self, result) -> None:
                self.result = result
                self.calls: list[tuple[str, str]] = []

            def supersede_pending(self, job_id, *, reason_code):
                self.calls.append((job_id, reason_code))
                return self.result

        superseded_queue = Queue(object())
        with (
            patch.object(
                application_runtime,
                "_durable_jobs",
                return_value=superseded_queue,
            ),
            patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=27,
            ),
        ):
            superseded = document_jobs._supersede_stale_pending_document_extraction(
                current,
                replacement_plan=replacement,
            )

        self.assertIsNone(superseded)
        self.assertEqual(
            superseded_queue.calls,
            [
                (
                    "job-stale-pending",
                    "document_extraction_connection_revision_superseded",
                )
            ],
        )

        race_queue = Queue(None)
        with (
            patch.object(
                application_runtime,
                "_durable_jobs",
                return_value=race_queue,
            ),
            patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=27,
            ),
        ):
            raced = document_jobs._supersede_stale_pending_document_extraction(
                current,
                replacement_plan=replacement,
            )
        self.assertIs(raced, current)

    def test_remote_extraction_is_not_enqueued_without_revision_compatible_worker(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        document_profile = DocumentProcessingProfileV1(
            primary_parser="pymupdf",
            enhancement_chain=("bailian_ocr",),
            auto_escalation=False,
        )
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=document_profile,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                enhancement_providers=("aliyun:bailian-ocr:qwen3.5-ocr",),
            ),
            connection_revision=27,
        )
        fleet = {
            "schema_version": "runtime-worker-fleet-status/v1",
            "state": "SYNCING",
            "desired_connection_revision": 27,
            "revision_required": True,
            "registered_worker_count": 2,
            "live_worker_count": 2,
            "eligible_worker_count": 0,
            "stale_revision_worker_count": 2,
            "workers": [
                {
                    "slot": "1",
                    "claim_profile": "general",
                    "connection_revision": 26,
                    "fresh": True,
                    "revision_match": False,
                    "eligible": False,
                    "last_seen_age_seconds": 1,
                }
            ],
        }

        class Queue:
            def __init__(self) -> None:
                self.enqueue_called = False

            @staticmethod
            def worker_fleet_status(**_kwargs):
                return fleet

            def enqueue(self, **_kwargs):
                self.enqueue_called = True
                raise AssertionError("incompatible work must not be enqueued")

        queue = Queue()
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pdf"
            source.write_bytes(b"%PDF-synthetic")
            with (
                patch(
                    "app.application.extraction.review_support."
                    "_require_project_document",
                    return_value={
                        "id": "document-a",
                        "project_id": "project-a",
                        "source_sha256": source_sha256,
                    },
                ),
                patch.object(
                    application_runtime,
                    "_runtime_record_path",
                    return_value=source,
                ),
                patch.object(application_runtime, "RUNTIME_SETTINGS", settings),
                patch.object(application_runtime, "_durable_jobs", return_value=queue),
                patch.object(
                    document_jobs,
                    "resolve_processing_plan",
                    return_value=plan,
                ),
                patch.object(
                    document_jobs,
                    "_get_llm_extraction_job",
                    return_value=None,
                ),
                patch.object(
                    document_jobs,
                    "_validated_structure_revisions_for_document_rerun",
                    return_value=(),
                ),
                patch.object(
                    document_jobs,
                    "get_runtime_connection_revision",
                    return_value=27,
                ),
                self.assertRaises(ApplicationError) as caught,
            ):
                document_jobs.start_document_extraction(
                    "project-a",
                    "document-a",
                    SimpleNamespace(headers={}),
                )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"],
            "document_extraction_worker_fleet_not_ready",
        )
        self.assertEqual(
            caught.exception.detail["reason_code"],
            "worker_connection_revision_not_acknowledged",
        )
        self.assertFalse(queue.enqueue_called)

    def test_local_durable_plan_ignores_unrelated_connection_revision_change(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            # Compatibility case: old local plans carried the global revision.
            connection_revision=26,
        )
        durable = SimpleNamespace(
            payload={
                "project_id": "project-a",
                "document_id": "document-a",
                "source_sha256": source_sha256,
                "processing_plan": plan.model_dump(mode="json"),
                "execution_requirements": (
                    document_jobs._document_extraction_execution_requirements(plan)
                ),
                "result_runtime_sha256": (
                    document_jobs._document_extraction_result_runtime_sha256(plan)
                ),
                "visual_strategy": {"mode": "OFF"},
                "structure_revision_chain_sha256": "",
            },
            job_id="job-local-old-revision",
        )

        class ReachedExecutionBoundary(Exception):
            pass

        with (
            patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={
                    "id": "document-a",
                    "project_id": "project-a",
                    "source_sha256": source_sha256,
                },
            ),
            patch(
                "app.application.extraction.processing_pipeline."
                "processing_plan_visual_strategy",
                return_value={"mode": "OFF"},
            ),
            patch.object(
                document_jobs,
                "_validated_structure_revisions_for_document_rerun",
                return_value=(),
            ),
            patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=27,
            ),
            patch.object(
                document_jobs,
                "_llm_extraction_snapshot_from_durable",
                side_effect=ReachedExecutionBoundary,
            ),
            self.assertRaises(ReachedExecutionBoundary),
        ):
            document_jobs.run_durable_document_extraction(durable, object())

    def test_queue_capacity_reflects_the_frozen_local_extraction_lane(self) -> None:
        settings = SimpleNamespace(local_worker_count=2)
        with patch.object(application_runtime, "RUNTIME_SETTINGS", settings):
            local_capacity = document_jobs._configured_local_worker_capacity(
                {"processing_plan": {"llm_provider": "none"}}
            )
            semantic_capacity = document_jobs._configured_local_worker_capacity(
                {"processing_plan": {"llm_provider": "aliyun"}}
            )

        self.assertEqual(local_capacity, 2)
        self.assertEqual(semantic_capacity, 1)

    def test_running_no_llm_snapshot_reports_both_occupied_slots(self) -> None:
        now = datetime.now(timezone.utc)
        durable = DurableJob(
            job_id="job-two-slots",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="two-slots-a",
            payload={"processing_plan": {"llm_provider": "none"}},
            status=JobStatus.RUNNING,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="local-runtime-a-slot-1-123456789abc",
            lease_expires_at=now + timedelta(minutes=1),
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="",
            error_message="",
        )

        class _Queue:
            @staticmethod
            def queue_diagnostics(_job_id: str):
                return {"position": None, "ahead_count": 0, "running_count": 2}

        settings = SimpleNamespace(local_worker_count=2)
        with (
            patch.object(application_runtime, "RUNTIME_SETTINGS", settings),
            patch.object(application_runtime, "_durable_jobs", return_value=_Queue()),
        ):
            state = document_jobs._durable_document_extraction_queue_state(durable)

        self.assertEqual(state["running_count"], 2)
        self.assertEqual(state["configured_capacity"], 2)
        self.assertEqual(state["available_slots"], 0)

    def test_durable_running_snapshot_keeps_page_progress_for_status_projection(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        durable = DurableJob(
            job_id="job-page-progress",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="page-progress-a",
            payload={
                "project_id": "project-a",
                "document_id": "document-a",
                "source_sha256": "a" * 64,
            },
            status=JobStatus.RUNNING,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="worker-a",
            lease_expires_at=now + timedelta(minutes=1),
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={
                "stage": "source_ir",
                "completed_pages": 37,
                "total_pages": 137,
                "current_page": 38,
                "page_progress_kind": "SOURCE",
                "page_started_at_utc": "2026-08-18T12:00:00+00:00",
                "last_progress": "已处理 37/137 页，当前第 38 页。",
                "latest_visual_page_event": {
                    "page_number": 37,
                    "provider_id": "bailian-visual-text",
                    "provider_version": "1.4.0+deadline.45s",
                    "request_count": 1,
                    "latency_ms": 36708,
                    "cache_hit": False,
                    "outcome": "READY",
                    "updated_at": "2026-08-18T12:00:37+00:00",
                    "source_text": "must never leave durable status",
                    "request_url": "https://secret.example.test",
                },
            },
            result=None,
            error_code="",
            error_message="",
        )

        snapshot = document_jobs._llm_extraction_snapshot_from_durable(durable)
        public_job = document_jobs._serialize_llm_extraction_job(snapshot)

        self.assertEqual(public_job["status"], "RUNNING")
        self.assertEqual(public_job["queue"]["state"], "CLAIMED")
        self.assertEqual(public_job["progress"]["completed_pages"], 37)
        self.assertEqual(public_job["progress"]["total_pages"], 137)
        self.assertEqual(public_job["progress"]["current_page"], 38)
        self.assertEqual(public_job["progress"]["page_progress_kind"], "SOURCE")
        self.assertEqual(
            public_job["progress"]["page_started_at_utc"],
            "2026-08-18T12:00:00+00:00",
        )
        self.assertEqual(
            public_job["last_progress"],
            "已处理 37/137 页，当前第 38 页。",
        )
        self.assertEqual(
            public_job["latest_visual_page_event"],
            {
                "page_number": 37,
                "provider_id": "bailian-visual-text",
                "provider_version": "1.4.0+deadline.45s",
                "request_count": 1,
                "latency_ms": 36708,
                "cache_hit": False,
                "outcome": "READY",
                "updated_at": "2026-08-18T12:00:37+00:00",
            },
        )

    def test_retry_delivery_clears_stale_page_progress_before_new_pages(
        self,
    ) -> None:
        document_id = "document-retry-progress"
        job_id = "llm-extraction-" + "d" * 32
        live = {
            "job_id": job_id,
            "project_id": "project-a",
            "document_id": document_id,
            "source_sha256": "a" * 64,
            "status": "RUNNING",
            "stage": "preflight",
            "partial_revision": 1,
            "completed_pages": 37,
            "total_pages": 137,
            "current_page": 38,
            "page_progress_kind": "SOURCE",
            "latest_visual_page_event": {
                "page_number": 37,
                "provider_id": "stale-provider",
                "provider_version": "stale-version",
                "request_count": 1,
                "latency_ms": 100,
                "cache_hit": False,
                "outcome": "READY",
                "updated_at": "2026-08-18T12:00:00+00:00",
            },
        }
        document_jobs._reset_document_extraction_page_progress(live)

        first_status = document_jobs._serialize_llm_extraction_job(live)
        self.assertEqual(first_status["progress"]["completed_pages"], 0)
        self.assertEqual(first_status["progress"]["total_pages"], 0)
        self.assertIsNone(first_status["progress"]["current_page"])
        self.assertEqual(first_status["progress"]["page_progress_kind"], "")
        self.assertEqual(first_status["progress"]["page_started_at_utc"], "")
        self.assertIsNone(first_status["latest_visual_page_event"])

        with document_jobs._LLM_EXTRACTION_JOB_LOCK:
            document_jobs._LLM_EXTRACTION_JOBS[document_id] = live
        try:
            heartbeat = document_jobs._LlmExtractionPreparationHeartbeat(
                document_id,
                job_id,
                interval_seconds=60.0,
            )
            heartbeat.publish_source_page_progress(1, 137, 2)
            with document_jobs._LLM_EXTRACTION_JOB_LOCK:
                after_page = dict(document_jobs._LLM_EXTRACTION_JOBS[document_id])
        finally:
            with document_jobs._LLM_EXTRACTION_JOB_LOCK:
                document_jobs._LLM_EXTRACTION_JOBS.pop(document_id, None)

        next_status = document_jobs._serialize_llm_extraction_job(after_page)
        self.assertEqual(next_status["progress"]["completed_pages"], 1)
        self.assertEqual(next_status["progress"]["total_pages"], 137)
        self.assertEqual(next_status["progress"]["current_page"], 2)
        self.assertEqual(next_status["progress"]["page_progress_kind"], "SOURCE")
        self.assertTrue(next_status["progress"]["page_started_at_utc"])

    def test_previous_local_artifact_remains_read_only_during_visual_rerun(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        profile = build_local_rules_profile(project_id="project-a")
        previous_plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=4,
        )
        visual_plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=DocumentProcessingProfileV1(
                primary_parser="pymupdf",
                enhancement_chain=("bailian_layout",),
                complex_vision=None,
                auto_escalation=False,
            ),
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                enhancement_providers=("aliyun:bailian-layout:qwen3.7-flash",),
            ),
            connection_revision=5,
        )
        previous_runtime = document_jobs._document_extraction_result_runtime_sha256(
            previous_plan
        )
        now = datetime.now(timezone.utc)
        previous = DurableJob(
            job_id="job-previous",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="previous-a",
            payload={
                "project_id": "project-a",
                "document_id": "document-a",
                "source_sha256": source_sha256,
                "processing_plan": previous_plan.model_dump(mode="json"),
                "result_runtime_sha256": previous_runtime,
            },
            status=JobStatus.SUCCEEDED,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result={
                "schema_version": "document-extraction-result-artifact/v1",
                "sha256": "b" * 64,
                "storage_key": "extraction-results/previous.json",
            },
            error_code="",
            error_message="",
        )

        class _Queue:
            @staticmethod
            def get_latest_succeeded(**_kwargs):
                return previous

        current_job = {
            "job_id": "job-current",
            "status": "PENDING",
            "source_sha256": source_sha256,
            "processing_plan": visual_plan.model_dump(mode="json"),
            "result_runtime_sha256": (
                document_jobs._document_extraction_result_runtime_sha256(visual_plan)
            ),
        }
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        with (
            patch.object(application_runtime, "RUNTIME_SETTINGS", settings),
            patch.object(application_runtime, "_durable_jobs", return_value=_Queue()),
        ):
            summary = document_jobs._previous_document_extraction_artifact_summary(
                project_id="project-a",
                document_id="document-a",
                current_job=current_job,
            )

        assert summary is not None
        self.assertTrue(summary["runtime_compatible"])
        self.assertEqual(summary["read_mode"], "READ_ONLY")
        self.assertTrue(summary["can_browse_structure"])
        self.assertFalse(summary["can_continue_structure"])

    def test_previous_artifact_uses_current_code_runtime_not_two_stale_job_hashes(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=4,
        )
        stale_runtime = document_jobs._document_extraction_result_runtime_sha256(plan)
        now = datetime.now(timezone.utc)
        previous = DurableJob(
            job_id="job-previous",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="previous-a",
            payload={
                "project_id": "project-a",
                "document_id": "document-a",
                "source_sha256": source_sha256,
                "processing_plan": plan.model_dump(mode="json"),
                "result_runtime_sha256": stale_runtime,
            },
            status=JobStatus.SUCCEEDED,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result={
                "schema_version": "document-extraction-result-artifact/v1",
                "sha256": "b" * 64,
                "storage_key": "extraction-results/previous.json",
            },
            error_code="",
            error_message="",
        )

        class _Queue:
            @staticmethod
            def get_latest_succeeded(**_kwargs):
                return previous

        current_job = {
            "job_id": "job-current",
            "status": "PENDING",
            "source_sha256": source_sha256,
            "processing_plan": plan.model_dump(mode="json"),
            "result_runtime_sha256": stale_runtime,
        }
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        with (
            patch.object(application_runtime, "RUNTIME_SETTINGS", settings),
            patch.object(application_runtime, "_durable_jobs", return_value=_Queue()),
            patch.object(
                document_jobs,
                "_document_extraction_result_runtime_sha256",
                return_value="c" * 64,
            ),
        ):
            summary = document_jobs._previous_document_extraction_artifact_summary(
                project_id="project-a",
                document_id="document-a",
                current_job=current_job,
            )

        assert summary is not None
        self.assertFalse(summary["runtime_compatible"])
        self.assertEqual(summary["read_mode"], "HISTORICAL_ONLY")
        self.assertFalse(summary["can_browse_structure"])

    def test_pending_snapshot_explains_queue_position_and_worker_health(self) -> None:
        now = datetime.now(timezone.utc)
        queued = DurableJob(
            job_id="job-queued",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="queued-a",
            payload={
                "project_id": "project-a",
                "document_id": "document-a",
                "source_sha256": "a" * 64,
                "stage": "queued",
            },
            status=JobStatus.PENDING,
            priority=10,
            attempts=0,
            max_attempts=3,
            generation=0,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="",
            error_message="",
        )

        class _Queue:
            def queue_diagnostics(self, job_id: str):
                self.requested_job_id = job_id
                return {
                    "position": 2,
                    "ahead_count": 1,
                    "running_count": 1,
                }

        queue = _Queue()
        with patch("app.application.runtime._durable_jobs", return_value=queue):
            snapshot = document_jobs._llm_extraction_snapshot_from_durable(queued)

        self.assertEqual(queue.requested_job_id, queued.job_id)
        self.assertEqual(snapshot["queue"]["state"], "WAITING_BEHIND_JOBS")
        self.assertEqual(snapshot["queue"]["position"], 2)
        self.assertEqual(snapshot["queue"]["ahead_count"], 1)
        self.assertEqual(snapshot["queue"]["running_count"], 1)
        self.assertTrue(snapshot["queue"]["can_cancel"])

    def test_unclaimed_first_job_is_reported_as_recoverable_worker_gap(self) -> None:
        now = datetime.now(timezone.utc)
        queued = DurableJob(
            job_id="job-unclaimed",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="unclaimed-a",
            payload={
                "project_id": "project-a",
                "document_id": "document-a",
                "source_sha256": "a" * 64,
                "stage": "queued",
            },
            status=JobStatus.PENDING,
            priority=10,
            attempts=0,
            max_attempts=3,
            generation=0,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now.replace(year=now.year - 1),
            created_at=now.replace(year=now.year - 1),
            updated_at=now.replace(year=now.year - 1),
            progress={},
            result=None,
            error_code="",
            error_message="",
        )

        class _Queue:
            @staticmethod
            def queue_diagnostics(_job_id: str):
                return {"position": 1, "ahead_count": 0, "running_count": 0}

        with patch("app.application.runtime._durable_jobs", return_value=_Queue()):
            snapshot = document_jobs._llm_extraction_snapshot_from_durable(queued)

        self.assertEqual(snapshot["queue"]["state"], "WORKER_NOT_CLAIMING")
        self.assertTrue(snapshot["queue"]["recoverable"])

    def test_retry_becomes_stalled_only_after_its_runnable_window(self) -> None:
        now = datetime.now(timezone.utc)

        class _Queue:
            @staticmethod
            def queue_diagnostics(_job_id: str):
                return {"position": 1, "ahead_count": 0, "running_count": 0}

        def queued(*, runnable_seconds: int) -> DurableJob:
            return DurableJob(
                job_id=f"job-retry-{runnable_seconds}",
                kind="document.extract",
                aggregate_type="sor-document",
                aggregate_id="document-a",
                idempotency_key=f"retry-{runnable_seconds}",
                payload={
                    "project_id": "project-a",
                    "document_id": "document-a",
                    "source_sha256": "a" * 64,
                    "stage": "queued",
                },
                status=JobStatus.PENDING,
                priority=10,
                attempts=1,
                max_attempts=3,
                generation=1,
                cancel_requested=False,
                lease_owner="",
                lease_expires_at=None,
                available_at=now - timedelta(seconds=runnable_seconds),
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=2),
                progress={},
                result=None,
                error_code="",
                error_message="",
            )

        with patch("app.application.runtime._durable_jobs", return_value=_Queue()):
            just_runnable = document_jobs._llm_extraction_snapshot_from_durable(
                queued(runnable_seconds=10)
            )
            stalled = document_jobs._llm_extraction_snapshot_from_durable(
                queued(runnable_seconds=60)
            )

        self.assertEqual(just_runnable["queue"]["state"], "WAITING_FOR_WORKER")
        self.assertLess(just_runnable["queue"]["runnable_wait_seconds"], 45)
        self.assertEqual(stalled["queue"]["state"], "WORKER_NOT_CLAIMING")
        self.assertGreaterEqual(stalled["queue"]["runnable_wait_seconds"], 45)

    def test_failed_durable_result_creates_a_source_bound_retry_job(self) -> None:
        source_sha256 = "a" * 64
        profile = build_local_rules_profile(project_id="project-a")
        processing_plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=1,
        )
        plan_payload = processing_plan.model_dump(mode="json")
        visual_strategy = {
            "schema_version": "extraction-visual-strategy/v1",
            "mode": "OFF",
        }
        result_runtime_sha256 = (
            document_jobs._document_extraction_result_runtime_sha256(processing_plan)
        )
        strategy_sha256 = hashlib.sha256(
            json.dumps(
                visual_strategy,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        base_idempotency_key = (
            document_jobs._document_extraction_durable_idempotency_key(
                source_sha256=source_sha256,
                processing_plan_sha256=processing_plan.canonical_sha256,
                visual_strategy_sha256=strategy_sha256,
                structure_revision_chain_sha256="",
                result_runtime_sha256=result_runtime_sha256,
            )
        )
        now = datetime.now(timezone.utc)
        failed_payload = {
            "project_id": "project-a",
            "document_id": "document-a",
            "source_sha256": source_sha256,
            "processing_plan": plan_payload,
            "result_runtime_sha256": result_runtime_sha256,
            "visual_strategy": visual_strategy,
            "structure_revision_chain_sha256": "",
        }
        failed = DurableJob(
            job_id="job-failed",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key=base_idempotency_key,
            payload=failed_payload,
            status=JobStatus.FAILED,
            priority=10,
            attempts=3,
            max_attempts=3,
            generation=3,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error_code="worker_failed",
            error_message="synthetic failure",
        )
        first_retry_key = document_jobs._document_extraction_retry_idempotency_key(
            base_idempotency_key=base_idempotency_key,
            retry_of_job_id=failed.job_id,
        )
        first_retry_failed = DurableJob(
            job_id="job-first-retry-failed",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key=first_retry_key,
            payload={**failed_payload, "retry_of_job_id": failed.job_id},
            status=JobStatus.FAILED,
            priority=10,
            attempts=3,
            max_attempts=3,
            generation=3,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now + timedelta(milliseconds=500),
            updated_at=now + timedelta(milliseconds=500),
            progress={},
            result=None,
            error_code="worker_failed_again",
            error_message="synthetic retry failure",
        )
        other_plan_failure = DurableJob(
            job_id="job-other-plan-failed",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id="document-a",
            idempotency_key="b" * 64,
            payload={
                **failed_payload,
                "processing_plan": {"canonical_sha256": "b" * 64},
            },
            status=JobStatus.FAILED,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now + timedelta(seconds=1),
            updated_at=now + timedelta(seconds=1),
            progress={},
            result=None,
            error_code="other_plan_failed",
            error_message="synthetic other-plan failure",
        )

        class _Queue:
            def __init__(self) -> None:
                self.enqueued: dict[str, object] | None = None

            def get_latest(self, **_kwargs):
                # The aggregate's latest attempt belongs to another plan.  The
                # exact retry lineage must still be found by idempotency key.
                return other_plan_failure

            def get_by_idempotency(self, *, kind, idempotency_key):
                self.assert_kind = kind
                return {
                    base_idempotency_key: failed,
                    first_retry_key: first_retry_failed,
                }.get(idempotency_key)

            def enqueue(self, **kwargs):
                self.enqueued = dict(kwargs)
                if kwargs["idempotency_key"] == base_idempotency_key:
                    # PostgreSQL reuses the already terminal base identity.
                    return failed
                return DurableJob(
                    job_id="job-retry",
                    kind=str(kwargs["kind"]),
                    aggregate_type=str(kwargs["aggregate_type"]),
                    aggregate_id=str(kwargs["aggregate_id"]),
                    idempotency_key=str(kwargs["idempotency_key"]),
                    payload=dict(kwargs["payload"]),
                    status=JobStatus.PENDING,
                    priority=int(kwargs["priority"]),
                    attempts=0,
                    max_attempts=int(kwargs["max_attempts"]),
                    generation=0,
                    cancel_requested=False,
                    lease_owner="",
                    lease_expires_at=None,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                    progress={},
                    result=None,
                    error_code="",
                    error_message="",
                )

        queue = _Queue()
        with patch(
            "app.application.runtime._durable_jobs",
            return_value=queue,
        ):
            snapshot = document_jobs._enqueue_durable_llm_document_extraction(
                project_id="project-a",
                document={
                    "id": "document-a",
                    "source_sha256": source_sha256,
                },
                processing_plan=processing_plan,
                visual_provider="off",
                visual_strategy=visual_strategy,
            )

        self.assertEqual(snapshot["status"], "PENDING")
        self.assertIsNotNone(queue.enqueued)
        assert queue.enqueued is not None
        retry_payload = queue.enqueued["payload"]
        self.assertEqual(retry_payload["retry_of_job_id"], first_retry_failed.job_id)
        self.assertNotEqual(
            queue.enqueued["idempotency_key"],
            base_idempotency_key,
        )
        self.assertEqual(
            queue.enqueued["idempotency_key"],
            document_jobs._document_extraction_retry_idempotency_key(
                base_idempotency_key=base_idempotency_key,
                retry_of_job_id=first_retry_failed.job_id,
            ),
        )


class DocumentExtractionResultArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        document_jobs._DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE.clear()

    def tearDown(self) -> None:
        document_jobs._DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE.clear()

    def test_large_result_is_object_backed_and_restored_from_compact_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalObjectStore(Path(temporary) / "objects")
            project_id = "project-1"
            document_id = "document-1"
            source_sha256 = "a" * 64
            result = {
                "project_id": project_id,
                "document_id": document_id,
                "processing_plan": {"source_sha256": source_sha256},
                "content": "x" * (2 * 1024 * 1024),
            }
            with patch.object(
                application_runtime.RUNTIME_STATE,
                "container",
                SimpleNamespace(object_store=store),
            ):
                receipt = document_jobs._persist_document_extraction_result_artifact(
                    result,
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                )
                restored = document_jobs._load_document_extraction_result_artifact(
                    receipt,
                    expected_project_id=project_id,
                    expected_document_id=document_id,
                    expected_source_sha256=source_sha256,
                )

            self.assertEqual(restored, result)
            self.assertLess(len(str(receipt)), 1_000)
            self.assertGreater(receipt["size"], 2 * 1024 * 1024)
            self.assertRegex(receipt["sha256"], r"^[0-9a-f]{64}$")

    def test_verified_artifact_bytes_are_reused_without_sharing_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalObjectStore(Path(temporary) / "objects")
            project_id = "project-1"
            document_id = "document-1"
            source_sha256 = "b" * 64
            result = {
                "project_id": project_id,
                "document_id": document_id,
                "processing_plan": {"source_sha256": source_sha256},
                "content": {"value": "source-bound"},
            }
            with patch.object(
                application_runtime.RUNTIME_STATE,
                "container",
                SimpleNamespace(object_store=store),
            ):
                receipt = document_jobs._persist_document_extraction_result_artifact(
                    result,
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                )
                with (
                    patch.object(store, "stat", wraps=store.stat) as stat,
                    patch.object(store, "open", wraps=store.open) as open_object,
                ):
                    first = document_jobs._load_document_extraction_result_artifact(
                        receipt,
                        expected_project_id=project_id,
                        expected_document_id=document_id,
                        expected_source_sha256=source_sha256,
                    )
                    first["content"]["value"] = "mutated"
                    second = document_jobs._load_document_extraction_result_artifact(
                        receipt,
                        expected_project_id=project_id,
                        expected_document_id=document_id,
                        expected_source_sha256=source_sha256,
                    )

            self.assertEqual(second["content"]["value"], "source-bound")
            self.assertIsNot(first, second)
            self.assertEqual(stat.call_count, 1)
            self.assertEqual(open_object.call_count, 1)


if __name__ == "__main__":
    unittest.main()
