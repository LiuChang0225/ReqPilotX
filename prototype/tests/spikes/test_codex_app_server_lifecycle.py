from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from spikes.codex_app_server.lifecycle import (
    EventLedger,
    JsonlRpcSession,
    LifecycleProbeError,
    SYNTHETIC_MARKER,
    _REQUIRED_DISABLED_FEATURES,
    _codex_error_reason,
    _validate_thread_start_result,
    _turn_status_from_thread_result,
    _validate_turn_start_result,
    _validate_synthetic_response,
    build_lifecycle_command,
    build_lifecycle_environment,
    load_protocol_lock,
    validate_effective_config,
    validate_private_codex_home,
)


class CodexAppServerLifecycleTests(unittest.TestCase):
    def test_codex_error_info_maps_without_retaining_provider_message(self) -> None:
        self.assertEqual(
            _codex_error_reason(
                {"error": {"codexErrorInfo": {"type": "UsageLimitExceeded"}}}
            ),
            "USAGE_LIMITED",
        )
        self.assertEqual(
            _codex_error_reason(
                {
                    "error": {
                        "codexErrorInfo": {
                            "type": "HttpConnectionFailed",
                            "httpStatusCode": 401,
                        }
                    }
                }
            ),
            "REAUTH_REQUIRED",
        )
        self.assertEqual(
            _codex_error_reason(
                {
                    "error": {
                        "message": "provider body must not be projected",
                        "codexErrorInfo": {
                            "HttpConnectionFailed": {"httpStatusCode": 503}
                        },
                    }
                }
            ),
            "APP_SERVER_TEMPORARY_UNAVAILABLE",
        )

    def test_protocol_lock_is_complete_and_content_free(self) -> None:
        lock = load_protocol_lock()

        self.assertEqual(lock["schema"], "reqpilotx-codex-protocol-lock/v1")
        self.assertRegex(lock["binary_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(lock["protocol_schema_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(lock["protocol_v2_schema_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            lock["experimental_protocol_schema_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertRegex(
            lock["experimental_protocol_v2_schema_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_private_codex_home_rejects_symlink_and_loose_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-home-check-") as root:
            root_path = Path(root)
            private_home = root_path / "private"
            private_home.mkdir(mode=0o700)
            os.chmod(private_home, 0o700)
            private_home = private_home.resolve()
            repository = root_path / "repository"
            repository.mkdir()
            repository = repository.resolve()
            self.assertEqual(
                validate_private_codex_home(
                    private_home,
                    repository_root=repository,
                ),
                private_home.resolve(),
            )
            os.chmod(private_home, 0o755)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CODEX_HOME_PERMISSIONS_INVALID",
            ):
                validate_private_codex_home(
                    private_home,
                    repository_root=repository,
                )
            os.chmod(private_home, 0o700)
            link = root_path / "home-link"
            link.symlink_to(private_home, target_is_directory=True)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CODEX_HOME_PATH_INVALID",
            ):
                validate_private_codex_home(link, repository_root=repository)
            repository_inside_home = private_home / "repository"
            repository_inside_home.mkdir()
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CODEX_HOME_LOCATION_FORBIDDEN",
            ):
                validate_private_codex_home(
                    private_home,
                    repository_root=repository_inside_home,
                )

    def test_lifecycle_environment_drops_parent_secrets_and_proxies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-env-check-") as root:
            root_path = Path(root)
            codex_home = root_path / "codex"
            credential_home = root_path / "home"
            tmp_dir = root_path / "tmp"
            for directory in (codex_home, credential_home, tmp_dir):
                directory.mkdir()
            environment = build_lifecycle_environment(
                codex_home=codex_home,
                credential_home=credential_home,
                tmp_dir=tmp_dir,
            )

        self.assertEqual(
            set(environment),
            {"CODEX_HOME", "HOME", "LANG", "LC_ALL", "NO_COLOR", "PATH", "TMPDIR"},
        )
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("DASHSCOPE_API_KEY", environment)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")

    def test_lifecycle_command_pins_state_and_disables_agent_surfaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-command-check-") as root:
            root_path = Path(root)
            sqlite_home = root_path / "sqlite"
            log_dir = root_path / "logs"
            sqlite_home.mkdir()
            log_dir.mkdir()
            command = build_lifecycle_command(
                Path("/opt/codex"),
                sqlite_home=sqlite_home,
                log_dir=log_dir,
            )

        joined = " ".join(command)
        self.assertIn("features.shell_tool=false", joined)
        self.assertIn("features.multi_agent=false", joined)
        self.assertIn("features.hooks=false", joined)
        self.assertIn("features.image_generation=false", joined)
        self.assertIn("features.plugins=false", joined)
        self.assertIn("features.view_image=false", joined)
        self.assertIn("tools.update_plan.enabled=false", joined)
        self.assertIn('forced_login_method="chatgpt"', joined)
        self.assertIn('cli_auth_credentials_store="keyring"', joined)
        self.assertIn('otel.metrics_exporter="none"', joined)
        self.assertIn("sqlite_home=", joined)

    def test_effective_config_requires_every_hardening_control(self) -> None:
        config = {
            "analytics": {"enabled": False},
            "approval_policy": "never",
            "check_for_update_on_startup": False,
            "cli_auth_credentials_store": "keyring",
            "feedback": {"enabled": False},
            "forced_login_method": "chatgpt",
            "features": {name: False for name in _REQUIRED_DISABLED_FEATURES},
            "history": {"persistence": "none"},
            "hooks": {"PreToolUse": [], "SessionStart": []},
            "mcp_servers": {},
            "otel": {
                "exporter": "none",
                "log_user_prompt": False,
                "metrics_exporter": "none",
                "trace_exporter": "none",
            },
            "plugins": {},
            "shell_environment_policy": {
                "ignore_default_excludes": False,
                "inherit": "none",
            },
        }
        validate_effective_config({"config": config})

        config["features"]["shell_tool"] = True
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "HARDENING_FEATURE_NOT_DISABLED",
        ):
            validate_effective_config({"config": config})

    def test_start_responses_prove_model_cwd_sandbox_and_initial_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-response-check-") as root:
            cwd = Path(root).resolve()
            thread_result = {
                "approvalPolicy": "never",
                "cwd": str(cwd),
                "instructionSources": [],
                "model": "gpt-5.6-sol",
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "thread": {
                    "cwd": str(cwd),
                    "ephemeral": False,
                    "id": "thread-1",
                },
            }
            self.assertEqual(
                _validate_thread_start_result(
                    thread_result,
                    model="gpt-5.6-sol",
                    sterile_cwd=cwd,
                ),
                "thread-1",
            )
            self.assertEqual(
                _validate_turn_start_result(
                    {
                        "turn": {
                            "error": None,
                            "id": "turn-1",
                            "items": [],
                            "status": "inProgress",
                        }
                    }
                ),
                "turn-1",
            )
            thread_result["model"] = "rerouted-model"
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "THREAD_START_RESULT_INVALID",
            ):
                _validate_thread_start_result(
                    thread_result,
                    model="gpt-5.6-sol",
                    sterile_cwd=cwd,
                )

    def test_crash_snapshot_requires_exact_thread_and_turn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-crash-check-") as root:
            cwd = Path(root).resolve()
            result = {
                "thread": {
                    "cwd": str(cwd),
                    "ephemeral": False,
                    "id": "thread-1",
                    "turns": [{"id": "turn-1", "status": "interrupted"}],
                }
            }
            self.assertEqual(
                _turn_status_from_thread_result(
                    result,
                    thread_id="thread-1",
                    turn_id="turn-1",
                    sterile_cwd=cwd,
                ),
                "interrupted",
            )
            result["thread"]["turns"].append({"id": "turn-1", "status": "interrupted"})
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CRASH_TURN_SNAPSHOT_INVALID",
            ):
                _turn_status_from_thread_result(
                    result,
                    thread_id="thread-1",
                    turn_id="turn-1",
                    sterile_cwd=cwd,
                )

    def test_event_ledger_rejects_tools_identity_drift_and_duplicate_terminal(
        self,
    ) -> None:
        ledger = EventLedger()
        ledger.bind_thread("thread-1")
        ledger.bind_turn("turn-1")
        ledger.observe(
            {
                "method": "thread/started",
                "params": {"thread": {"id": "thread-1"}},
            }
        )
        ledger.observe(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "inProgress"},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "user-1", "type": "userMessage"},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "user-1", "type": "userMessage"},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "agent-1", "type": "agentMessage"},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "agent-1",
                        "type": "agentMessage",
                        "text": "synthetic",
                    },
                },
            }
        )
        ledger.observe(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )
        ledger.observe(
            {
                "method": "thread/deleted",
                "params": {"threadId": "thread-1"},
            }
        )
        ledger.validate_normal_sequence(thread_id="thread-1", turn_id="turn-1")
        self.assertEqual(ledger.terminal_turns, {"turn-1": "completed"})
        self.assertNotIn("synthetic", json.dumps(ledger.content_free_summary()))

        with self.assertRaisesRegex(
            LifecycleProbeError,
            "TURN_TERMINAL_DUPLICATE",
        ):
            ledger.observe(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                }
            )
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "SECURITY_POLICY_VIOLATION",
        ):
            EventLedger().observe(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": "command-1", "type": "commandExecution"},
                    },
                }
            )
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "EVENT_THREAD_ID_MISMATCH",
        ):
            ledger.observe(
                {
                    "method": "warning",
                    "params": {"threadId": "thread-2", "message": "discarded"},
                }
            )

    def test_interrupted_sequence_allows_unfinished_started_item(self) -> None:
        ledger = EventLedger()
        ledger.bind_thread("thread-1")
        ledger.bind_turn("turn-1")
        ledger.observe(
            {
                "method": "thread/started",
                "params": {"thread": {"id": "thread-1"}},
            }
        )
        ledger.observe(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "inProgress"},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "user-1", "type": "userMessage"},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "user-1", "type": "userMessage"},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "reason-1", "type": "reasoning"},
                },
            }
        )
        ledger.observe(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "interrupted"},
                },
            }
        )

        ledger.validate_interrupted_sequence(
            thread_id="thread-1",
            turn_id="turn-1",
        )

    def test_structured_response_is_validated_but_never_returned(self) -> None:
        raw = json.dumps({"marker": SYNTHETIC_MARKER}, separators=(",", ":"))
        digest = _validate_synthetic_response(raw)

        self.assertEqual(digest, hashlib.sha256(raw.encode("utf-8")).hexdigest())
        self.assertNotIn(SYNTHETIC_MARKER, digest)
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "STRUCTURED_RESPONSE_INVALID",
        ):
            _validate_synthetic_response('{"marker":"forged"}')

    def test_jsonl_session_keeps_notification_that_precedes_response(self) -> None:
        script = r"""
import json
import sys
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "initialize":
        print(json.dumps({"method":"thread/started","params":{"thread":{"id":"thread-1"}}}), flush=True)
        print(json.dumps({"id":message["id"],"result":{}}), flush=True)
"""
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            ledger = EventLedger()
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", script],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=5,
                ledger=ledger,
            )
            try:
                self.assertEqual(session.request("initialize", {}), {})
                ledger.bind_thread("thread-1")
            finally:
                session.close()

        self.assertEqual(ledger.method_counts["thread/started"], 1)

    def test_jsonl_session_rejects_server_requests(self) -> None:
        script = r"""
import json
import sys
for raw in sys.stdin:
    message = json.loads(raw)
    print(json.dumps({"method":"item/commandExecution/requestApproval","id":99,"params":{}}), flush=True)
"""
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", script],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=5,
                ledger=EventLedger(),
            )
            try:
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "SERVER_REQUEST_FORBIDDEN",
                ):
                    session.request("initialize", {})
            finally:
                session.close()

    def test_jsonl_session_ordinary_eof_is_not_labeled_crash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", "pass"],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=1,
                ledger=EventLedger(),
            )
            try:
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "APP_SERVER_EOF",
                ):
                    session.request("initialize", {})
            finally:
                session.close()

    def test_jsonl_session_graceful_stop_uses_stdin_eof(self) -> None:
        script = "import sys\nfor _ in sys.stdin: pass\n"
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", script],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=1,
                ledger=EventLedger(),
            )
            session.stop_gracefully()
            session.close()

        self.assertEqual(session.process.returncode, 0)

    def test_jsonl_session_interrupts_turn_when_cancellation_is_requested(self) -> None:
        script = r"""
import json
import sys
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "turn/interrupt":
        print(json.dumps({"id":message["id"],"result":{}}), flush=True)
        print(json.dumps({
            "method":"turn/completed",
            "params":{
                "threadId":"thread-1",
                "turn":{"id":"turn-1","status":"interrupted"}
            }
        }), flush=True)
"""
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            ledger = EventLedger()
            ledger.bind_thread("thread-1")
            ledger.bind_turn("turn-1")
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", script],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=2,
                ledger=ledger,
            )
            try:
                status = session.wait_for_turn(
                    "turn-1",
                    cancellation_requested=lambda: True,
                    cancellation_poll_seconds=0.01,
                )
            finally:
                session.close()

        self.assertEqual(status, "interrupted")
        self.assertEqual(ledger.terminal_turns["turn-1"], "interrupted")

    def test_jsonl_session_steers_exact_active_turn_and_acknowledges(self) -> None:
        script = r"""
import json
import sys
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "turn/steer":
        params = message["params"]
        assert params["threadId"] == "thread-1"
        assert params["expectedTurnId"] == "turn-1"
        assert params["input"] == [{"type":"text","text":"structured steer"}]
        print(json.dumps({"id":message["id"],"result":{"turnId":"turn-1"}}), flush=True)
        print(json.dumps({
            "method":"turn/completed",
            "params":{
                "threadId":"thread-1",
                "turn":{"id":"turn-1","status":"completed"}
            }
        }), flush=True)
"""
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            ledger = EventLedger()
            ledger.bind_thread("thread-1")
            ledger.bind_turn("turn-1")
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", script],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=2,
                ledger=ledger,
            )
            commands = [("steer-1", "structured steer")]
            delivered: list[str] = []
            try:
                status = session.wait_for_turn(
                    "turn-1",
                    steer_requested=lambda: commands.pop(0) if commands else None,
                    steer_delivered=delivered.append,
                    cancellation_poll_seconds=0.01,
                )
            finally:
                session.close()

        self.assertEqual(status, "completed")
        self.assertEqual(delivered, ["steer-1"])

    def test_jsonl_session_forced_kill_confirms_unknown_after_crash(self) -> None:
        script = "import time\ntime.sleep(30)\n"
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            ledger = EventLedger()
            ledger.bind_turn("turn-1")
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", script],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=1,
                ledger=ledger,
            )
            session.kill_and_confirm_unknown("turn-1")
            session.close()

        self.assertEqual(session.process.returncode, -9)

    def test_jsonl_session_partial_line_honors_deadline(self) -> None:
        script = r"""
import json
import sys
import time
for raw in sys.stdin:
    json.loads(raw)
    sys.stdout.write("{")
    sys.stdout.flush()
    time.sleep(2)
"""
        with tempfile.TemporaryDirectory(prefix="reqpilotx-rpc-check-") as root:
            session = JsonlRpcSession(
                command=[sys.executable, "-u", "-c", script],
                environment=os.environ,
                cwd=Path(root),
                timeout_seconds=0.1,
                ledger=EventLedger(),
            )
            started = __import__("time").monotonic()
            try:
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "APP_SERVER_TIMEOUT",
                ):
                    session.request("initialize", {})
            finally:
                session.close(force=True)
            elapsed = __import__("time").monotonic() - started

        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
