"""Synthetic provider contract: persistent tools, restart and redacted identity."""
# ruff: noqa: E402
from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from openai import AzureOpenAI, OpenAI
from unittest import TestCase
from unittest.mock import Mock, patch

from app.adapters.agents.codex_app_server.native_loop_driver import NativeLoopToolSpec, _digest
from reqpilotx_server.adapters.agents.provider_tool_loop import (
    ADAPTER_ID, TRANSPORT_KIND, ProviderToolLoopDriver, ProviderToolLoopError,
    build_provider_tool_loop_driver, _write_json,
)
from app.infrastructure.runtime_settings import (
    AgentProvider, AgentRuntimeBackend, RuntimeSettings, RuntimeConfigurationError,
)
from app.application.agent_workflow import source_document_loop_jobs as document_jobs
from app.application.agent_workflow import source_item_loop_jobs as item_jobs
from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from app.application.agent_workflow.native_loop_finalization import (
    _completed_provider_result, verified_finalization_inputs,
)


class FakeCompletions:
    def __init__(self, responses, on_call=None):
        self.responses = list(responses)
        self.calls = []
        self.on_call = on_call
        self.options = []

    def client(self):
        owner = self

        class Client:
            chat = SimpleNamespace(completions=owner)

            def with_options(self, **kwargs):
                owner.options.append(kwargs)
                return self

        return Client()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call()
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def tool_choice(call_id="call-1", name="reqpilot_read", arguments='{"path":"source"}'):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": call_id,
        "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}


def final_choice(text="Done"):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


class ToolHost:
    def __init__(self, image=False):
        self.invocations = []
        self.image = image

    def specs(self):
        return [NativeLoopToolSpec("reqpilot_read", "Read a source", {
            "type": "object", "required": ["path"], "additionalProperties": False,
            "properties": {"path": {"type": "string"}},
        })]

    def invoke(self, name, args):
        self.invocations.append((name, args))
        items = [{"type": "inputText", "text": "Source evidence"}]
        if self.image:
            items.append({"type": "inputImage", "imageUrl": "data:image/png;base64,iVBORw0KGgo="})
        return {"success": True, "content_items": items}


class ProviderToolLoopTests(TestCase):
    def _run(self, root, provider, responses, host=None, on_call=None, **kwargs):
        fake = FakeCompletions(responses, on_call=on_call)
        host = host or ToolHost()
        skill = root / "SKILL.md"
        skill.write_text("Registered extraction skill", encoding="utf-8")
        driver = ProviderToolLoopDriver(provider, "synthetic-model", fake.client)
        result = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
            skill_paths={"reqpilot": skill}, tool_host=host,
            binding={"task_id": "synthetic-task", "source_sha256": "a" * 64},
            **kwargs)
        return result, fake, host, driver, skill

    def test_both_providers_share_persistent_tool_contract(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                result, fake, host, driver, skill = self._run(
                    root, provider, [tool_choice(), final_choice()])
                self.assertEqual(result.status, "COMPLETED")
                self.assertEqual(result.receipt["adapter_id"], ADAPTER_ID)
                self.assertEqual(result.receipt["transport_kind"], TRANSPORT_KIND)
                self.assertEqual(result.receipt["identity"]["provider"], provider)
                self.assertEqual(result.receipt["tools"][0]["name"], "reqpilot_read")
                self.assertEqual(len(host.invocations), 1)
                self.assertEqual(len(fake.calls), 2)
                self.assertEqual(fake.calls[1]["messages"][-1]["role"], "tool")
                self.assertTrue(result.receipt["local_state_persisted"])
                snapshot = json.loads((root / "runtime" / result.receipt["run_state_reference"]).read_text())
                self.assertEqual(_digest(snapshot), result.receipt["run_state_sha256"])
                state = json.loads((root / "runtime/run-state.json").read_text())
                self.assertEqual(state["thread_id"], result.receipt["thread_id"])
                self.assertNotIn("api_key", json.dumps(result.receipt))
                self.assertNotIn("secret", json.dumps(result.receipt))
                followup = FakeCompletions([final_choice("Reviewed")])
                driver.client_factory = followup.client
                continued = driver.run(session_dir=root / "runtime", goal="Review latest feedback",
                    skill_paths={"reqpilot": skill}, tool_host=host,
                    binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                self.assertEqual(continued.status, "COMPLETED")
                self.assertEqual(continued.receipt["thread_id"], result.receipt["thread_id"])
                self.assertTrue(continued.receipt["resumed"])
                self.assertEqual(len(host.invocations), 1)

    def test_interrupted_pending_tool_resumes_without_duplicate_model_turn(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            controls = {"cancel": False}
            result, fake, host, driver, skill = self._run(
                root, "aliyun", [tool_choice()],
                cancellation_requested=lambda: controls["cancel"],
                on_call=lambda: controls.update(cancel=True))
            self.assertEqual(result.status, "INTERRUPTED")
            self.assertEqual(len(host.invocations), 0)
            resumed_model = FakeCompletions([final_choice()])
            driver.client_factory = resumed_model.client
            resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                skill_paths={"reqpilot": skill}, tool_host=host,
                binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
            self.assertEqual(resumed.status, "COMPLETED")
            self.assertEqual(len(host.invocations), 1)
            self.assertEqual(len(resumed_model.calls), 1)
            self.assertEqual(resumed_model.calls[0]["messages"][-1]["role"], "tool")

    def test_unknown_tool_outcome_never_replays_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            controls = {"cancel": False}
            result, _, host, driver, skill = self._run(
                root, "azure", [tool_choice()],
                cancellation_requested=lambda: controls["cancel"],
                on_call=lambda: controls.update(cancel=True))
            thread = result.receipt["thread_id"]
            identity = {"thread_id": thread, "turn_id": result.receipt["turn_id"],
                        "call_id": "call-1", "name": "reqpilot_read",
                        "arguments_sha256": _digest({"path": "source"})}
            receipt_path = root / "runtime/calls" / (hashlib.sha256((thread + ":" + result.receipt["turn_id"] + ":call-1").encode()).hexdigest() + ".json")
            receipt_path.write_text(json.dumps({"identity": identity, "status": "INVOKING"}))
            model = FakeCompletions([final_choice()])
            driver.client_factory = model.client
            with self.assertRaises(ProviderToolLoopError) as raised:
                driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                    skill_paths={"reqpilot": skill}, tool_host=host,
                    binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
            self.assertEqual(raised.exception.runtime_receipt["status"], "UNKNOWN")
            self.assertEqual(raised.exception.runtime_receipt["reason_code"],
                             "PROVIDER_LOOP_TOOL_OUTCOME_UNKNOWN")
            self.assertEqual(host.invocations, [])
            self.assertEqual(model.calls, [])

    def test_cancel_before_first_request_never_creates_client(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                result, model, host, _, _ = self._run(
                    Path(directory), provider, [final_choice()], cancellation_requested=lambda: True)
                self.assertEqual(result.status, "INTERRUPTED")
                self.assertEqual(result.receipt["reason_code"], "PROVIDER_LOOP_CANCELLED")
                self.assertIsNone(result.final_message)
                self.assertEqual(model.calls, [])
                self.assertEqual(model.options, [])
                self.assertEqual(host.invocations, [])

    def test_cancel_during_response_retains_safe_resume_without_completion(self):
        for provider in ("azure", "aliyun"):
            for response in (final_choice(), tool_choice()):
                with self.subTest(provider=provider, response=response), TemporaryDirectory() as directory:
                    root = Path(directory)
                    controls = {"cancel": False}
                    result, model, host, driver, skill = self._run(
                        root, provider, [response], on_call=lambda: controls.update(cancel=True),
                        cancellation_requested=lambda: controls["cancel"])
                    self.assertEqual(result.status, "INTERRUPTED")
                    self.assertIsNone(result.final_message)
                    self.assertEqual(len(model.calls), 1)
                    self.assertEqual(model.options, [{"max_retries": 0}])
                    self.assertEqual(host.invocations, [])
                    with self.assertRaises(ValueError):
                        _completed_provider_result(root / "runtime")
                    resumed_model = FakeCompletions([final_choice("Resumed")])
                    driver.client_factory = resumed_model.client
                    resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                        skill_paths={"reqpilot": skill}, tool_host=host,
                        binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                    self.assertEqual(resumed.status, "COMPLETED")
                    self.assertEqual(len(resumed_model.calls), 1 if response == tool_choice() else 0)
                    self.assertEqual(len(host.invocations), 1 if response == tool_choice() else 0)

    def test_deadline_during_response_never_completes_or_invokes_tools(self):
        for provider in ("azure", "aliyun"):
            for response in (final_choice(), tool_choice()):
                with self.subTest(provider=provider, response=response), TemporaryDirectory() as directory:
                    root = Path(directory)
                    clock = {"now": 0.0}
                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                               side_effect=lambda: clock["now"]):
                        result, model, host, _, _ = self._run(
                            root, provider, [response], timeout_seconds=10,
                            on_call=lambda: clock.update(now=11.0))
                    self.assertEqual(result.status, "FAILED")
                    self.assertEqual(result.receipt["reason_code"], "PROVIDER_LOOP_TIMEOUT")
                    self.assertIsNone(result.final_message)
                    self.assertEqual(model.calls[0]["timeout"], 10.0)
                    self.assertEqual(len(model.calls), 1)
                    self.assertEqual(host.invocations, [])
                    with self.assertRaises(ValueError):
                        _completed_provider_result(root / "runtime")

    def test_timed_out_terminal_response_requires_fresh_completion_on_resume(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                clock = {"now": 0.0}
                with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                           side_effect=lambda: clock["now"]):
                    result, _, host, driver, skill = self._run(
                        root, provider, [final_choice("Expired response")], timeout_seconds=10,
                        on_call=lambda: clock.update(now=11.0))
                self.assertEqual(result.status, "FAILED")
                model = FakeCompletions([final_choice("Fresh completion")])
                driver.client_factory = model.client
                resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                    skill_paths={"reqpilot": skill}, tool_host=host,
                    binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                self.assertEqual(resumed.status, "COMPLETED")
                self.assertEqual(resumed.final_message, "Fresh completion")
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(host.invocations, [])

    def test_saved_terminal_response_obeys_cancel_and_deadline_on_resume(self):
        for provider in ("azure", "aliyun"):
            for control in ("cancel", "deadline"):
                with self.subTest(provider=provider, control=control), TemporaryDirectory() as directory:
                    root = Path(directory)
                    first_controls = {"cancel": False}
                    result, _, host, driver, skill = self._run(
                        root, provider, [final_choice()],
                        on_call=lambda: first_controls.update(cancel=True),
                        cancellation_requested=lambda: first_controls["cancel"])
                    self.assertEqual(result.status, "INTERRUPTED")
                    clock = {"now": 0.0}
                    fresh_model = FakeCompletions([final_choice()])
                    driver.client_factory = fresh_model.client

                    def cancellation():
                        if control == "deadline":
                            clock["now"] = 11.0
                        return control == "cancel"

                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                               side_effect=lambda: clock["now"]):
                        resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                            skill_paths={"reqpilot": skill}, tool_host=host,
                            binding={"task_id": "synthetic-task", "source_sha256": "a" * 64},
                            timeout_seconds=10, cancellation_requested=cancellation)
                    self.assertEqual(resumed.status, "INTERRUPTED" if control == "cancel" else "FAILED")
                    self.assertIsNone(resumed.final_message)
                    self.assertEqual(fresh_model.calls, [])
                    self.assertEqual(fresh_model.options, [])
                    with self.assertRaises(ValueError):
                        _completed_provider_result(root / "runtime")

    def test_control_between_tools_preserves_returned_call_and_stops_batch(self):
        for provider in ("azure", "aliyun"):
            for control in ("cancel", "deadline"):
                with self.subTest(provider=provider, control=control), TemporaryDirectory() as directory:
                    root = Path(directory)
                    controls = {"cancel": False, "now": 0.0}
                    host = ToolHost()
                    invoke = host.invoke

                    def controlled_invoke(name, args):
                        result = invoke(name, args)
                        controls["cancel"] = control == "cancel"
                        controls["now"] = 11.0 if control == "deadline" else 0.0
                        return result

                    host.invoke = controlled_invoke
                    response = tool_choice()
                    response["choices"][0]["message"]["tool_calls"].append({
                        "id": "call-2", "type": "function", "function": {
                            "name": "reqpilot_read", "arguments": '{"path":"adjacent"}'}})
                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                               side_effect=lambda: controls["now"]):
                        result, model, _, driver, skill = self._run(
                            root, provider, [response], host=host, timeout_seconds=10,
                            cancellation_requested=lambda: controls["cancel"])
                    self.assertEqual(result.status, "INTERRUPTED" if control == "cancel" else "FAILED")
                    self.assertEqual(len(model.calls), 1)
                    self.assertEqual(len(host.invocations), 1)
                    self.assertEqual(len(result.receipt["tools"]), 1)
                    host.invoke = invoke
                    model = FakeCompletions([final_choice()])
                    driver.client_factory = model.client
                    resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                        skill_paths={"reqpilot": skill}, tool_host=host,
                        binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                    self.assertEqual(resumed.status, "COMPLETED")
                    self.assertEqual(host.invocations, [
                        ("reqpilot_read", {"path": "source"}), ("reqpilot_read", {"path": "adjacent"})])

    def test_deadline_expiring_during_client_setup_blocks_request(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                controls = {"cancel": False, "now": 0.0}
                result, _, host, driver, skill = self._run(
                    root, provider, [final_choice()],
                    on_call=lambda: controls.update(cancel=True),
                    cancellation_requested=lambda: controls["cancel"])
                # A new goal deliberately requires a fresh request.
                model = FakeCompletions([final_choice()])

                def factory():
                    controls["now"] = 11.0
                    return model.client()

                driver.client_factory = factory
                with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                           side_effect=lambda: controls["now"]):
                    resumed = driver.run(session_dir=root / "runtime", goal="New authorized feedback",
                        skill_paths={"reqpilot": skill}, tool_host=host,
                        binding={"task_id": "synthetic-task", "source_sha256": "a" * 64}, timeout_seconds=10)
                self.assertEqual(resumed.status, "FAILED")
                self.assertEqual(model.calls, [])

    def test_transport_error_after_control_uses_control_terminal_status(self):
        for provider in ("azure", "aliyun"):
            for control in ("cancel", "deadline"):
                with self.subTest(provider=provider, control=control), TemporaryDirectory() as directory:
                    controls = {"cancel": False, "now": 0.0}

                    def on_call():
                        controls["cancel"] = control == "cancel"
                        controls["now"] = 11.0 if control == "deadline" else 0.0

                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                               side_effect=lambda: controls["now"]):
                        result, model, host, _, _ = self._run(
                            Path(directory), provider, [RuntimeError("secret provider detail")],
                            on_call=on_call, cancellation_requested=lambda: controls["cancel"], timeout_seconds=10)
                    self.assertEqual(result.status, "INTERRUPTED" if control == "cancel" else "FAILED")
                    self.assertEqual(result.receipt["transport_error_code"], "PROVIDER_LOOP_RUNTIMEERROR")
                    self.assertNotIn("secret provider detail", json.dumps(result.receipt))
                    self.assertEqual(len(model.calls), 1)
                    self.assertEqual(host.invocations, [])

    def test_host_exception_is_unknown_and_cannot_replay_under_new_call_id(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                host = ToolHost()

                def uncertain_invoke(name, args):
                    host.invocations.append((name, args))
                    raise RuntimeError("secret detail after mutation")

                host.invoke = uncertain_invoke
                with self.assertRaises(ProviderToolLoopError) as first:
                    self._run(root, provider, [tool_choice(), final_choice()], host=host)
                self.assertEqual(first.exception.runtime_receipt["status"], "UNKNOWN")
                self.assertEqual(first.exception.runtime_receipt["reason_code"], "PROVIDER_LOOP_TOOL_OUTCOME_UNKNOWN")
                self.assertNotIn("secret detail", str(first.exception))
                saved_call = json.loads(next((root / "runtime/calls").glob("*.json")).read_text())
                self.assertEqual(saved_call["status"], "INVOKING")
                model = FakeCompletions([tool_choice(call_id="new-id"), final_choice()])
                driver = ProviderToolLoopDriver(provider, "synthetic-model", model.client)
                with self.assertRaises(ProviderToolLoopError) as resumed:
                    driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                        skill_paths={"reqpilot": root / "SKILL.md"}, tool_host=host,
                        binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                self.assertEqual(resumed.exception.runtime_receipt["status"], "UNKNOWN")
                self.assertEqual(len(host.invocations), 1)
                self.assertEqual(model.calls, [])
                self.assertEqual(model.options, [])

    def test_tool_started_callback_control_stops_before_host_invocation(self):
        for provider in ("azure", "aliyun"):
            for control in ("cancel", "deadline"):
                with self.subTest(provider=provider, control=control), TemporaryDirectory() as directory:
                    root = Path(directory)
                    controls = {"cancel": False, "now": 0.0}

                    def event_sink(event):
                        if event["type"] == "tool_started":
                            controls["cancel"] = control == "cancel"
                            controls["now"] = 11.0 if control == "deadline" else 0.0

                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                               side_effect=lambda: controls["now"]):
                        result, model, host, driver, skill = self._run(
                            root, provider, [tool_choice()], event_sink=event_sink,
                            cancellation_requested=lambda: controls["cancel"], timeout_seconds=10)
                    self.assertEqual(result.status, "INTERRUPTED" if control == "cancel" else "FAILED")
                    self.assertEqual(host.invocations, [])
                    self.assertEqual(len(model.calls), 1)
                    self.assertEqual(list((root / "runtime/calls").glob("*.json")), [])
                    model = FakeCompletions([final_choice()])
                    driver.client_factory = model.client
                    resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                        skill_paths={"reqpilot": skill}, tool_host=host,
                        binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                    self.assertEqual(resumed.status, "COMPLETED")
                    self.assertEqual(len(host.invocations), 1)

    def test_unsealed_returned_mutation_stays_unknown_even_after_control(self):
        for provider in ("azure", "aliyun"):
            for control in ("cancel", "deadline"):
                with self.subTest(provider=provider, control=control), TemporaryDirectory() as directory:
                    root = Path(directory)
                    controls = {"cancel": False, "now": 0.0}
                    host = ToolHost()
                    invoke = host.invoke

                    def controlled_invoke(name, args):
                        result = invoke(name, args)
                        controls["cancel"] = control == "cancel"
                        controls["now"] = 11.0 if control == "deadline" else 0.0
                        return result

                    def fail_returned(path, value):
                        if path.parent.name == "calls" and value.get("status") == "RETURNED":
                            raise OSError("synthetic receipt-write failure")
                        _write_json(path, value)

                    host.invoke = controlled_invoke
                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                               side_effect=lambda: controls["now"]), \
                         patch("reqpilotx_server.adapters.agents.provider_tool_loop._write_json",
                               side_effect=fail_returned):
                        with self.assertRaises(ProviderToolLoopError) as raised:
                            self._run(root, provider, [tool_choice()], host=host,
                                cancellation_requested=lambda: controls["cancel"], timeout_seconds=10)
                    self.assertEqual(raised.exception.runtime_receipt["status"], "UNKNOWN")
                    self.assertEqual(raised.exception.runtime_receipt["reason_code"],
                                     "PROVIDER_LOOP_TOOL_OUTCOME_UNKNOWN")
                    saved_call = json.loads(next((root / "runtime/calls").glob("*.json")).read_text())
                    self.assertEqual(saved_call["status"], "INVOKING")
                    model = FakeCompletions([final_choice()])
                    driver = ProviderToolLoopDriver(provider, "synthetic-model", model.client)
                    with self.assertRaises(ProviderToolLoopError) as resumed:
                        driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                            skill_paths={"reqpilot": root / "SKILL.md"}, tool_host=host,
                            binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                    self.assertEqual(resumed.exception.runtime_receipt["status"], "UNKNOWN")
                    self.assertEqual(len(host.invocations), 1)
                    self.assertEqual(model.calls, [])

    def test_control_in_last_tool_step_takes_precedence_over_step_limit(self):
        for provider in ("azure", "aliyun"):
            for control in ("cancel", "deadline"):
                with self.subTest(provider=provider, control=control), TemporaryDirectory() as directory:
                    root = Path(directory)
                    controls = {"cancel": False, "now": 0.0}
                    host = ToolHost()
                    invoke = host.invoke

                    def controlled_invoke(name, args):
                        result = invoke(name, args)
                        controls["cancel"] = control == "cancel"
                        controls["now"] = 11.0 if control == "deadline" else 0.0
                        return result

                    host.invoke = controlled_invoke
                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic",
                               side_effect=lambda: controls["now"]), \
                         patch("reqpilotx_server.adapters.agents.provider_tool_loop._MAX_TOOL_STEPS", 2):
                        result, model, _, driver, skill = self._run(
                            root, provider, [tool_choice()], host=host,
                            cancellation_requested=lambda: controls["cancel"], timeout_seconds=10)
                    self.assertEqual(result.status, "INTERRUPTED" if control == "cancel" else "FAILED")
                    self.assertEqual(result.receipt["reason_code"],
                                     "PROVIDER_LOOP_CANCELLED" if control == "cancel" else "PROVIDER_LOOP_TIMEOUT")
                    saved_call = json.loads(next((root / "runtime/calls").glob("*.json")).read_text())
                    self.assertEqual(saved_call["status"], "RETURNED")
                    self.assertEqual(len(model.calls), 1)
                    host.invoke = invoke
                    model = FakeCompletions([final_choice()])
                    driver.client_factory = model.client
                    resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                        skill_paths={"reqpilot": skill}, tool_host=host,
                        binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
                    self.assertEqual(resumed.status, "COMPLETED")
                    self.assertEqual(len(host.invocations), 1)
                    self.assertEqual(len(model.calls), 1)

    def test_failed_control_read_is_redacted_unknown_at_request_and_final_sealing(self):
        for provider in ("azure", "aliyun"):
            for boundary in ("initial", "final", "transport_error"):
                with self.subTest(provider=provider, boundary=boundary), TemporaryDirectory() as directory:
                    root = Path(directory)
                    controls = {"fail": boundary == "initial", "reads": 0}

                    def cancellation():
                        controls["reads"] += 1
                        if controls["fail"]:
                            raise RuntimeError("synthetic-sensitive-control-detail")
                        return False

                    def event_sink(event):
                        if boundary == "final" and event["type"] == "agent_message":
                            controls["fail"] = True

                    def on_call():
                        if boundary == "transport_error":
                            controls["fail"] = True

                    response = RuntimeError("synthetic-sensitive-provider-detail") if boundary == "transport_error" else final_choice()
                    with self.assertRaises(ProviderToolLoopError) as raised:
                        self._run(root, provider, [response], cancellation_requested=cancellation,
                            event_sink=event_sink, on_call=on_call)
                    receipt = raised.exception.runtime_receipt
                    self.assertEqual(receipt["status"], "UNKNOWN")
                    self.assertEqual(receipt["reason_code"], "PROVIDER_LOOP_CONTROL_UNAVAILABLE")
                    self.assertNotIn("synthetic-sensitive", str(raised.exception))
                    self.assertNotIn("synthetic-sensitive", json.dumps(receipt))
                    if boundary == "initial":
                        self.assertEqual(controls["reads"], 1)  # Never read a failed callback twice.
                    state = json.loads((root / "runtime/run-state.json").read_text())
                    self.assertEqual(state["status"], "UNKNOWN")
                    saved_receipt = json.loads((root / "runtime" / ("receipt-" + receipt["attempt_id"] + ".json")).read_text())
                    self.assertEqual(saved_receipt["status"], "UNKNOWN")
                    with self.assertRaises(ValueError):
                        _completed_provider_result(root / "runtime")

    def test_known_tool_failures_keep_normal_response_contract(self):
        for provider in ("azure", "aliyun"):
            for failure in ("schema", "host_response"):
                with self.subTest(provider=provider, failure=failure), TemporaryDirectory() as directory:
                    host = ToolHost()
                    if failure == "host_response":
                        def known_failure(name, args):
                            host.invocations.append((name, args))
                            return {"success": False, "content_items": [
                                {"type": "inputText", "text": "Known safe validation failure"}]}
                        host.invoke = known_failure
                    response = tool_choice(arguments='{}') if failure == "schema" else tool_choice()
                    result, model, _, _, _ = self._run(
                        Path(directory), provider, [response, final_choice()], host=host)
                    self.assertEqual(result.status, "COMPLETED")
                    self.assertEqual(len(model.calls), 2)
                    self.assertFalse(result.receipt["tools"][0]["success"])
                    self.assertEqual(len(host.invocations), 0 if failure == "schema" else 1)
                    tool_result = json.loads(model.calls[-1]["messages"][-1]["content"])
                    self.assertFalse(tool_result["success"])

    def test_last_completion_boundary_rechecks_control(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                controls = {"cancel": False}

                def event_sink(event):
                    if event["type"] == "agent_message":
                        controls["cancel"] = True

                result, model, _, _, _ = self._run(
                    root, provider, [final_choice()], event_sink=event_sink,
                    cancellation_requested=lambda: controls["cancel"])
                self.assertEqual(result.status, "INTERRUPTED")
                self.assertIsNone(result.final_message)
                self.assertEqual(len(model.calls), 1)
                with self.assertRaises(ValueError):
                    _completed_provider_result(root / "runtime")

    def test_real_sdk_disables_retries_bounds_timeout_and_keeps_shared_client_open(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                requests = []
                fail = {"enabled": True}

                def handle(request):
                    requests.append(request)
                    if fail["enabled"]:
                        return httpx.Response(429, json={"error": {"message": "synthetic throttle", "type": "rate_limit_error"}})
                    return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion",
                        "created": 0, "model": "synthetic-model", "choices": final_choice()["choices"]})

                http_client = httpx.Client(transport=httpx.MockTransport(handle))
                if provider == "azure":
                    client = AzureOpenAI(api_key="synthetic-not-a-credential", api_version="2024-10-21",
                        azure_endpoint="https://synthetic.invalid", max_retries=3, timeout=600, http_client=http_client)
                else:
                    client = OpenAI(api_key="synthetic-not-a-credential", base_url="https://synthetic.invalid/v1",
                        max_retries=3, timeout=600, http_client=http_client)
                try:
                    skill = root / "SKILL.md"
                    skill.write_text("Synthetic extraction skill")
                    driver = ProviderToolLoopDriver(provider, "synthetic-model", lambda: client)
                    with patch("reqpilotx_server.adapters.agents.provider_tool_loop.time.monotonic", return_value=0):
                        with self.assertRaises(ProviderToolLoopError) as raised:
                            driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                                skill_paths={"reqpilot": skill}, tool_host=ToolHost(),
                                binding={"task_id": "synthetic-task"}, timeout_seconds=10)
                    self.assertEqual(raised.exception.runtime_receipt["status"], "UNKNOWN")
                    self.assertEqual(len(requests), 1)  # 429 must not invoke an SDK retry.
                    self.assertEqual(requests[0].extensions["timeout"],
                                     {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0})
                    self.assertEqual(client.max_retries, 3)
                    self.assertFalse(client.is_closed())
                    self.assertFalse(http_client.is_closed)
                    fail["enabled"] = False
                    resumed = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                        skill_paths={"reqpilot": skill}, tool_host=ToolHost(),
                        binding={"task_id": "synthetic-task"})
                    self.assertEqual(resumed.status, "COMPLETED")
                    self.assertEqual(len(requests), 2)
                    self.assertFalse(client.is_closed())
                finally:
                    client.close()  # This test owns the SDK/HTTP transport.

    def test_image_result_is_sent_as_multimodal_source_context(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, model, host, _, _ = self._run(
                root, "aliyun", [tool_choice(), final_choice()], ToolHost(image=True))
            self.assertEqual(result.status, "COMPLETED")
            messages = model.calls[1]["messages"]
            self.assertEqual(messages[-2]["role"], "tool")
            self.assertEqual(messages[-1]["content"][1]["type"], "image_url")

    def test_parallel_model_tool_calls_are_serialized_before_image_context(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = tool_choice()["choices"][0]
            first["message"]["tool_calls"].append({
                "id": "call-2", "type": "function", "function": {
                    "name": "reqpilot_read", "arguments": '{"path":"adjacent"}'}})
            result, model, host, _, _ = self._run(
                root, "aliyun", [{"choices": [first]}, final_choice()], ToolHost(image=True))
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(len(host.invocations), 2)
            self.assertEqual([row["role"] for row in model.calls[1]["messages"][-4:]],
                             ["assistant", "tool", "tool", "user"])

    def test_real_registered_pdf_read_has_source_bound_page_receipt(self):
        import pymupdf
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            source = workspace / "source.pdf"
            pdf = pymupdf.open()
            page = pdf.new_page()
            page.insert_text((72, 72), "Synthetic source only")
            pdf.save(source)
            pdf.close()
            plugin = Path(__file__).resolve().parents[2] / "plugins/reqpilot"
            host = NativeLoopToolHost(workspace=workspace, plugin_root=plugin)
            model = FakeCompletions([
                tool_choice(arguments='{"path":"source.pdf","page":1}'), final_choice(),
            ])
            driver = ProviderToolLoopDriver("aliyun", "synthetic-model", model.client)
            result = driver.run(session_dir=root / "runtime", goal="Read synthetic page",
                skill_paths={"reqpilot": plugin / "skills/reqpilot/SKILL.md"},
                tool_host=host, binding={"task_id": "synthetic-task",
                                          "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
            self.assertEqual(result.status, "COMPLETED")
            receipts = document_jobs._source_page_return_receipts(
                root, hashlib.sha256(source.read_bytes()).hexdigest(),
                result.receipt["thread_id"])
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["page"], 1)

    def test_settings_require_explicit_provider_and_no_codex_binary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"REQPILOT_ENV": "test", "REQPILOT_AGENT_RUNTIME": "provider-tool-loop",
                   "REQPILOT_AGENT_PROVIDER": "aliyun", "REQPILOT_OBJECT_ROOT": str(root)}
            settings = RuntimeSettings.from_environment(environment=env, prototype_dir=root)
            self.assertEqual(settings.agent_runtime_backend, AgentRuntimeBackend.PROVIDER_TOOL_LOOP)
            self.assertEqual(settings.agent_provider, AgentProvider.ALIYUN)
            self.assertIsNone(settings.codex_bin)
            self.assertEqual(settings.agent_model_id, "")
            with self.assertRaises(RuntimeConfigurationError):
                RuntimeSettings.from_environment(environment={**env, "REQPILOT_AGENT_PROVIDER": ""}, prototype_dir=root)

    def test_web_source_document_job_selects_provider_driver(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(agent_runtime_backend=AgentRuntimeBackend.PROVIDER_TOOL_LOOP,
                                       agent_model_id="", agent_provider=AgentProvider.ALIYUN)
            container = SimpleNamespace(settings=settings)
            meta = {"latest_job_id": "job", "task_id": "task", "project_id": "p",
                    "document_id": "d", "source_sha256": "a" * 64, "request": {}}
            job = SimpleNamespace(job_id="job", aggregate_id="d", payload={
                "project_id": "p", "task_id": "task", "application_entrypoint": document_jobs.ENTRYPOINT,
                "phase": "INITIAL_SOURCE", "goal": "Read original", "origin": "USER_REQUEST"})
            with patch.object(document_jobs, "_raw_existing", return_value=(container, root, meta)), \
                 patch("app.application.agent_workflow.native_loop_capabilities.native_loop_plugin_for_job", return_value=root / "plugin"), \
                 patch.object(document_jobs, "SourceDocumentToolHost") as host, \
                 patch.object(document_jobs, "build_provider_tool_loop_driver") as factory:
                from app.application.agent_workflow.native_task_brief import build_task_brief
                host.return_value.task_brief.return_value = build_task_brief(
                    goal="Read original", origin="USER_REQUEST", require_map_confirmation=False)
                factory.return_value.run.return_value = SimpleNamespace(status="COMPLETED", receipt={}, final_message="Ready")
                result = document_jobs.run_durable_source_document_loop(job, Mock())
            self.assertTrue(factory.called)
            self.assertEqual(result["status"], "NEEDS_CONTINUATION")
            self.assertFalse(result["application_saved"])

    def test_web_source_item_review_selects_same_provider_driver(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(agent_runtime_backend=AgentRuntimeBackend.PROVIDER_TOOL_LOOP,
                                       agent_model_id="", agent_provider=AgentProvider.ALIYUN)
            container = SimpleNamespace(settings=settings)
            meta = {"latest_job_id": "job", "task_id": "task", "project_id": "p",
                    "document_id": "d", "source_sha256": "a" * 64,
                    "document_map_sha256": "b" * 64, "request": {}}
            job = SimpleNamespace(job_id="job", aggregate_id="d", payload={
                "project_id": "p", "task_id": "task", "application_entrypoint": item_jobs.ENTRYPOINT,
                "goal": "Review", "origin": "USER_REQUEST"})
            with patch.object(item_jobs, "_existing", return_value=(container, root, meta)), \
                 patch("app.application.agent_workflow.native_loop_capabilities.native_loop_plugin_for_job", return_value=root / "plugin"), \
                 patch.object(item_jobs, "NativeLoopToolHost") as host, \
                 patch.object(item_jobs, "build_provider_tool_loop_driver") as factory:
                from app.application.agent_workflow.native_task_brief import build_task_brief
                host.return_value.task_brief.return_value = build_task_brief(
                    goal="Review", origin="USER_REQUEST", require_map_confirmation=False)
                host.return_value.workspace = root / "review"
                factory.return_value.run.return_value = SimpleNamespace(status="COMPLETED", receipt={}, final_message="Ready")
                result = item_jobs.run_durable_source_item_loop(job, Mock())
            self.assertTrue(factory.called)
            self.assertEqual(result["status"], "NEEDS_CONTINUATION")

    def test_configured_clients_are_selected_without_exposing_credentials(self):
        azure_profile = SimpleNamespace(
            azure_openai_chat_ready=True, azure_openai_chat_deployment="configured-azure",
            azure_openai_endpoint="https://example.openai.azure.com", secret="never-print")
        aliyun_profile = SimpleNamespace(
            dashscope_chat_ready=True, chat_model="configured-qwen",
            screening_model="configured-screening", dashscope_api_key="never-print")
        with patch("reqpilotx_server.services.ai_copilot.load_ai_copilot_config",
                   side_effect=lambda name: azure_profile if name == "azure" else aliyun_profile), \
             patch("reqpilotx_server.services.ai_copilot._azure_openai_client", return_value="azure-client") as azure, \
             patch("reqpilotx_server.services.ai_copilot._aliyun_openai_client", return_value="aliyun-client") as aliyun:
            for provider, expected_model, expected_client in (
                (AgentProvider.AZURE, "configured-azure", "azure-client"),
                (AgentProvider.ALIYUN, "configured-qwen", "aliyun-client"),
            ):
                driver = build_provider_tool_loop_driver(SimpleNamespace(
                    agent_provider=provider, agent_model_id=""))
                self.assertEqual(driver.model_id, expected_model)
                self.assertEqual(driver.client_factory(), expected_client)
                self.assertNotIn("never-print", repr(driver))
        azure.assert_called_once()
        aliyun.assert_called_once()

    def test_completed_provider_publication_can_finalize_without_model_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review = root / "review"
            candidate = review / "versions/v1"
            delivery = review / "delivery/v1"
            candidate.mkdir(parents=True)
            delivery.mkdir(parents=True)
            file_bytes, xlsx_bytes = b"candidate", b"synthetic Excel bytes"
            (candidate / "item.json").write_bytes(file_bytes)
            (delivery / "matrix.xlsx").write_bytes(xlsx_bytes)
            baseline_sha, source_sha, review_sha = "b" * 64, "a" * 64, "c" * 64
            (delivery / "delivery-receipt.json").write_text(json.dumps({
                "xlsx_sha256": hashlib.sha256(xlsx_bytes).hexdigest(),
                "baseline_artifact_sha256": baseline_sha, "source_sha256": source_sha,
            }))
            (review / "input.json").write_text(json.dumps({
                "delivery_path": "delivery/v1", "candidate_path": "versions/v1"}))
            (review / "workspace-manifest.json").write_text(json.dumps({
                "review_sha256": review_sha,
                "current": {"version_path": "versions/v1", "baseline_sha256": baseline_sha,
                            "files": {"item.json": hashlib.sha256(file_bytes).hexdigest()}},
                "delivery": {"path": "delivery/v1", "xlsx_sha256": hashlib.sha256(xlsx_bytes).hexdigest()},
            }))
            (root / "controls.json").write_text(json.dumps({"steering": []}))

            class PublicationHost:
                def specs(self):
                    return [NativeLoopToolSpec(name, name, {"type": "object"})
                            for name in ("reqpilot_run", "reqpilot_publish")]

                def invoke(self, name, args):
                    if name == "reqpilot_publish":
                        (root / "publish-request-job.json").write_text(json.dumps({
                            "review_sha256": review_sha, "job_id": "job"}))
                        output = {"status": "PUBLICATION_REQUEST_RECORDED"}
                    elif args["operation"] == "review-close":
                        output = {"status": "REVIEW_ADVANCED", "review_sha256": review_sha,
                                  "current_version_path": "versions/v1", "baseline_sha256": baseline_sha}
                    else:
                        output = {"status": "DRAFT_EXPORTED"}
                    return {"success": True, "content_items": [{"type": "inputText", "text": json.dumps(output)}]}

            tool_calls = [
                ("close", "reqpilot_run", {"operation": "review-close", "arguments": {"input": "input.json"}}),
                ("deliver", "reqpilot_run", {"operation": "deliver", "arguments": {
                    "output_dir": "delivery/v1", "baseline_dir": "versions/v1"}}),
                ("publish", "reqpilot_publish", {"review_sha256": review_sha}),
            ]
            completion = {"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [
                    {"id": call_id, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args)}}
                    for call_id, name, args in tool_calls]}}]}
            model = FakeCompletions([completion, final_choice("Published")])
            skill = root / "SKILL.md"
            skill.write_text("Synthetic skill")
            driver = ProviderToolLoopDriver("azure", "synthetic-model", model.client)
            run = driver.run(session_dir=root / "runtime", goal="Publish synthetic review",
                skill_paths={"reqpilot": skill}, tool_host=PublicationHost(),
                binding={"task_id": "task", "project_id": "p", "document_id": "d",
                         "source_sha256": source_sha, "document_map_sha256": "d" * 64})
            self.assertEqual(run.status, "COMPLETED")
            job = SimpleNamespace(job_id="job", payload={"task_id": "task",
                "source_sha256": source_sha, "capability_epoch": None})
            meta = {"task_id": "task", "project_id": "p", "document_id": "d",
                    "source_sha256": source_sha, "document_map_sha256": "d" * 64,
                    "capability_epoch": None, "job_kind": "agent.source-item.loop"}
            recovered, request, proof = verified_finalization_inputs(root, meta, job)
            self.assertEqual(recovered.receipt["run_state_sha256"], run.receipt["run_state_sha256"])
            self.assertEqual(proof["successful_publish_call_id"], "publish")
            self.assertEqual(len(model.calls), 2)  # Host-only recovery never calls a provider.
            publish_call = next(path for path in (root / "runtime/calls").glob("*.json")
                                if json.loads(path.read_text())["identity"]["name"] == "reqpilot_publish")
            original_call = publish_call.read_bytes()
            ambiguous = json.loads(original_call)
            ambiguous["status"] = "INVOKING"
            publish_call.write_text(json.dumps(ambiguous))
            with self.assertRaises(ValueError):
                verified_finalization_inputs(root, meta, job)
            publish_call.write_bytes(original_call)
            state_path = root / "runtime/run-state.json"
            original_state = state_path.read_bytes()
            failed = json.loads(original_state)
            failed["status"] = "FAILED"
            state_path.write_text(json.dumps(failed))
            with self.assertRaises(ValueError):
                verified_finalization_inputs(root, meta, job)
            state_path.write_bytes(original_state)
            snapshot = root / "runtime" / run.receipt["run_state_reference"]
            original = snapshot.read_bytes()
            snapshot.write_bytes(original + b" ")
            # A JSON whitespace edit preserves the canonical object, so alter its content.
            value = json.loads(snapshot.read_text())
            value["final_message"] = "tampered"
            snapshot.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                verified_finalization_inputs(root, meta, job)
