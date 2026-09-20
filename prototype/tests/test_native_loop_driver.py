from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()


import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tempfile
import unittest

from app.adapters.agents.codex_app_server import native_loop_driver as native


class Host:
    def __init__(self):
        self.calls = []

    def specs(self):
        return [
            native.NativeLoopToolSpec(
                "reqpilot_read",
                "Read original source",
                {
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {"path": {"type": "string"}},
                },
            )
        ]

    def invoke(self, name, args):
        self.calls.append((name, args))
        return {
            "success": True,
            "content_items": [{"type": "inputText", "text": "original"}],
        }


def tool_call(ledger, call_id="call-1", args=None):
    args = {"path": "source.json"} if args is None else args
    ledger.observe(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": call_id,
                    "type": "dynamicToolCall",
                    "tool": "reqpilot_read",
                    "namespace": None,
                    "arguments": args,
                    "status": "inProgress",
                },
            },
        }
    )
    return {
        "method": "item/tool/call",
        "id": "rpc-1",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": call_id,
            "tool": "reqpilot_read",
            "namespace": None,
            "arguments": args,
        },
    }


def test_tool_replay_and_crash_outcome_do_not_repeat_mutation(tmp_path):
    host = Host()
    ledger = native.NativeLoopEventLedger()
    ledger.bind_thread("thread-1")
    ledger.bind_turn("turn-1")
    dispatcher = native.NativeLoopToolDispatcher(
        host=host, ledger=ledger, calls_dir=tmp_path
    )
    call = tool_call(ledger)
    first = dispatcher(call)
    assert dispatcher(call) == first
    assert len(host.calls) == 1
    receipt = next(tmp_path.glob("*.json"))
    previous = json.loads(receipt.read_text())
    previous["status"] = "INVOKING"
    native._write_json(receipt, previous)
    unknown = dispatcher(call)
    assert unknown["success"] is False
    assert "OUTCOME_UNKNOWN" in unknown["contentItems"][0]["text"]
    assert len(host.calls) == 1


def test_tool_invalid_arguments_can_be_revised_but_identity_cannot(tmp_path):
    host = Host()
    ledger = native.NativeLoopEventLedger()
    ledger.bind_thread("thread-1")
    ledger.bind_turn("turn-1")
    dispatcher = native.NativeLoopToolDispatcher(
        host=host, ledger=ledger, calls_dir=tmp_path
    )
    bad = tool_call(ledger, args={"unexpected": "value"})
    assert dispatcher(bad)["success"] is False
    good = tool_call(ledger, call_id="call-2")
    assert dispatcher(good)["success"] is True
    good["params"]["threadId"] = "another-thread"
    with unittest.TestCase().assertRaisesRegex(
        native.LifecycleProbeError, "IDENTITY_MISMATCH"
    ):
        dispatcher(good)
    assert len(host.calls) == 1


def test_native_image_output_accepts_data_only_and_bounds_size():
    image = {"type": "inputImage", "imageUrl": "data:image/png;base64,aGVsbG8="}
    assert native._tool_result({"success": True, "content_items": [image]})[
        "contentItems"
    ] == [image]
    with unittest.TestCase().assertRaisesRegex(ValueError, "data URLs"):
        native._tool_result(
            {
                "success": True,
                "content_items": [
                    {
                        "type": "inputImage",
                        "imageUrl": "https://example.invalid/customer.png",
                    }
                ],
            }
        )
    assert (
        native._tool_result(
            {
                "success": True,
                "content_items": [{"type": "inputText", "text": "x" * 950_001}],
            }
        )["success"]
        is False
    )


def test_ledger_retains_public_actions_but_not_reasoning_text():
    events = []
    ledger = native.NativeLoopEventLedger(emit=events.append)
    ledger.bind_thread("thread-1")
    ledger.bind_turn("turn-1")
    for kind, body in [
        ("reasoning", {"summary": ["private reasoning"]}),
        ("agentMessage", {"phase": "final_answer", "text": "Published draft"}),
    ]:
        for method in ("item/started", "item/completed"):
            ledger.observe(
                {
                    "method": method,
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": kind, "type": kind, **body},
                    },
                }
            )
    assert "private reasoning" not in json.dumps(events)
    assert events[-1]["text"] == "Published draft"


class FakeSession:
    turns = []
    instances = []
    creation_cwd = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.ledger = kwargs["ledger"]
        self.process = SimpleNamespace(pid=123)
        self.methods = []
        self.instances.append(self)

    def request(self, method, params):
        self.methods.append(method)
        if method in {"thread/start", "thread/resume"}:
            if method == "thread/start":
                assert params["dynamicTools"][0]["name"] == "reqpilot_read"
                FakeSession.creation_cwd = str(self.kwargs["cwd"])
            return {
                "model": "model",
                "cwd": str(self.kwargs["cwd"]),
                "approvalPolicy": "never",
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "instructionSources": [],
                "thread": {
                    "id": "thread-1",
                    "cwd": FakeSession.creation_cwd,
                    "ephemeral": False,
                    "turns": list(self.turns),
                },
            }
        if method == "turn/start":
            assert "outputSchema" not in params
            assert "SOURCE_PACKET" not in params["input"][-1]["text"]
            turn = {
                "id": "turn-" + str(len(self.turns) + 1),
                "status": "inProgress",
                "items": [],
                "error": None,
            }
            self.turns.append(turn)
            return {"turn": turn.copy()}
        if method == "thread/read":
            assert params["includeTurns"] is False
            return {
                "thread": {
                    "id": "thread-1",
                    "turns": [],
                    "cwd": FakeSession.creation_cwd,
                }
            }
        if method == "thread/turns/list":
            assert params["itemsView"] == "notLoaded"
            return {
                "data": [{**turn, "items": []} for turn in self.turns],
                "nextCursor": None,
            }
        raise AssertionError(method)

    def wait_for_turn(self, turn_id, **kwargs):
        self.turns[-1]["status"] = "completed"
        self.ledger.final_agent_message = "Completed with host receipts"
        return "completed"

    def close(self, **kwargs):
        pass


def test_persistent_thread_resumes_without_replacing_it_or_deleting_history(tmp_path):
    driver = object.__new__(native.LocalCodexAppServerNativeLoopDriver)
    driver.model_id, driver.reasoning_effort = "model", "high"
    driver.codex_home = tmp_path / "auth-home"
    source = tmp_path / "skills" / "reqpilotx-test" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: reqpilotx-test\ndescription: Test\n---\nUse tools.")
    root = tmp_path / "run"

    def prepare(root):
        for name in ("workspace", "calls"):
            (root / name).mkdir(exist_ok=True)
        return SimpleNamespace(
            sterile_cwd=root / "workspace",
            codex_home=driver.codex_home,
            command=(),
            environment={},
            protocol={"binary_sha256": "binary"},
        )

    FakeSession.instances, FakeSession.turns = [], []
    with (
        patch.object(driver, "_prepare_runtime", side_effect=prepare),
        patch.object(
            native,
            "_isolate_runtime_skills",
            side_effect=lambda runtime, **kwargs: (runtime, 0),
        ),
        patch.object(native, "_preflight_session"),
        patch.object(native, "_verify_skill_allowlist"),
        patch.object(native, "NativeLoopRpcSession", FakeSession),
    ):
        args = dict(
            session_dir=root,
            skill_paths={"reqpilotx-test": source},
            tool_host=Host(),
            binding={"source": "sha"},
        )
        first = driver.run(goal="Read, revise and export", **args)
        second = driver.run(goal="Apply this feedback", **args)
    assert first.receipt["thread_id"] == second.receipt["thread_id"] == "thread-1"
    assert first.receipt["turn_id"] != second.receipt["turn_id"]
    assert second.receipt["resumed"] is True
    methods = [
        method for session in FakeSession.instances for method in session.methods
    ]
    assert methods.count("thread/start") == 1
    assert methods.count("thread/resume") == 1
    assert "thread/delete" not in methods
    assert len(list(root.glob("receipt-*.json"))) == 2
    assert json.loads((root / "run-state.json").read_text())["status"] == "COMPLETED"


def test_concurrent_same_session_is_rejected(tmp_path):
    with native._session_lock(tmp_path):
        with unittest.TestCase().assertRaisesRegex(
            native.LifecycleProbeError, "ALREADY_RUNNING"
        ):
            with native._session_lock(tmp_path):
                raise AssertionError("Concurrent lock unexpectedly obtained")


def test_skill_bundle_drift_rejected_on_resume(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "bundle" / "skills" / "entry" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("Read portable references")
    reference = tmp_path / "bundle" / "references" / "rules.md"
    reference.parent.mkdir()
    reference.write_text("Preserve original")
    native._materialize_skills(
        workspace, {"reqpilotx-test": source}, tmp_path / "bundle"
    )
    reference.write_text("Changed meaning")
    with unittest.TestCase().assertRaisesRegex(
        native.LifecycleProbeError, "BUNDLE_DRIFT"
    ):
        native._materialize_skills(
            workspace, {"reqpilotx-test": source}, tmp_path / "bundle"
        )


class NativeLoopDriverTests(unittest.TestCase):
    def test_terminal_index_failure_recovers_original_turn_without_second_run(self):
        from app.adapters.agents.codex_app_server import native_loop_history as history

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = object.__new__(native.LocalCodexAppServerNativeLoopDriver)
            recovered = native.NativeLoopResult(
                status="COMPLETED",
                final_message="actual final",
                receipt={"turn_id": "original-turn"},
            )
            with (
                patch.object(
                    driver,
                    "_run_locked",
                    side_effect=native.LifecycleProbeError(
                        "NATIVE_LOOP_TERMINAL_SNAPSHOT_MISMATCH"
                    ),
                ) as run,
                patch.object(
                    history, "observed_completed_native_turn", return_value=True
                ),
                patch.object(history, "reconcile_native_loop_history") as reconcile,
                patch.object(
                    history, "recover_completed_native_result", return_value=recovered
                ),
            ):
                result = driver.run(
                    session_dir=root,
                    goal="Authorized goal",
                    skill_paths={"entry": root / "skill"},
                    tool_host=Host(),
                    binding={"source": "sha"},
                )
            self.assertIs(result, recovered)
            run.assert_called_once()
            reconcile.assert_called_once_with(
                driver=driver, session_dir=root, apply=True
            )
            with (
                patch.object(
                    driver,
                    "_run_locked",
                    side_effect=native.LifecycleProbeError(
                        "NATIVE_LOOP_TERMINAL_SNAPSHOT_MISMATCH"
                    ),
                ),
                patch.object(
                    history, "observed_completed_native_turn", return_value=False
                ),
                patch.object(history, "reconcile_native_loop_history") as reconcile,
            ):
                with self.assertRaisesRegex(
                    native.LifecycleProbeError, "SNAPSHOT_MISMATCH"
                ):
                    driver.run(
                        session_dir=root,
                        goal="Authorized goal",
                        skill_paths={"entry": root / "skill"},
                        tool_host=Host(),
                        binding={"source": "sha"},
                    )
                reconcile.assert_not_called()

    def test_oversized_status_probe_keeps_unknown_and_original_handle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = {
                "status": "UNKNOWN",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "pid": None,
                "app_server_pid": None,
                "reason_code": "APP_SERVER_TIMEOUT",
            }
            native._write_json(root / "run-state.json", state)
            driver = object.__new__(native.LocalCodexAppServerNativeLoopDriver)
            driver.model_id, driver.reasoning_effort = "model", "high"
            runtime = SimpleNamespace(
                command=(), environment={}, sterile_cwd=root, codex_home=root
            )
            with (
                patch.object(driver, "_prepare_runtime", return_value=runtime),
                patch.object(native, "NativeLoopRpcSession", FakeSession),
                patch.object(native, "_preflight_session"),
                patch.object(
                    native,
                    "_read_status_snapshot",
                    side_effect=native.LifecycleProbeError("INBOUND_MESSAGE_TOO_LARGE"),
                ),
            ):
                with self.assertRaisesRegex(
                    native.LifecycleProbeError, "INBOUND_MESSAGE_TOO_LARGE"
                ):
                    driver.inspect_session(session_dir=root)
            self.assertEqual(json.loads((root / "run-state.json").read_text()), state)
            self.assertFalse(
                any(
                    method == "turn/start"
                    for method in FakeSession.instances[-1].methods
                )
            )

    def test_status_pages_never_load_tool_items(self):
        calls = []

        class Pages:
            def request(self, method, params):
                calls.append((method, params))
                if method == "thread/read":
                    return {"thread": {"id": "t", "cwd": "/task", "turns": []}}
                index = 1 if params.get("cursor") else 0
                return {
                    "data": [
                        {"id": "u" + str(index), "status": "completed", "items": []}
                    ],
                    "nextCursor": None if index else "page-2",
                }

        result = native._read_status_snapshot(Pages(), "t")
        self.assertEqual(result["status_read"]["page_count"], 2)
        self.assertEqual(
            native._turn_statuses(result),
            [{"id": "u0", "status": "completed"}, {"id": "u1", "status": "completed"}],
        )
        self.assertFalse(calls[0][1]["includeTurns"])
        self.assertTrue(
            all(params["itemsView"] == "notLoaded" for _, params in calls[1:])
        )

    def test_status_page_failures_do_not_infer_terminal(self):
        class InvalidPage:
            def __init__(self, body):
                self.body = body

            def request(self, method, params):
                return {"thread": {"id": "t"}} if method == "thread/read" else self.body

        for page, reason in [
            (
                {
                    "data": [
                        {
                            "id": "u",
                            "status": "completed",
                            "items": [{"type": "dynamicToolCall"}],
                        }
                    ]
                },
                "STATUS_PAGE_INVALID",
            ),
            ({"data": [], "nextCursor": "again"}, "STATUS_PAGE_CURSOR_INVALID"),
            (
                {"data": [{"id": "u", "status": "unknown", "items": []}]},
                "STATUS_PAGE_INVALID",
            ),
        ]:
            with self.assertRaisesRegex(native.LifecycleProbeError, reason):
                native._read_status_snapshot(InvalidPage(page), "t")

        class Excessive:
            def request(self, method, params):
                if method == "thread/read":
                    return {"thread": {"id": "t"}}
                return {
                    "data": [],
                    "nextCursor": str(int(params.get("cursor", "0")) + 1),
                }

        with self.assertRaisesRegex(native.LifecycleProbeError, "STATUS_PAGE_LIMIT"):
            native._read_status_snapshot(Excessive(), "t")

    def test_system_error_waits_for_detail_and_terminal_without_weakening_stage_ledger(
        self,
    ):
        events = []
        ledger = native.NativeLoopEventLedger(emit=events.append)
        ledger.bind_thread("thread-1")
        ledger.bind_turn("turn-1")
        status = {
            "method": "thread/status/changed",
            "params": {"threadId": "thread-1", "status": {"type": "systemError"}},
        }
        ledger.observe(status)
        self.assertIsNotNone(ledger.error_drain_since)
        self.assertEqual(ledger.terminal_turns, {})
        detail = {
            "method": "error",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": False,
                "error": {
                    "message": "private customer body must not be logged",
                    "codexErrorInfo": "badRequest",
                },
            },
        }
        ledger.observe(detail)
        self.assertEqual(ledger.pending_error_reason, "APP_SERVER_REQUEST_REJECTED")
        ledger.observe(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "failed", "items": []},
                },
            }
        )
        self.assertEqual(ledger.terminal_turns["turn-1"], "failed")
        self.assertIsNone(ledger.error_drain_since)
        self.assertNotIn("private customer body", json.dumps(events))
        strict = native.EventLedger()
        with self.assertRaisesRegex(native.LifecycleProbeError, "THREAD_SYSTEM_ERROR"):
            strict.observe(status)

    def test_retryable_service_errors_return_to_sdk_and_grace_is_bounded(self):
        ledger = native.NativeLoopEventLedger()
        ledger.bind_thread("thread-1")
        ledger.bind_turn("turn-1")
        ledger.observe(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "thread-1", "status": {"type": "systemError"}},
            }
        )
        ledger.observe(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": True,
                    "error": {"codexErrorInfo": "internalServerError"},
                },
            }
        )
        self.assertIsNone(ledger.error_drain_since)
        self.assertIsNone(ledger.pending_error_reason)
        session = object.__new__(native.NativeLoopRpcSession)
        session.ledger = ledger
        ledger.error_drain_since = 10.0
        with (
            patch.object(native.time, "monotonic", return_value=41.0),
            patch.object(
                native.JsonlRpcSession,
                "_read",
                side_effect=native.LifecycleProbeError("APP_SERVER_TIMEOUT"),
            ) as read,
        ):
            with self.assertRaisesRegex(
                native.LifecycleProbeError, "THREAD_SYSTEM_ERROR_WITHOUT_TERMINAL"
            ):
                session._read(1000.0)
            self.assertEqual(read.call_args.args[0], 40.0)
        with self.assertRaisesRegex(native.LifecycleProbeError, "IDENTITY_MISMATCH"):
            ledger.observe(
                {
                    "method": "error",
                    "params": {
                        "threadId": "other",
                        "turnId": "turn-1",
                        "willRetry": False,
                        "error": {},
                    },
                }
            )

    def test_tool_argument_diagnostics_name_bad_fields_without_echoing_values(self):
        schema = {
            "type": "object",
            "required": ["name", "value"],
            "properties": {"name": {"type": "string"}, "value": {"type": "object"}},
            "additionalProperties": False,
        }
        errors = list(
            native.Draft202012Validator(schema).iter_errors(
                {"Push": "private source details", "name": "candidate", "value": {}}
            )
        )
        result = native._argument_error_summaries(errors)
        self.assertEqual(result[0]["unexpected_fields"], ["Push"])
        self.assertNotIn("private source details", json.dumps(result))

    def test_context_compaction_is_a_public_lifecycle_event_without_content(self):
        events = []
        ledger = native.NativeLoopEventLedger(emit=events.append)
        ledger.bind_thread("thread")
        ledger.bind_turn("turn")
        for method in ("item/started", "item/completed"):
            ledger.observe(
                {
                    "method": method,
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "item": {
                            "id": "compaction-1",
                            "type": "contextCompaction",
                            "summary": "PRIVATE_COMPACTED_CONTENT",
                        },
                    },
                }
            )
        self.assertEqual(ledger.item_type_counts["contextCompaction"], 2)
        self.assertEqual(
            [event["status"] for event in events], ["STARTED", "COMPLETED"]
        )
        self.assertNotIn("PRIVATE_COMPACTED_CONTENT", json.dumps(events))
        self.assertIn("compaction-1", ledger.completed_item_ids)
        ledger.observe(
            {
                "method": "thread/compacted",
                "params": {"threadId": "thread", "turnId": "turn"},
            }
        )
        self.assertEqual(ledger.method_counts["thread/compacted"], 1)

    def test_capability_epoch_preserves_thread_and_old_skill_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            driver = object.__new__(native.LocalCodexAppServerNativeLoopDriver)
            driver.model_id, driver.reasoning_effort = "model", "high"
            driver.codex_home = temp / "auth"
            first_skill = temp / "first/SKILL.md"
            first_skill.parent.mkdir()
            first_skill.write_text(
                "---\nname: reqpilotx-test\ndescription: Test\n---\nCapability one."
            )
            second_skill = temp / "second/SKILL.md"
            second_skill.parent.mkdir()
            second_skill.write_text(
                "---\nname: reqpilotx-test\ndescription: Test\n---\nCapability two."
            )
            root = temp / "runtime"

            def prepare(root, workspace_relative="workspace"):
                workspace = root / workspace_relative
                workspace.mkdir(parents=True, exist_ok=True)
                (root / "calls").mkdir(exist_ok=True)
                return SimpleNamespace(
                    sterile_cwd=workspace,
                    codex_home=driver.codex_home,
                    command=(),
                    environment={},
                    protocol={"binary_sha256": "binary"},
                )

            FakeSession.instances, FakeSession.turns = [], []
            with (
                patch.object(driver, "_prepare_runtime", side_effect=prepare),
                patch.object(
                    native,
                    "_isolate_runtime_skills",
                    side_effect=lambda runtime, **kwargs: (runtime, 0),
                ),
                patch.object(native, "_preflight_session"),
                patch.object(native, "_verify_skill_allowlist"),
                patch.object(native, "NativeLoopRpcSession", FakeSession),
            ):
                arguments = {
                    "session_dir": root,
                    "tool_host": Host(),
                    "binding": {"source": "same-source"},
                }
                original = driver.run(
                    goal="First source task",
                    skill_paths={"reqpilotx-test": first_skill},
                    **arguments,
                )
                old_files = native._workspace_manifest(root / "workspace")
                state = json.loads((root / "run-state.json").read_text())
                epoch = {
                    "schema_version": "reqpilotx-native-capability-epoch/v1",
                    "epoch_id": "cap-12345678",
                    "previous_identity_sha256": native._digest(state["identity"]),
                    "previous_bundle_sha256": "a" * 64,
                    "bundle_sha256": "b" * 64,
                    "origin": "AGENT_SELF_REVIEW",
                    "reason": "Enable a validated compiler",
                    "authorization_reference": "host-upgrade-event.json",
                }
                updated = driver.run(
                    goal="Continue the same source task",
                    skill_paths={"reqpilotx-test": second_skill},
                    capability_epoch=epoch,
                    **arguments,
                )
            self.assertEqual(
                original.receipt["thread_id"], updated.receipt["thread_id"]
            )
            self.assertEqual(old_files, native._workspace_manifest(root / "workspace"))
            state = json.loads((root / "run-state.json").read_text())
            self.assertEqual(
                state["runtime_workspace_relative"],
                "capability-workspaces/cap-12345678",
            )
            self.assertEqual(
                updated.receipt["capability_transition"]["status"], "ACTIVATED"
            )
            self.assertEqual(
                updated.receipt["identity"]["tools_sha256"],
                original.receipt["identity"]["tools_sha256"],
            )
            self.assertNotEqual(
                updated.receipt["identity"]["skills_sha256"],
                original.receipt["identity"]["skills_sha256"],
            )

    def test_capability_epoch_refuses_active_or_different_predecessor(self):
        state = {
            "thread_id": "thread",
            "status": "RUNNING",
            "identity": {"binding_sha256": "source"},
        }
        epoch = {
            "schema_version": "reqpilotx-native-capability-epoch/v1",
            "epoch_id": "cap-12345678",
            "previous_identity_sha256": native._digest(state["identity"]),
            "previous_bundle_sha256": "a" * 64,
            "bundle_sha256": "b" * 64,
            "origin": "AGENT_SELF_REVIEW",
            "reason": "Add source read capability",
            "authorization_reference": "host-authorized.json",
        }
        with self.assertRaisesRegex(native.LifecycleProbeError, "VERIFIED_TERMINAL"):
            native._capability_workspace(state, epoch)
        state.update(
            status="COMPLETED", last_receipt={"terminal_snapshot_verified": True}
        )
        with self.assertRaisesRegex(native.LifecycleProbeError, "PREDECESSOR_CHANGED"):
            native._capability_workspace(
                state, {**epoch, "previous_identity_sha256": "f" * 64}
            )

    def test_optional_host_tools_do_not_change_registered_thread_contract(self):
        def schema(operations):
            return {
                "type": "object",
                "properties": {"operation": {"type": "string", "enum": operations}},
                "required": ["operation"],
                "additionalProperties": False,
            }

        class VersionedHost:
            def __init__(self, operations):
                self.operations = operations

            def specs(self):
                return [
                    native.NativeLoopToolSpec(
                        "reqpilot_run", "Portable operations", schema(self.operations)
                    )
                ]

            def invoke(self, name, args):
                return {
                    "success": True,
                    "content_items": [{"type": "inputText", "text": args["operation"]}],
                }

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, original = native._pin_tool_host(root, {}, VersionedHost(["read"]))
            state = {
                "thread_id": "retained-thread",
                "identity": {"tools_sha256": native._digest(original)},
            }
            host, resumed = native._pin_tool_host(
                root, state, VersionedHost(["read", "new-operation"])
            )
            self.assertEqual(resumed, original)
            self.assertEqual(
                host.specs()[0].input_schema["properties"]["operation"]["enum"],
                ["read"],
            )
            self.assertTrue(
                host.invoke("reqpilot_run", {"operation": "read"})["success"]
            )

    def test_legacy_contract_recovers_only_from_matching_sdk_registry(self):
        old = native.NativeLoopToolSpec(
            "reqpilot_read",
            "Original tool",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )

        class UpgradedHost(Host):
            def specs(self):
                return [
                    native.NativeLoopToolSpec(
                        "reqpilot_read", "New description", old.input_schema
                    )
                ]

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "sqlite").mkdir()
            db = sqlite3.connect(root / "sqlite/state_5.sqlite")
            db.execute(
                "CREATE TABLE thread_dynamic_tools(thread_id TEXT, position INTEGER, name TEXT, description TEXT, input_schema TEXT, defer_loading INTEGER, namespace TEXT)"
            )
            db.execute(
                "INSERT INTO thread_dynamic_tools VALUES(?,?,?,?,?,?,?)",
                (
                    "thread",
                    0,
                    old.name,
                    old.description,
                    json.dumps(old.input_schema),
                    0,
                    None,
                ),
            )
            db.commit()
            db.close()
            state = {
                "thread_id": "thread",
                "identity": {
                    "tools_sha256": native._digest([old.app_server_payload()])
                },
            }
            _, actual = native._pin_tool_host(root, state, UpgradedHost())
            self.assertEqual(actual, [old.app_server_payload()])
            self.assertEqual(
                json.loads((root / "tool-specs.json").read_text())["origin"],
                "PINNED_SDK_TOOL_REGISTRY",
            )
            with self.assertRaisesRegex(
                native.LifecycleProbeError, "RECOVERY_HASH_MISMATCH"
            ):
                native._recover_sdk_tool_contract(
                    root, "another-thread", state["identity"]["tools_sha256"]
                )

    def test_host_cannot_silently_narrow_the_registered_tool_contract(self):
        old = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
        narrowed = {
            **old,
            "properties": {"path": {"type": "string", "enum": ["one-file-only"]}},
        }
        self.assertFalse(native._schema_accepts_previous(old, narrowed))
        self.assertFalse(
            native._schema_accepts_previous(
                old, {**old, "required": ["path", "new-required"]}
            )
        )
        self.assertFalse(
            native._schema_accepts_previous(
                {"type": "object", "additionalProperties": True},
                {
                    "type": "object",
                    "properties": {"previously_free": {"type": "string"}},
                    "additionalProperties": True,
                },
            )
        )
        self.assertTrue(
            native._schema_accepts_previous(
                {"type": "integer", "minimum": 1, "maximum": 500},
                {"type": "integer", "minimum": 1, "maximum": 2000},
            )
        )

    def test_replay_and_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            test_tool_replay_and_crash_outcome_do_not_repeat_mutation(Path(root))

    def test_tool_revision_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            test_tool_invalid_arguments_can_be_revised_but_identity_cannot(Path(root))

    def test_image_output_contract(self):
        test_native_image_output_accepts_data_only_and_bounds_size()

    def test_public_events(self):
        test_ledger_retains_public_actions_but_not_reasoning_text()

    def test_thread_continuity(self):
        with tempfile.TemporaryDirectory() as root:
            test_persistent_thread_resumes_without_replacing_it_or_deleting_history(
                Path(root)
            )

    def test_concurrent_session(self):
        with tempfile.TemporaryDirectory() as root:
            test_concurrent_same_session_is_rejected(Path(root))

    def test_skill_drift(self):
        with tempfile.TemporaryDirectory() as root:
            test_skill_bundle_drift_rejected_on_resume(Path(root))
