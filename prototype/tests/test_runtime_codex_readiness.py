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
    _cached_codex_protocol_reason,
    build_runtime_router,
)
from app.application.agent_workflow.workflow_readiness import (
    AGENT_WORKFLOW_REQUIRED_JOB_KINDS,
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
):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        settings=SimpleNamespace(codex_bin=Path("/tmp/codex")),
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


if __name__ == "__main__":
    unittest.main()
