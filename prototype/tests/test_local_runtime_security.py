from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import os
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

if __package__:
    from .application_under_test import main
else:
    from application_under_test import main
from app.main import app as http_app

from app.infrastructure.http_security import (
    is_allowed_local_host,
    is_allowed_local_origin,
)
from app.infrastructure.local_runtime import (
    LOOPBACK_HOST,
    QDRANT_SERVICE_HOST_ENV,
    loopback_url,
    probe_local_readiness,
    probe_local_runtime_readiness,
    prepare_artifact_smoke_storage,
    qdrant_loopback_environment,
    require_loopback_host,
    shutdown_runtime_in_order,
    uvicorn_loopback_kwargs,
    wait_for_local_readiness,
)


main.app = http_app


class _FakeResponse:
    def __init__(self, *, status: int, payload: dict[str, object]) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class LocalRuntimeSecurityTests(unittest.TestCase):
    def test_local_bind_configuration_cannot_fall_back_to_all_interfaces(self) -> None:
        self.assertEqual(require_loopback_host(), LOOPBACK_HOST)
        self.assertEqual(require_loopback_host("localhost"), LOOPBACK_HOST)
        self.assertEqual(
            uvicorn_loopback_kwargs(8543),
            {"host": LOOPBACK_HOST, "port": 8543, "log_level": "warning"},
        )
        for unsafe_host in ("0.0.0.0", "::", "192.168.1.20", ""):
            if unsafe_host == "":
                continue
            with self.subTest(host=unsafe_host):
                with self.assertRaises(ValueError):
                    require_loopback_host(unsafe_host)

    def test_qdrant_subprocess_environment_overrides_unsafe_inherited_host(self) -> None:
        environment = qdrant_loopback_environment(
            {
                QDRANT_SERVICE_HOST_ENV: "0.0.0.0",
                "REQPILOT_ALIYUN_API_KEY": "must-not-reach-qdrant",
                "AZURE_DOCUMENT_INTELLIGENCE_API_KEY": "must-not-reach-qdrant",
                "AZURE_OPENAI_API_KEY": "must-not-reach-qdrant",
                "UNRELATED": "kept",
            }
        )

        self.assertEqual(environment[QDRANT_SERVICE_HOST_ENV], LOOPBACK_HOST)
        self.assertEqual(environment["UNRELATED"], "kept")
        self.assertNotIn("REQPILOT_ALIYUN_API_KEY", environment)
        self.assertNotIn("AZURE_DOCUMENT_INTELLIGENCE_API_KEY", environment)
        self.assertNotIn("AZURE_OPENAI_API_KEY", environment)

    def test_backend_probe_requires_truthful_http_readiness_payload(self) -> None:
        requests: list[tuple[str, float]] = []

        def ready_opener(request, timeout: float):
            requests.append((request.full_url, timeout))
            return _FakeResponse(status=200, payload={"status": "ok", "ready": True})

        self.assertTrue(probe_local_readiness(8543, opener=ready_opener))
        self.assertEqual(requests[0][0], loopback_url(8543, "/api/readiness"))

        def false_ready_opener(_request, timeout: float):
            return _FakeResponse(status=200, payload={"status": "ok", "ready": False})

        self.assertFalse(probe_local_readiness(8543, opener=false_ready_opener))

    def test_runtime_probe_allows_recovery_ui_when_only_ai_is_not_ready(self) -> None:
        def runtime_ready_opener(_request, timeout: float):
            return _FakeResponse(
                status=200,
                payload={
                    "status": "not_ready",
                    "ready": False,
                    "runtime_ready": True,
                },
            )

        self.assertTrue(
            probe_local_runtime_readiness(8543, opener=runtime_ready_opener)
        )

    def test_backend_wait_polls_readiness_instead_of_accepting_an_open_port(self) -> None:
        results = iter((False, False, True))
        attempts: list[int] = []

        def probe(port: int) -> bool:
            attempts.append(port)
            return next(results)

        self.assertTrue(
            wait_for_local_readiness(
                8543,
                timeout_seconds=1.0,
                poll_interval_seconds=0.01,
                probe=probe,
            )
        )
        self.assertEqual(attempts, [8543, 8543, 8543])

    def test_artifact_smoke_storage_requires_frozen_empty_absolute_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "smoke storage"
            prepared = prepare_artifact_smoke_storage(candidate, frozen=True)
            self.assertEqual(prepared, candidate.resolve())
            self.assertTrue(prepared.is_dir())

            (prepared / "existing.txt").write_text("data", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not contain"):
                prepare_artifact_smoke_storage(prepared, frozen=True)
            with self.assertRaisesRegex(ValueError, "only in a frozen"):
                prepare_artifact_smoke_storage(root / "other", frozen=False)
            with self.assertRaisesRegex(ValueError, "absolute"):
                prepare_artifact_smoke_storage(Path("relative"), frozen=True)

    def test_shutdown_order_never_releases_storage_after_backend_timeout(self) -> None:
        calls: list[str] = []

        def fail_backend() -> None:
            calls.append("backend")
            raise RuntimeError("timeout")

        with self.assertRaisesRegex(RuntimeError, "timeout"):
            shutdown_runtime_in_order(
                stop_qdrant=lambda: calls.append("qdrant"),
                stop_backend=fail_backend,
                release_storage=lambda: calls.append("release"),
            )
        self.assertEqual(calls, ["qdrant", "backend"])

        calls.clear()
        shutdown_runtime_in_order(
            stop_qdrant=lambda: calls.append("qdrant"),
            stop_backend=lambda: calls.append("backend"),
            release_storage=lambda: calls.append("release"),
        )
        self.assertEqual(calls, ["qdrant", "backend", "release"])

    def test_http_boundary_rejects_untrusted_host_before_routing(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/api/health", headers={"host": "evil.example"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid request host.")

    def test_http_boundary_rejects_non_loopback_origin_before_routing(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/api/projects",
                headers={"origin": "https://evil.example"},
                json={"name": "must-not-be-created"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Cross-origin request blocked.")

    def test_cors_preflight_allows_only_explicit_loopback_origin(self) -> None:
        local_origin = "http://127.0.0.1:8543"
        with TestClient(main.app) as client:
            allowed = client.options(
                "/api/projects",
                headers={
                    "origin": local_origin,
                    "access-control-request-method": "POST",
                },
            )
            blocked = client.options(
                "/api/projects",
                headers={
                    "origin": "https://evil.example",
                    "access-control-request-method": "POST",
                },
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["access-control-allow-origin"], local_origin)
        self.assertEqual(blocked.status_code, 403)
        self.assertNotIn("access-control-allow-origin", blocked.headers)

    def test_local_host_and_origin_parsers_reject_userinfo_and_remote_hosts(self) -> None:
        self.assertTrue(is_allowed_local_host("127.0.0.1:8543"))
        self.assertTrue(is_allowed_local_host("testserver"))
        self.assertFalse(is_allowed_local_host("testserver", test_mode=False))
        self.assertFalse(is_allowed_local_host("evil.example"))
        self.assertFalse(is_allowed_local_host("evil@127.0.0.1:8543"))
        self.assertFalse(is_allowed_local_origin("https://evil.example"))
        self.assertFalse(is_allowed_local_origin("http://user@127.0.0.1:8543"))
        self.assertFalse(is_allowed_local_origin("http://127.0.0.1:99999"))

    def test_testserver_host_is_rejected_without_explicit_test_mode(self) -> None:
        environment = os.environ.copy()
        environment.pop("REQPILOT_TEST_MODE", None)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from app.infrastructure.http_security import "
                    "is_allowed_local_host; "
                    "raise SystemExit(1 if is_allowed_local_host('testserver') else 0)"
                ),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LaunchSurfaceSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.prototype_dir = cls.repo_root / "prototype"

    def test_shell_launchers_reject_non_loopback_host_before_startup(self) -> None:
        start_result = subprocess.run(
            ["bash", str(self.prototype_dir / "start.sh"), "--host", "0.0.0.0"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status_environment = os.environ.copy()
        status_environment["HOST"] = "0.0.0.0"
        status_result = subprocess.run(
            ["bash", str(self.prototype_dir / "status.sh")],
            cwd=self.repo_root,
            env=status_environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(start_result.returncode, 2)
        self.assertIn("loopback", start_result.stdout)
        self.assertEqual(status_result.returncode, 2)
        self.assertIn("loopback", status_result.stdout)

    def test_windows_qdrant_and_api_bindings_are_loopback_only(self) -> None:
        source = (self.prototype_dir / "start.ps1").read_text(encoding="utf-8")

        self.assertIn('if ($BindHost -ne "127.0.0.1")', source)
        self.assertIn('-p "127.0.0.1:${HttpPort}:6333"', source)
        self.assertIn('-p "127.0.0.1:${GrpcPort}:6334"', source)
        self.assertNotIn('-p "${HttpPort}:6333"', source)
        self.assertNotIn('-p "${GrpcPort}:6334"', source)
        self.assertIn("function Assert-LoopbackOnlyListener", source)
        self.assertIn("function Test-QdrantContainerLoopbackBindings", source)
        self.assertIn("ConvertFrom-Json", source)
        ensure_body = source[source.index("function Ensure-QdrantContainer") :]
        self.assertLess(
            ensure_body.index("Assert-LoopbackOnlyListener -LocalPort $HttpPort"),
            ensure_body.index("if (Test-QdrantReady -LocalPort $HttpPort)"),
        )
        self.assertLess(
            ensure_body.index("Test-QdrantContainerLoopbackBindings"),
            ensure_body.index("& $docker.Source start $ContainerName"),
        )

    def test_desktop_uses_loopback_uvicorn_qdrant_and_http_readiness(self) -> None:
        source = (self.repo_root / "apps/desktop/app_desktop.py").read_text(encoding="utf-8")

        self.assertIn("uvicorn.Config(app, **uvicorn_loopback_kwargs(port))", source)
        self.assertIn("backend_server = uvicorn.Server(config)", source)
        self.assertIn("backend_server.run(sockets=[listener])", source)
        self.assertIn("backend_server.should_exit = True", source)
        self.assertIn("listener.bind((LOOPBACK_HOST, 0))", source)
        self.assertNotIn("def find_free_port", source)
        self.assertIn("env=qdrant_loopback_environment()", source)
        self.assertIn("probe=probe_local_runtime_readiness", source)
        self.assertNotIn('host="0.0.0.0"', source)

    def test_embedded_secret_obfuscation_workflow_is_disabled(self) -> None:
        secret = "secret-that-must-never-be-echoed"
        desktop_source = (self.repo_root / "apps/desktop/app_desktop.py").read_text(encoding="utf-8")
        script_path = self.prototype_dir / "scripts" / "obfuscate_key.py"
        script_source = script_path.read_text(encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script_path), secret],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("OB_DASHSCOPE_API_KEY", desktop_source)
        self.assertNotIn("deobfuscate_secret", desktop_source)
        self.assertNotIn("base64", script_source)


if __name__ == "__main__":
    unittest.main()
