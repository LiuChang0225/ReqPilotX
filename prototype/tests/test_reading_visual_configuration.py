from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routers import runtime as runtime_router
from app.application import runtime as runtime_application
from app.application.reading_visual_configuration import (
    READING_VISUAL_BILLING_BASIS,
    READING_VISUAL_MODEL,
    READING_VISUAL_PROVIDER,
    READING_VISUAL_SETUP_SCHEMA_VERSION,
    build_reading_visual_setup,
    load_reading_visual_environment,
    reading_visual_persisted_values,
)
from app.application.runtime_contracts import RuntimeAIProviderSetupRequest
from app.services import ai_copilot
from app.application.errors import ApplicationError


class ReadingVisualConfigurationTests(unittest.TestCase):
    def test_runtime_get_embeds_the_versioned_reading_visual_section(self) -> None:
        base = {
            "default_profile_id": "aliyun",
            "profiles": [],
            "azure": {},
            "aliyun": {},
            "connection_revision": 11,
        }
        with patch.object(
            runtime_application,
            "build_runtime_ai_provider_setup",
            return_value=base,
        ), patch.object(
            runtime_application,
            "get_runtime_aliyun_connection_probe_receipt",
            return_value=None,
        ), patch.object(
            runtime_application,
            "list_ai_copilot_profiles",
            return_value=[],
        ), patch.object(
            runtime_application,
            "get_runtime_capabilities_route",
            return_value={"capabilities": []},
        ):
            payload = runtime_application._build_runtime_ai_provider_setup_response()

        reading_visual = payload["aliyun"]["reading_visual"]
        self.assertEqual(
            reading_visual["schema_version"],
            READING_VISUAL_SETUP_SCHEMA_VERSION,
        )
        self.assertEqual(reading_visual["connection_revision"], 11)
        self.assertIn("reason_code", reading_visual)
        self.assertIn("message", reading_visual)

    def test_setup_contract_is_fixed_config_only_and_declares_billing(self) -> None:
        values = reading_visual_persisted_values()
        source = {
            **values,
            "REQPILOT_READING_ALIYUN_API_KEY": "sk-test-0000000000000000000000000000",
            "REQPILOT_READING_ALIYUN_BASE_URL": (
                "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
        }

        payload = build_reading_visual_setup(
            connection_revision=7,
            environment=source,
        )

        unavailable = build_reading_visual_setup(
            connection_revision=8,
            environment={
                **values,
                "REQPILOT_READING_ALIYUN_API_KEY": "aliyun-key",
            },
        )
        self.assertTrue(unavailable["configured"])
        self.assertFalse(unavailable["ready"])
        self.assertEqual(
            unavailable["reason_code"],
            "reading_visual_credentials_unavailable",
        )

        self.assertEqual(
            payload,
            {
                "schema_version": READING_VISUAL_SETUP_SCHEMA_VERSION,
                "provider": READING_VISUAL_PROVIDER,
                "model": READING_VISUAL_MODEL,
                "timeout_seconds": 300,
                "max_retries": 0,
                "billing_basis": list(READING_VISUAL_BILLING_BASIS),
                "configured": True,
                "ready": True,
                "reason_code": None,
                "message": None,
                "connection_revision": 7,
            },
        )

    def test_environment_overrides_persisted_visual_values(self) -> None:
        merged = load_reading_visual_environment(
            environment={"REQPILOT_READING_VISUAL_MODEL": "env-model"},
            config_values={
                "REQPILOT_READING_VISUAL_MODEL": "file-model",
                "REQPILOT_READING_VISUAL_PROVIDER": "file-provider",
            },
        )

        self.assertEqual(merged["REQPILOT_READING_VISUAL_MODEL"], "env-model")
        self.assertEqual(
            merged["REQPILOT_READING_VISUAL_PROVIDER"],
            "file-provider",
        )

    def test_tampering_any_fixed_field_is_validation_error_and_api_422(self) -> None:
        valid = {
            "schema_version": READING_VISUAL_SETUP_SCHEMA_VERSION,
            "provider": READING_VISUAL_PROVIDER,
            "model": READING_VISUAL_MODEL,
            "timeout_seconds": 300,
            "max_retries": 0,
        }
        mutations = {
            "provider": "bailian-qwen-ocr",
            "model": "qwen3.7-plus",
            "timeout_seconds": 299,
            "max_retries": 1,
        }
        app = FastAPI()
        app.include_router(runtime_router.build())
        client = TestClient(app)
        for field, invalid_value in mutations.items():
            with self.subTest(field=field):
                reading_visual = {**valid, field: invalid_value}
                with self.assertRaises(ValidationError):
                    RuntimeAIProviderSetupRequest.model_validate(
                        {"aliyun": {"reading_visual": reading_visual}}
                    )
                response = client.post(
                    "/api/runtime/ai-provider-setup",
                    json={"aliyun": {"reading_visual": reading_visual}},
                )
                self.assertEqual(response.status_code, 422)

    def test_apply_persists_only_the_fixed_reading_visual_keys(self) -> None:
        request = {
            "schema_version": READING_VISUAL_SETUP_SCHEMA_VERSION,
            "provider": READING_VISUAL_PROVIDER,
            "model": READING_VISUAL_MODEL,
            "timeout_seconds": 300,
            "max_retries": 0,
        }
        production_config = Path(__file__).resolve().parents[1] / "storage/config.json"
        production_before = (
            production_config.read_bytes(),
            production_config.stat().st_mtime_ns,
        ) if production_config.exists() else None
        with tempfile.TemporaryDirectory() as directory:
            storage_dir = Path(directory)
            with patch.object(ai_copilot, "STORAGE_DIR", storage_dir), patch.dict(
                ai_copilot._RUNTIME_AI_ENV_OVERRIDES,
                {},
                clear=True,
            ):
                ai_copilot.apply_runtime_ai_provider_setup(
                    {"aliyun": {"reading_visual": request}}
                )
                persisted = json.loads(
                    (storage_dir / "config.json").read_text(encoding="utf-8")
                )

        for key, value in reading_visual_persisted_values().items():
            self.assertEqual(persisted[key], value)
        if production_before is None:
            self.assertFalse(production_config.exists())
        else:
            self.assertEqual(production_config.read_bytes(), production_before[0])
            self.assertEqual(production_config.stat().st_mtime_ns, production_before[1])

    def test_production_runtime_rejects_placeholder_dashscope_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ApplicationError) as error:
                runtime_application._validate_ascii_runtime_secret(
                    "DashScope API Key",
                    "aliyun-key",
                )
        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("placeholder", str(error.exception.detail))


if __name__ == "__main__":
    unittest.main()
