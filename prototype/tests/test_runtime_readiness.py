from __future__ import annotations

import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main


class RuntimeReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self.tmpdir.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.tmp_path))
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.tmp_path / "app.db"))
        self.exit_stack.enter_context(patch.object(main, "STORAGE_DIR", self.tmp_path))
        self.exit_stack.enter_context(patch.object(main, "UPLOADS_DIR", self.tmp_path / "uploads"))
        self.exit_stack.enter_context(patch.object(main, "KNOWLEDGE_DIR", self.tmp_path / "knowledge"))
        self.exit_stack.enter_context(patch.object(main, "EXPORTS_DIR", self.tmp_path / "exports"))
        self.exit_stack.enter_context(patch.object(main, "TABLE_SNAPSHOT_DIR", self.tmp_path / "table_snapshots"))
        self.exit_stack.enter_context(
            patch.dict(
                main._APP_STARTUP_STATE,
                {"initialized": False, "error": "", "checked_at": ""},
                clear=True,
            )
        )
        self.exit_stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces"),
                    "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                    "REQPILOT_RETRIEVAL_BACKEND": "local",
                    "REQPILOT_AZURE_AUTH_MODE": "auto",
                    "REQPILOT_ALIYUN_ASSESSMENT_MODE": "llm_shadow",
                    "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "",
                    "REQPILOT_ALIYUN_API_KEY": "",
                    "REQPILOT_DASHSCOPE_API_KEY": "",
                    "DASHSCOPE_API_KEY": "",
                    "REQPILOT_DASHSCOPE_BASE_URL": "",
                    "REQPILOT_ALIYUN_CHAT_MODEL": "",
                    "REQPILOT_ALIYUN_EMBEDDING_MODEL": "",
                    "REQPILOT_DASHVECTOR_API_KEY": "",
                    "REQPILOT_ALIYUN_DASHVECTOR_API_KEY": "",
                    "DASHVECTOR_API_KEY": "",
                    "REQPILOT_DASHVECTOR_ENDPOINT": "",
                    "REQPILOT_ALIYUN_DASHVECTOR_ENDPOINT": "",
                    "DASHVECTOR_ENDPOINT": "",
                    "REQPILOT_DASHVECTOR_COLLECTION": "",
                    "REQPILOT_ALIYUN_DASHVECTOR_COLLECTION": "",
                    "REQPILOT_QDRANT_URL": "",
                    "REQPILOT_ALIYUN_QDRANT_URL": "",
                    "QDRANT_URL": "",
                    "REQPILOT_QDRANT_API_KEY": "",
                    "REQPILOT_ALIYUN_QDRANT_API_KEY": "",
                    "QDRANT_API_KEY": "",
                    "REQPILOT_QDRANT_COLLECTION": "",
                    "REQPILOT_ALIYUN_QDRANT_COLLECTION": "",
                    "AZURE_OPENAI_ENDPOINT": "",
                    "AZURE_OPENAI_CHAT_DEPLOYMENT": "",
                    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "",
                    "AZURE_OPENAI_API_KEY": "",
                    "AZURE_AI_SEARCH_ENDPOINT": "",
                    "AZURE_AI_SEARCH_API_KEY": "",
                    "AZURE_AI_SEARCH_INDEX_NAME": "",
                },
                clear=False,
            )
        )
        db.init_db()

    def tearDown(self) -> None:
        self.exit_stack.close()
        db_file = self.tmp_path / "app.db"
        for _ in range(5):
            try:
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{db_file}{suffix}")
                    if sidecar.exists():
                        sidecar.unlink()
                self.tmpdir.cleanup()
                return
            except OSError:
                time.sleep(0.05)
        try:
            self.tmpdir.cleanup()
        except OSError:
            pass

    def test_health_reports_ready_when_runtime_dependencies_are_available(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checks"]["startup"]["status"], "ok")
        self.assertEqual(payload["checks"]["storage"]["status"], "ok")
        self.assertEqual(payload["checks"]["sqlite"]["status"], "ok")
        self.assertEqual(payload["checks"]["azure"]["status"], "not_applicable")
        self.assertEqual(payload["checks"]["aliyun"]["status"], "error")
        self.assertEqual(payload["ai"]["runnable_profiles"], ["azure"])

    def test_readiness_reports_missing_azure_configuration_for_llm_mode(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "llm_shadow",
                "REQPILOT_RETRIEVAL_BACKEND": "azure_sdk",
                "REQPILOT_AZURE_AUTH_MODE": "api_key",
            },
            clear=False,
        ):
            with TestClient(main.app) as client:
                response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["status"], "not_ready")
        self.assertEqual(payload["checks"]["azure"]["status"], "error")
        self.assertEqual(payload["checks"]["aliyun"]["status"], "error")
        missing = payload["checks"]["azure"]["missing"]
        self.assertIn("AZURE_OPENAI_ENDPOINT", missing)
        self.assertIn("AZURE_OPENAI_CHAT_DEPLOYMENT", missing)
        self.assertIn("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", missing)
        self.assertIn("AZURE_AI_SEARCH_ENDPOINT", missing)
        self.assertIn("AZURE_OPENAI_API_KEY", missing)

    def test_health_reports_startup_failure_without_crashing_app(self) -> None:
        with patch.object(main.db, "init_db", side_effect=RuntimeError("db unavailable during startup")):
            with TestClient(main.app) as client:
                response = client.get("/api/health")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["checks"]["startup"]["status"], "error")
        self.assertIn("db unavailable during startup", payload["checks"]["startup"]["detail"])

    def test_readiness_reports_aliyun_qdrant_unreachable_without_affecting_azure(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REQPILOT_ALIYUN_API_KEY": "aliyun-key",
                "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.7-plus",
                "REQPILOT_ALIYUN_EMBEDDING_MODEL": "text-embedding-v4",
                "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "qdrant",
                "REQPILOT_ALIYUN_QDRANT_URL": "http://localhost:6333",
                "REQPILOT_ALIYUN_QDRANT_COLLECTION": "reqpilot-knowledge-aliyun-local",
            },
            clear=True,
        ):
            with patch("app.runtime_readiness._probe_qdrant_endpoint", return_value={"status": "error", "detail": "Qdrant local service is unreachable: connection refused.", "url": "http://localhost:6333"}):
                with TestClient(main.app) as client:
                    response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["checks"]["aliyun"]["status"], "error")
        self.assertEqual(payload["checks"]["aliyun"]["retrieval_backend"], "qdrant")
        self.assertIn("ALIYUN_QDRANT_UNREACHABLE", payload["checks"]["aliyun"]["missing"])
        self.assertNotIn("aliyun", payload["ai"]["runnable_profiles"])

    def test_readiness_reports_non_ascii_aliyun_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REQPILOT_ALIYUN_API_KEY": "sk-测试key",
                "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.7-plus",
                "REQPILOT_ALIYUN_EMBEDDING_MODEL": "text-embedding-v4",
                "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "qdrant",
                "REQPILOT_ALIYUN_QDRANT_URL": "http://localhost:6333",
                "REQPILOT_ALIYUN_QDRANT_COLLECTION": "reqpilot-knowledge-aliyun-local",
            },
            clear=True,
        ):
            with TestClient(main.app) as client:
                response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["checks"]["aliyun"]["status"], "error")
        self.assertIn("REQPILOT_ALIYUN_API_KEY_NON_ASCII", payload["checks"]["aliyun"]["missing"])
        self.assertIn("non-ASCII", payload["checks"]["aliyun"]["detail"])
        self.assertNotIn("aliyun", payload["ai"]["runnable_profiles"])

    def test_readiness_reports_azure_and_aliyun_as_runnable_when_local_qdrant_is_ready(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces"),
                "REQPILOT_AI_ASSESSMENT_MODE": "llm_shadow",
                "REQPILOT_RETRIEVAL_BACKEND": "azure_sdk",
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-5.4",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
                "REQPILOT_ALIYUN_API_KEY": "aliyun-key",
                "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.7-plus",
                "REQPILOT_ALIYUN_EMBEDDING_MODEL": "text-embedding-v4",
                "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "qdrant",
                "REQPILOT_ALIYUN_QDRANT_URL": "http://localhost:6333",
                "REQPILOT_ALIYUN_QDRANT_COLLECTION": "reqpilot-knowledge-aliyun-local",
            },
            clear=True,
        ):
            with patch("app.runtime_readiness._probe_qdrant_endpoint", return_value={"status": "ok", "detail": "Qdrant local service responded successfully.", "url": "http://localhost:6333", "http_status": 200}):
                with TestClient(main.app) as client:
                    response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["checks"]["azure"]["status"], "ok")
        self.assertEqual(payload["checks"]["aliyun"]["status"], "ok")
        self.assertEqual(payload["ai"]["runnable_profiles"], ["aliyun", "azure"])

    def test_readiness_allows_aliyun_only_runtime_when_azure_is_missing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces"),
                "REQPILOT_AI_ASSESSMENT_MODE": "llm_shadow",
                "REQPILOT_RETRIEVAL_BACKEND": "azure_sdk",
                "REQPILOT_ALIYUN_API_KEY": "aliyun-key",
                "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.7-plus",
                "REQPILOT_ALIYUN_EMBEDDING_MODEL": "text-embedding-v4",
                "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "qdrant",
                "REQPILOT_ALIYUN_QDRANT_URL": "http://localhost:6333",
                "REQPILOT_ALIYUN_QDRANT_COLLECTION": "reqpilot-knowledge-aliyun-local",
            },
            clear=True,
        ):
            with patch("app.runtime_readiness._probe_qdrant_endpoint", return_value={"status": "ok", "detail": "Qdrant local service responded successfully.", "url": "http://localhost:6333", "http_status": 200}):
                with TestClient(main.app) as client:
                    response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["checks"]["azure"]["status"], "error")
        self.assertEqual(payload["checks"]["aliyun"]["status"], "ok")
        self.assertEqual(payload["ai"]["runnable_profiles"], ["aliyun"])
