from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.api.runtime_routes import (
    _build_agent_workflow_readiness_response,
    _build_selected_runtime_readiness_response,
    _cached_codex_protocol_reason,
    build_runtime_router,
)
from app.infrastructure.runtime_settings import AgentProvider, AgentRuntimeBackend
from app.application.agent_workflow.workflow_readiness import (
    AGENT_WORKFLOW_REQUIRED_JOB_KINDS,
    PROVIDER_WORKFLOW_REQUIRED_JOB_KINDS,
    build_agent_workflow_readiness,
)


class _Database:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def healthcheck(self) -> None:
        if self.error is not None:
            raise self.error


class _ObjectStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def stat(self, _key: str):  # type: ignore[no-untyped-def]
        if self.error is not None:
            raise self.error
        return None


class _Jobs:
    def __init__(self, unavailable: tuple[str, ...] = ()) -> None:
        self.unavailable = set(unavailable)
        self.calls: list[dict[str, object]] = []

    def worker_fleet_status(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(dict(kwargs))
        eligible = 0 if kwargs["required_kind"] in self.unavailable else 1
        return {
            "state": "READY" if eligible else "UNAVAILABLE",
            "live_worker_count": 1,
            "eligible_worker_count": eligible,
        }


def _container(
    *,
    database_error: Exception | None = None,
    object_store_error: Exception | None = None,
    unavailable_workers: tuple[str, ...] = (),
    backend=AgentRuntimeBackend.CODEX_APP_SERVER,
    provider=None,
    model_id="gpt-5.6-sol",
    customer_data_enabled=True,
):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        settings=SimpleNamespace(
            agent_runtime_backend=backend,
            agent_provider=provider,
            agent_model_id=model_id,
            agent_customer_data_enabled=customer_data_enabled,
            codex_bin=Path("/tmp/codex") if backend is AgentRuntimeBackend.CODEX_APP_SERVER else None,
        ),
        database=_Database(database_error),
        object_store=_ObjectStore(object_store_error),
        jobs=_Jobs(unavailable_workers),
    )


def _workflow_report(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "container": _container(),
        "codex_auth_ready": True,
        "codex_auth_reason": None,
        "protocol_reason": None,
        "source_reader_ready": True,
        "source_revision": "source-revision-1",
        "runtime_instance_id": "runtime-1",
    }
    values.update(overrides)
    return build_agent_workflow_readiness(**values)


class CodexRuntimeReadinessTests(unittest.TestCase):
    def tearDown(self) -> None:
        _cached_codex_protocol_reason.cache_clear()

    def test_protocol_drift_is_reported_before_a_document_job_starts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw) / "codex"
            binary.write_bytes(b"fixture")
            stat = binary.stat()
            with patch(
                "app.api.runtime_routes.verify_protocol_binary_identity",
                side_effect=LifecycleProbeError("CODEX_BINARY_HASH_MISMATCH"),
            ):
                reason = _cached_codex_protocol_reason(
                    str(binary),
                    stat.st_size,
                    stat.st_mtime_ns,
                )

        self.assertEqual(reason, "CODEX_BINARY_HASH_MISMATCH")

    def test_verified_protocol_lock_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw) / "codex"
            binary.write_bytes(b"fixture")
            stat = binary.stat()
            with patch("app.api.runtime_routes.verify_protocol_binary_identity"):
                reason = _cached_codex_protocol_reason(
                    str(binary),
                    stat.st_size,
                    stat.st_mtime_ns,
                )

        self.assertIsNone(reason)

    def test_agent_workflow_ready_uses_only_current_core_dependencies(self) -> None:
        container = _container()
        report = _workflow_report(container=container)

        self.assertTrue(report["ready"])
        self.assertEqual(report["status"], "ready")
        self.assertIsNone(report["reason_code"])
        self.assertEqual(report["blocking_reason_codes"], [])
        self.assertTrue(all(check["ready"] for check in report["checks"].values()))
        self.assertNotIn("azure", str(report).lower())
        self.assertNotIn("aliyun", str(report).lower())
        self.assertNotIn("semantic", str(report).lower())
        self.assertEqual(
            [call["required_kind"] for call in container.jobs.calls],
            list(AGENT_WORKFLOW_REQUIRED_JOB_KINDS),
        )
        self.assertTrue(
            all(
                call["require_connection_revision"] is False
                for call in container.jobs.calls
            )
        )

    def test_azure_and_aliyun_configuration_do_not_change_agent_readiness(
        self,
    ) -> None:
        container = _container()
        baseline = _workflow_report(container=container)
        with patch.dict(
            "os.environ",
            {
                "AZURE_OPENAI_ENDPOINT": "https://changed.invalid",
                "REQPILOT_ALIYUN_API_KEY": "changed",
                "REQPILOT_AI_DEFAULT_PROFILE": "aliyun",
            },
            clear=False,
        ):
            changed = _workflow_report(container=container)

        self.assertEqual(changed["ready"], baseline["ready"])
        self.assertEqual(changed["reason_code"], baseline["reason_code"])
        self.assertEqual(changed["checks"], baseline["checks"])

    def test_codex_logged_out_has_specific_reason(self) -> None:
        report = _workflow_report(
            codex_auth_ready=False,
            codex_auth_reason="reading_codex_auth_required",
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason_code"], "CODEX_AUTH_REQUIRED")
        self.assertEqual(
            report["checks"]["codex_auth"]["runtime_reason_code"],
            "reading_codex_auth_required",
        )

    def test_protocol_drift_has_specific_reason(self) -> None:
        report = _workflow_report(protocol_reason="CODEX_BINARY_HASH_MISMATCH")

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason_code"], "CODEX_BINARY_HASH_MISMATCH")
        self.assertFalse(report["checks"]["protocol_lock"]["ready"])

    def test_postgresql_failure_has_specific_reason(self) -> None:
        report = _workflow_report(
            container=_container(database_error=RuntimeError("secret")),
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason_code"], "POSTGRESQL_UNAVAILABLE")
        self.assertNotIn("secret", str(report))

    def test_object_store_failure_has_specific_reason(self) -> None:
        report = _workflow_report(
            container=_container(object_store_error=RuntimeError("secret")),
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason_code"], "OBJECT_STORE_UNAVAILABLE")
        self.assertNotIn("secret", str(report))

    def test_durable_worker_failure_has_specific_reason(self) -> None:
        missing = AGENT_WORKFLOW_REQUIRED_JOB_KINDS[-1]
        report = _workflow_report(
            container=_container(unavailable_workers=(missing,)),
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason_code"], "DURABLE_WORKER_UNAVAILABLE")
        self.assertEqual(
            report["checks"]["durable_worker"]["unavailable_job_kinds"],
            [missing],
        )

    def test_source_reader_failure_has_specific_reason(self) -> None:
        report = _workflow_report(source_reader_ready=False)

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason_code"], "SOURCE_READER_UNAVAILABLE")

    def test_agent_workflow_endpoint_is_an_independent_read_only_projection(
        self,
    ) -> None:
        app = FastAPI()
        app.include_router(build_runtime_router(lambda **_kwargs: {}))
        expected = _workflow_report()
        with patch(
            "app.api.runtime_routes._build_agent_workflow_readiness_response",
            return_value=expected,
        ):
            with TestClient(app) as client:
                response = client.get("/api/runtime/agent-workflow-readiness")
                rejected_write = client.post(
                    "/api/runtime/agent-workflow-readiness"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.assertEqual(rejected_write.status_code, 405)

    def test_runtime_projection_reports_unconfigured_binary_separately(self) -> None:
        from app.application import runtime

        container = _container()
        container.settings.codex_bin = None
        old_container = runtime.RUNTIME_STATE.container
        old_files = runtime.RUNTIME_STATE.files
        runtime.RUNTIME_STATE.container = container
        runtime.RUNTIME_STATE.files = object()
        try:
            with (
                patch(
                    "app.api.runtime_routes.get_reading_provider_readiness",
                    return_value=SimpleNamespace(ready=True, reason_code=None),
                ),
                patch(
                    "app.api.runtime_routes._configured_codex_protocol_reason",
                    return_value=None,
                ),
            ):
                report = _build_agent_workflow_readiness_response()
        finally:
            runtime.RUNTIME_STATE.container = old_container
            runtime.RUNTIME_STATE.files = old_files

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason_code"], "CODEX_BINARY_NOT_CONFIGURED")


class SelectedRuntimeReadinessTests(unittest.TestCase):
    def _provider_container(self, provider=AgentProvider.ALIYUN, **kwargs):
        return _container(
            backend=AgentRuntimeBackend.PROVIDER_TOOL_LOOP,
            provider=provider,
            model_id="",
            **kwargs,
        )

    def _profile(self, provider):
        if provider is AgentProvider.AZURE:
            return SimpleNamespace(
                azure_openai_chat_ready=True,
                azure_openai_chat_deployment="configured-azure-chat",
            )
        return SimpleNamespace(
            dashscope_chat_ready=True,
            chat_model="configured-aliyun-chat",
            screening_model="configured-aliyun-screening",
        )

    def test_each_provider_reports_configured_identity_without_live_probe(self):
        from app.application import runtime

        for provider in (AgentProvider.AZURE, AgentProvider.ALIYUN):
            with self.subTest(provider=provider):
                container = self._provider_container(provider)
                with (
                    patch.object(runtime.RUNTIME_STATE, "container", container),
                    patch.object(runtime.RUNTIME_STATE, "files", object()),
                    patch("app.services.ai_copilot.load_ai_copilot_config", return_value=self._profile(provider)),
                    patch("app.services.ai_copilot.get_runtime_ai_config_value", return_value=""),
                    patch("app.services.ai_copilot._azure_openai_client") as azure_client,
                    patch("app.services.ai_copilot._aliyun_openai_client") as aliyun_client,
                    patch("app.api.runtime_routes.get_reading_provider_readiness") as codex_probe,
                    patch("app.api.runtime_routes._configured_codex_protocol_reason") as codex_protocol,
                    patch.dict("os.environ", {"REQPILOT_AGENT_RUNTIME": "codex-app-server"}),
                ):
                    selected = _build_selected_runtime_readiness_response()
                    workflow = _build_agent_workflow_readiness_response()

                self.assertTrue(selected["ready"])
                self.assertTrue(selected["configured"])
                self.assertEqual(selected["runtime_id"], "provider-tool-loop")
                self.assertEqual(selected["provider_id"], provider.value)
                self.assertEqual(selected["model_name"], f"configured-{provider.value}-chat")
                self.assertEqual(selected["readiness_scope"], "configuration")
                self.assertFalse(selected["authentication_verified"])
                self.assertTrue(workflow["ready"])
                self.assertEqual(workflow["provider_id"], provider.value)
                self.assertEqual(workflow["model_name"], selected["model_name"])
                self.assertEqual(workflow["readiness_scope"], "configuration_and_core")
                self.assertFalse(workflow["authentication_verified"])
                self.assertNotIn("codex_auth", workflow["checks"])
                self.assertNotIn("protocol_lock", workflow["checks"])
                self.assertFalse(workflow["checks"]["provider_configuration"]["authentication_verified"])
                azure_client.assert_not_called()
                aliyun_client.assert_not_called()
                codex_probe.assert_not_called()
                codex_protocol.assert_not_called()

    def test_missing_provider_configuration_is_redacted_and_not_ready(self):
        from app.application import runtime

        container = self._provider_container()
        with (
            patch.object(runtime.RUNTIME_STATE, "container", container),
            patch.object(runtime.RUNTIME_STATE, "files", object()),
            patch("app.services.ai_copilot.load_ai_copilot_config", side_effect=ValueError("secret-config-value")),
            patch("app.api.runtime_routes.get_reading_provider_readiness") as codex_probe,
            patch("app.api.runtime_routes._configured_codex_protocol_reason") as codex_protocol,
        ):
            selected = _build_selected_runtime_readiness_response()
            workflow = _build_agent_workflow_readiness_response()

        self.assertFalse(selected["ready"])
        self.assertFalse(selected["configured"])
        self.assertEqual(selected["reason_code"], "PROVIDER_CONFIGURATION_UNAVAILABLE")
        self.assertEqual(selected["provider_id"], "aliyun")
        self.assertIsNone(selected["model_name"])
        self.assertFalse(workflow["ready"])
        self.assertEqual(workflow["reason_code"], "PROVIDER_CONFIGURATION_UNAVAILABLE")
        self.assertNotIn("secret-config-value", str(selected) + str(workflow))
        codex_probe.assert_not_called()
        codex_protocol.assert_not_called()

    def test_missing_provider_or_unconfigured_model_fails_closed(self):
        from app.application import runtime

        for provider, model in ((None, ""), (AgentProvider.ALIYUN, "unconfigured-model")):
            with self.subTest(provider=provider, model=model):
                container = self._provider_container(provider)
                container.settings.agent_model_id = model
                with (
                    patch.object(runtime.RUNTIME_STATE, "container", container),
                    patch("app.services.ai_copilot.load_ai_copilot_config", return_value=self._profile(AgentProvider.ALIYUN)),
                ):
                    report = _build_selected_runtime_readiness_response()
                self.assertFalse(report["ready"])
                self.assertEqual(report["reason_code"], "PROVIDER_CONFIGURATION_UNAVAILABLE")
                self.assertFalse(report["authentication_verified"])

    def test_provider_customer_data_gate_is_not_live_authentication(self):
        from app.application import runtime

        with (
            patch.object(runtime.RUNTIME_STATE, "container", self._provider_container(customer_data_enabled=False)),
            patch.object(runtime.RUNTIME_STATE, "files", object()),
            patch("app.services.ai_copilot.load_ai_copilot_config", return_value=self._profile(AgentProvider.ALIYUN)),
        ):
            selected = _build_selected_runtime_readiness_response()
            workflow = _build_agent_workflow_readiness_response()
        self.assertTrue(selected["configured"])
        self.assertFalse(workflow["ready"])
        self.assertEqual(workflow["reason_code"], "AGENT_CUSTOMER_DATA_DISABLED")
        self.assertFalse(workflow["authentication_verified"])

    def test_unselected_and_unsupported_runtime_endpoints_never_probe_codex(self):
        from app.application import runtime

        app = FastAPI()
        app.include_router(build_runtime_router(lambda **_kwargs: {}))
        for backend, reason in (
            (None, "AGENT_RUNTIME_NOT_CONFIGURED"),
            (AgentRuntimeBackend.NONE, "AGENT_RUNTIME_NOT_CONFIGURED"),
            (SimpleNamespace(value="future-runtime"), "AGENT_RUNTIME_UNSUPPORTED"),
            ("provider-tool-loop", "AGENT_RUNTIME_UNSUPPORTED"),
        ):
            with (
                self.subTest(backend=backend),
                patch.object(runtime.RUNTIME_STATE, "container", _container(backend=backend)),
                patch.object(runtime.RUNTIME_STATE, "files", object()),
                patch("app.api.runtime_routes.get_reading_provider_readiness") as codex_probe,
                patch("app.api.runtime_routes._configured_codex_protocol_reason") as codex_protocol,
                patch("reqpilotx_server.adapters.agents.provider_tool_loop._configuration") as provider_config,
                TestClient(app) as client,
            ):
                selected = client.get("/api/runtime/agent-readiness")
                workflow = client.get("/api/runtime/agent-workflow-readiness")
                for response in (selected, workflow):
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.json()["ready"])
                    self.assertEqual(response.json()["reason_code"], reason)
                codex_probe.assert_not_called()
                codex_protocol.assert_not_called()
                provider_config.assert_not_called()

    def test_provider_endpoint_is_read_only_and_does_not_probe_codex(self):
        from app.application import runtime

        app = FastAPI()
        app.include_router(build_runtime_router(lambda **_kwargs: {}))
        with (
            patch.object(runtime.RUNTIME_STATE, "container", self._provider_container()),
            patch("app.services.ai_copilot.load_ai_copilot_config", return_value=self._profile(AgentProvider.ALIYUN)),
            patch("app.api.runtime_routes.get_reading_provider_readiness") as codex_probe,
            patch("app.api.runtime_routes._configured_codex_protocol_reason") as codex_protocol,
            TestClient(app) as client,
        ):
            response = client.get("/api/runtime/agent-readiness")
            rejected_write = client.post("/api/runtime/agent-readiness")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(rejected_write.status_code, 405)
        self.assertEqual(response.json()["runtime_id"], "provider-tool-loop")
        self.assertFalse(response.json()["authentication_verified"])
        codex_probe.assert_not_called()
        codex_protocol.assert_not_called()

    def test_codex_specific_endpoint_keeps_its_existing_contract(self):
        from app.application import runtime

        app = FastAPI()
        app.include_router(build_runtime_router(lambda **_kwargs: {}))
        expected = {
            "schema_version": "codex-runtime-readiness/v1",
            "ready": False,
            "runtime_id": "codex-app-server",
            "model_name": "gpt-5.6-sol",
            "configuration_source": "CODEX_RUNTIME",
            "reason_code": "CODEX_BINARY_HASH_MISMATCH",
        }
        with (
            patch.object(runtime.RUNTIME_STATE, "container", _container()),
            patch("app.api.runtime_routes.get_reading_provider_readiness", return_value=SimpleNamespace(
                ready=True, provider_id="codex-app-server", model_name="gpt-5.6-sol",
                configuration_source="CODEX_RUNTIME", reason_code=None,
            )),
            patch("app.api.runtime_routes._configured_codex_protocol_reason", return_value="CODEX_BINARY_HASH_MISMATCH"),
            TestClient(app) as client,
        ):
            response = client.get("/api/runtime/codex-readiness")
            selected = client.get("/api/runtime/agent-readiness")
        self.assertEqual(response.json(), expected)
        self.assertEqual(selected.json(), expected)

    def test_codex_probe_exception_is_not_ready_and_probed_once_per_request(self):
        from app.application import runtime

        app = FastAPI()
        app.include_router(build_runtime_router(lambda **_kwargs: {}))
        with (
            patch.object(runtime.RUNTIME_STATE, "container", _container()),
            patch.object(runtime.RUNTIME_STATE, "files", object()),
            patch("app.api.runtime_routes.get_reading_provider_readiness", side_effect=RuntimeError("secret-probe-error")) as codex_probe,
            patch("app.api.runtime_routes._configured_codex_protocol_reason", return_value=None),
            TestClient(app) as client,
        ):
            workflow = client.get("/api/runtime/agent-workflow-readiness")
            self.assertEqual(codex_probe.call_count, 1)
            codex_probe.reset_mock()
            selected = client.get("/api/runtime/agent-readiness")
            self.assertEqual(codex_probe.call_count, 1)
        self.assertEqual(workflow.status_code, 200)
        self.assertEqual(workflow.json()["reason_code"], "CODEX_RUNTIME_UNAVAILABLE")
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["reason_code"], "reading_codex_unavailable")
        self.assertFalse(selected.json()["ready"])
        self.assertFalse(workflow.json()["ready"])
        self.assertNotIn("secret-probe-error", str(selected.json()) + str(workflow.json()))

    def test_provider_requires_workers_for_actual_autonomous_loop_kinds(self):
        from app.application import runtime

        for kind in ("agent.source-item.extract-loop", "agent.source-item.loop"):
            with self.subTest(kind=kind):
                container = self._provider_container(unavailable_workers=(kind,))
                with (
                    patch.object(runtime.RUNTIME_STATE, "container", container),
                    patch.object(runtime.RUNTIME_STATE, "files", object()),
                    patch("app.services.ai_copilot.load_ai_copilot_config", return_value=self._profile(AgentProvider.ALIYUN)),
                ):
                    report = _build_agent_workflow_readiness_response()
                self.assertFalse(report["ready"])
                self.assertEqual(report["reason_code"], "DURABLE_WORKER_UNAVAILABLE")
                self.assertEqual(report["checks"]["durable_worker"]["unavailable_job_kinds"], [kind])
                self.assertEqual(
                    report["checks"]["durable_worker"]["required_job_kinds"],
                    list(PROVIDER_WORKFLOW_REQUIRED_JOB_KINDS),
                )

    def test_codex_retains_its_established_worker_kind_contract(self):
        container = _container(unavailable_workers=("agent.source-item.extract-loop", "agent.source-item.loop"))
        report = _workflow_report(container=container)
        self.assertTrue(report["ready"])
        self.assertEqual(
            report["checks"]["durable_worker"]["required_job_kinds"],
            list(AGENT_WORKFLOW_REQUIRED_JOB_KINDS),
        )


if __name__ == "__main__":
    unittest.main()
