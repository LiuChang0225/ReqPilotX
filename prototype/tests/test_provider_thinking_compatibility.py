"""Offline Qwen snapshot compatibility; all SDK traffic uses MockTransport."""
from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx
from openai import AzureOpenAI, OpenAI

from reqpilotx_server.adapters.agents.provider_tool_loop import (
    ProviderToolLoopDriver, ProviderToolLoopError, build_provider_tool_loop_driver,
)
from reqpilotx_server.adapters.agents.provider_structured_stage import (
    ProviderStructuredStageInvocation, ProviderStructuredStageTransport,
)
from reqpilotx_server.application.agent_workflow.native_loop_finalization import _completed_provider_result
from reqpilotx_server.infrastructure.runtime_settings import RuntimeConfigurationError, RuntimeSettings
from .test_provider_tool_loop import FakeCompletions, ToolHost, final_choice, tool_choice

MODEL = "qwen3.8-max-0902"
SYNTHETIC_AUTH = "synthetic-auth-never-a-real-key"


def reasoning_choice(call_id, reasoning):
    response = tool_choice(call_id)
    response["choices"][0]["message"]["reasoning_content"] = reasoning
    return response


class ProviderThinkingCompatibilityTests(TestCase):
    def _arguments(self, root, host=None, **controls):
        skill = root / "SKILL.md"
        skill.write_text("Synthetic extraction skill", encoding="utf-8")
        return dict(session_dir=root / "runtime", goal="Read synthetic source",
                    skill_paths={"reqpilot": skill}, tool_host=host or ToolHost(),
                    binding={"task_id": "synthetic-task"}, **controls)

    def _assert_redacted(self, root, *forbidden):
        for path in (root / "runtime").rglob("*"):
            if path.is_file() and path.suffix in {".json", ".jsonl"}:
                text = path.read_text(encoding="utf-8")
                for value in forbidden:
                    self.assertNotIn(value, text, str(path))

    def test_exact_snapshot_and_thinking_setting_reach_configured_builder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"REQPILOT_ENV": "test", "REQPILOT_AGENT_RUNTIME": "provider-tool-loop",
                   "REQPILOT_AGENT_PROVIDER": "aliyun", "REQPILOT_AGENT_MODEL_ID": MODEL,
                   "REQPILOT_OBJECT_ROOT": str(root)}
            profile = SimpleNamespace(dashscope_chat_ready=True, chat_model=MODEL,
                                      screening_model="configured-screening")
            with patch("reqpilotx_server.services.ai_copilot.load_ai_copilot_config", return_value=profile), \
                 patch("reqpilotx_server.services.ai_copilot._aliyun_openai_client", return_value="mock-client"):
                for configured, expected in ((None, True), ("1", True), ("0", False), ("false", False)):
                    values = dict(env)
                    if configured is not None:
                        values["REQPILOT_AGENT_ALIYUN_ENABLE_THINKING"] = configured
                    settings = RuntimeSettings.from_environment(environment=values, prototype_dir=root)
                    driver = build_provider_tool_loop_driver(settings)
                    self.assertEqual(driver.model_id, MODEL)
                    self.assertEqual(driver.aliyun_thinking_enabled, expected)
                    self.assertEqual(driver.client_factory(), "mock-client")
                    self.assertEqual(driver._request_options(), {"extra_body": {
                        "enable_thinking": expected, "preserve_thinking": expected}})
                with self.assertRaises(ValueError):
                    build_provider_tool_loop_driver(settings, model_id="qwen3.8-max-0902-typo")
            with self.assertRaises(RuntimeConfigurationError):
                RuntimeSettings.from_environment(environment={**env,
                    "REQPILOT_AGENT_ALIYUN_ENABLE_THINKING": "invalid"}, prototype_dir=root)

    def test_other_configured_models_keep_existing_parameters(self):
        driver = ProviderToolLoopDriver("aliyun", "qwen2.5-max", lambda: None)
        self.assertEqual(driver._request_options(), {})

    def test_real_sdk_two_tool_rounds_preserve_history_and_isolate_azure(self):
        for provider, thinking in (("aliyun", True), ("aliyun", False), ("azure", True)):
            with self.subTest(provider=provider, thinking=thinking), TemporaryDirectory() as directory:
                root, requests = Path(directory), []
                responses = [reasoning_choice("first", "Synthetic reasoning one"),
                             reasoning_choice("second", "Synthetic reasoning two"), final_choice()]

                def handle(request):
                    requests.append(json.loads(request.content))
                    response = responses.pop(0)
                    response["choices"][0]["index"] = 0
                    return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
                        "created": 0, "model": MODEL, **response})

                http_client = httpx.Client(transport=httpx.MockTransport(handle))
                client = (OpenAI(api_key=SYNTHETIC_AUTH, base_url="https://synthetic.invalid/v1", http_client=http_client)
                          if provider == "aliyun" else AzureOpenAI(api_key=SYNTHETIC_AUTH,
                              azure_endpoint="https://synthetic.invalid", api_version="2024-10-21", http_client=http_client))
                host = ToolHost()
                try:
                    result = ProviderToolLoopDriver(provider, MODEL, lambda: client,
                        aliyun_thinking_enabled=thinking).run(**self._arguments(root, host))
                    self.assertEqual(result.status, "COMPLETED")
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(len(host.invocations), 2)
                    for body in requests:
                        self.assertEqual(body["model"], MODEL)
                        if provider == "aliyun":
                            self.assertIs(body["enable_thinking"], thinking)
                            self.assertIs(body["preserve_thinking"], thinking)
                        else:
                            self.assertNotIn("enable_thinking", body)
                            self.assertNotIn("preserve_thinking", body)
                            self.assertNotIn("extra_body", body)
                        self.assertNotIn("max_completion_tokens", body)
                        self.assertNotIn("max_tokens", body)
                    history = requests[2]["messages"][2:]
                    self.assertEqual([item["role"] for item in history], ["assistant", "tool", "assistant", "tool"])
                    self.assertEqual([history[1]["tool_call_id"], history[3]["tool_call_id"]], ["first", "second"])
                    for item, expected in ((history[0], "Synthetic reasoning one"), (history[2], "Synthetic reasoning two")):
                        if provider == "aliyun" and thinking:
                            self.assertEqual(item["reasoning_content"], expected)
                            self.assertEqual(item["content"], "")
                        else:
                            self.assertNotIn("reasoning_content", item)
                    state = json.loads((root / "runtime/run-state.json").read_text())
                    self.assertEqual(state["messages"][2]["reasoning_content"], "Synthetic reasoning one")
                    self.assertNotIn("provider_request_count", state)
                    events = (root / "runtime/events.jsonl").read_text()
                    self.assertNotIn("Synthetic reasoning", events)
                    self._assert_redacted(root, SYNTHETIC_AUTH, "Authorization", "api_key")
                    self.assertFalse(client.is_closed())
                finally:
                    client.close()

    def test_cancel_resume_preserves_reasoning_and_tool_id(self):
        with TemporaryDirectory() as directory:
            root, controls, host = Path(directory), {"cancel": False}, ToolHost()
            model = FakeCompletions([reasoning_choice("retained-id", "Synthetic retained reasoning")],
                                    on_call=lambda: controls.update(cancel=True))
            driver = ProviderToolLoopDriver("aliyun", MODEL, model.client)
            arguments = self._arguments(root, host, cancellation_requested=lambda: controls["cancel"])
            interrupted = driver.run(**arguments)
            self.assertEqual(interrupted.status, "INTERRUPTED")
            self.assertEqual(host.invocations, [])
            controls["cancel"] = False
            followup = FakeCompletions([final_choice()])
            driver.client_factory = followup.client
            resumed = driver.run(**arguments)
            self.assertEqual(resumed.status, "COMPLETED")
            self.assertEqual(resumed.receipt["thread_id"], interrupted.receipt["thread_id"])
            self.assertEqual(len(host.invocations), 1)
            history = followup.calls[0]["messages"]
            self.assertEqual(history[-2]["reasoning_content"], "Synthetic retained reasoning")
            self.assertEqual(history[-1]["tool_call_id"], "retained-id")

    def test_legacy_history_without_reasoning_continues_without_inventing_it(self):
        with TemporaryDirectory() as directory:
            root, controls = Path(directory), {"cancel": False}
            model = FakeCompletions([tool_choice()], on_call=lambda: controls.update(cancel=True))
            driver = ProviderToolLoopDriver("aliyun", MODEL, model.client)
            arguments = self._arguments(root, cancellation_requested=lambda: controls["cancel"])
            driver.run(**arguments)
            controls["cancel"] = False
            followup = FakeCompletions([final_choice()])
            driver.client_factory = followup.client
            self.assertEqual(driver.run(**arguments).status, "COMPLETED")
            self.assertNotIn("reasoning_content", followup.calls[0]["messages"][-2])

    def test_length_never_executes_partial_or_valid_tools_or_seals_success(self):
        for provider in ("azure", "aliyun"):
            for response in (tool_choice(arguments='{"path":'), tool_choice(), final_choice("Partial final")):
                with self.subTest(provider=provider, response=response), TemporaryDirectory() as directory:
                    root, host = Path(directory), ToolHost()
                    response["choices"][0]["finish_reason"] = "length"
                    model = FakeCompletions([response, final_choice()])
                    result = ProviderToolLoopDriver(provider, MODEL, model.client).run(**self._arguments(root, host))
                    self.assertEqual(result.status, "FAILED")
                    self.assertEqual(result.receipt["reason_code"], "PROVIDER_LOOP_OUTPUT_TRUNCATED")
                    self.assertIsNone(result.final_message)
                    self.assertEqual(host.invocations, [])
                    self.assertEqual(len(model.calls), 1)
                    self.assertEqual(list((root / "runtime/calls").glob("*.json")), [])
                    state = json.loads((root / "runtime/run-state.json").read_text())
                    self.assertNotIn("terminal_model_message", state)
                    self.assertFalse(any(item["role"] == "assistant" for item in state["messages"]))
                    with self.assertRaises(ValueError):
                        _completed_provider_result(root / "runtime")

    def test_real_sdk_timeout_has_no_automatic_retry_and_redacts_auth(self):
        with TemporaryDirectory() as directory:
            root, requests = Path(directory), []

            def handle(request):
                requests.append(request)
                raise httpx.ReadTimeout("Authorization Bearer " + SYNTHETIC_AUTH, request=request)

            client = OpenAI(api_key=SYNTHETIC_AUTH, base_url="https://synthetic.invalid/v1", max_retries=3,
                            http_client=httpx.Client(transport=httpx.MockTransport(handle)))
            try:
                with self.assertRaises(ProviderToolLoopError) as raised:
                    ProviderToolLoopDriver("aliyun", MODEL, lambda: client).run(**self._arguments(root))
                self.assertEqual(raised.exception.runtime_receipt["status"], "UNKNOWN")
                self.assertEqual(raised.exception.runtime_receipt["reason_code"], "PROVIDER_LOOP_APITIMEOUTERROR")
                self.assertEqual(len(requests), 1)
                self.assertEqual(client.max_retries, 3)
                self._assert_redacted(root, SYNTHETIC_AUTH, "Authorization", "Bearer")
            finally:
                client.close()

    def test_invalid_reasoning_is_redacted_before_tools(self):
        with TemporaryDirectory() as directory:
            root, host = Path(directory), ToolHost()
            response = reasoning_choice("invalid", {"Authorization": SYNTHETIC_AUTH})
            model = FakeCompletions([response])
            with self.assertRaises(ProviderToolLoopError) as raised:
                ProviderToolLoopDriver("aliyun", MODEL, model.client).run(**self._arguments(root, host))
            self.assertEqual(raised.exception.runtime_receipt["reason_code"], "PROVIDER_LOOP_VALUEERROR")
            self.assertEqual(host.invocations, [])
            self._assert_redacted(root, SYNTHETIC_AUTH, "Authorization")

    def test_shared_normalizer_keeps_structured_stage_azure_parameters_isolated(self):
        for provider in ("aliyun", "azure"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                source = Path(directory) / "synthetic.pdf"
                # Only the locked stage file is read in this transport test.
                source.write_bytes(b"Synthetic source identity; no PDF read requested")
                first = reasoning_choice("stage-file", "Synthetic stage reasoning")
                first["choices"][0]["message"]["tool_calls"][0]["function"] = {
                    "name": "read_stage_file", "arguments": '{"name":"scope.json"}'}
                model = FakeCompletions([first, final_choice('{"ok":true}')])
                result = ProviderStructuredStageTransport(provider=provider, model_id=MODEL,
                    client_factory=model.client, timeout_seconds=10,
                    cancellation_requested=lambda: False).invoke(ProviderStructuredStageInvocation(
                        source_path=source, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        execution_mode="SYNTHETIC", files=(("scope.json", {"synthetic": True}),),
                        instructions="Synthetic stage", output_schema={"type": "object",
                            "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}, binding={"task_id": "synthetic"}))
                self.assertEqual(result.structured_output, {"ok": True})
                assistant = model.calls[1]["messages"][-2]
                if provider == "aliyun":
                    self.assertEqual(assistant["reasoning_content"], "Synthetic stage reasoning")
                else:
                    self.assertNotIn("reasoning_content", assistant)
                self.assertNotIn("extra_body", model.calls[1])
