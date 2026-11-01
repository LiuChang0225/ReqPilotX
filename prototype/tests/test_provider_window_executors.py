from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime
activate_test_runtime()

from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from openai import AzureOpenAI, OpenAI

from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import build_synthetic_pdf
from app.adapters.agents.portable_kit import compute_agent_kit_lock
import shutil
from reqpilotx_server.adapters.agents.provider_structured_stage import (
    ProviderStructuredStageError, ProviderStructuredStageTransport, ProviderStructuredStageInvocation,
)
from reqpilotx_server.adapters.agents.provider_window_executors import (
    ProviderSourceItemTextExecutor, ProviderSourceRequirementWindowExecutor,
)
from app.infrastructure.runtime_container import (
    _build_source_item_text_executor, _build_source_requirement_window_executor,
)
from app.infrastructure.runtime_settings import RuntimeConfigurationError, RuntimeSettings
from app.ports.source_requirement_window_executor import SourceRequirementWindowTaskV1
from tests.test_source_requirement_app_server_executor import _source_bound_windowed_fixture
from tests.test_native_source_requirement_windowed_reference import _boundary_candidate, _classification_candidate
from tests.test_source_item_text_app_server_executor import (
    _scenario, _boundary_payload, _classification_payload,
)

ROOT = Path(__file__).resolve().parents[2]


class _Completions:
    """Mock the configured SDK, not the structured stage/executor interface."""
    def __init__(self, *, fail_window=None, invalid_json=False, invalid_schema=False,
                 on_call=None, wrong_key_once=False):
        self.calls = []
        self.phases = Counter()
        self.fail_window = fail_window
        self.invalid_json = invalid_json
        self.invalid_schema = invalid_schema
        self.on_call = on_call
        self.wrong_key_once = wrong_key_once
        self.lock = threading.Lock()
        self.closed = 0
        self.options = []

    def client(self):
        owner = self
        class Client:
            chat = SimpleNamespace(completions=owner)
            def with_options(self, **kwargs):
                owner.options.append(kwargs)
                return self
            def close(self):
                owner.closed += 1
        return Client()

    def create(self, **kwargs):
        packet = json.loads(kwargs["messages"][1]["content"])
        phase = packet["files"]["phase-input.json"]
        mode = packet["execution_mode"]
        tool_replies = [item for item in kwargs["messages"] if item["role"] == "tool"]
        with self.lock:
            self.calls.append(kwargs.copy())
        if self.on_call:
            self.on_call()
        if not tool_replies:
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [
                    {"id": "source-page", "type": "function", "function": {
                        "name": "read_source_page", "arguments": '{"page":1}'}},
                    {"id": "full-scope", "type": "function", "function": {
                        "name": "read_stage_file", "arguments": '{"name":"scope.full.json"}'}},
                ]}}]}
        window = phase.get("classification_window", {})
        window_id = window.get("window", {}).get("window_id", window.get("window_id", mode))
        key = (mode, phase.get("scope_id", window_id))
        with self.lock:
            self.phases[key] += 1
            attempt = self.phases[key]
        if self.fail_window is not None and window_id == self.fail_window:
            raise RuntimeError("secret-token customer-body https://private-endpoint")
        if mode == "SOURCE_REQUIREMENT_BOUNDARY_PLAN":
            payload = _boundary_candidate()
            for record in payload["records"]:
                record["owning_text_spans"] = []
        elif mode == "SOURCE_ITEM_TEXT_BOUNDARY_PLAN":
            full_scope = json.loads(json.loads(tool_replies[1]["content"])["text"])
            payload = _boundary_payload(full_scope)
            payload["table_owned_spans"] = []
        else:
            keys = [str(item["record_key"]) for item in window["records"]]
            if self.wrong_key_once and attempt == 1:
                keys = keys[:-1]
            payload = (_classification_candidate(keys) if mode.startswith("SOURCE_REQUIREMENT")
                       else _classification_payload(keys))
        content = "not JSON" if self.invalid_json else json.dumps({} if self.invalid_schema else payload)
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}


@contextmanager
def _table_task():
    with tempfile.TemporaryDirectory() as raw:
        source = Path(raw) / "source.pdf"
        build_synthetic_pdf(source)
        document_map, request = _source_bound_windowed_fixture(hashlib.sha256(source.read_bytes()).hexdigest())
        yield SourceRequirementWindowTaskV1(
            project_id="p", document_id="d", source_path=source,
            document_map=document_map.model_dump(mode="json"), scope_request=request.model_dump(mode="json"),
            timeout_seconds=60, max_concurrency=2,
        )


def _adapter(provider, completions, *, text=False, enabled=True, model="configured-model"):
    cls = ProviderSourceItemTextExecutor if text else ProviderSourceRequirementWindowExecutor
    return cls(provider=provider, model_id=model, client_factory=completions.client,
               kit_root=ROOT / "agent-kit", customer_data_enabled=enabled)


class ProviderWindowExecutorTests(unittest.TestCase):
    def test_runtime_stages_own_fresh_real_sdk_http_clients_and_never_close_cached_clients(self):
        # Exercise real OpenAI/Azure SDK construction, copy and close semantics;
        # only HTTP is mocked. with_options shares its underlying HTTP client.
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as raw:
                settings = RuntimeSettings.from_environment(environment={
                    "REQPILOT_ENV": "test", "REQPILOT_AGENT_RUNTIME": "provider-tool-loop",
                    "REQPILOT_AGENT_PROVIDER": provider, "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED": "1",
                    "REQPILOT_STRUCTURED_AGENT_KIT_ROOT": str(ROOT / "agent-kit"),
                }, prototype_dir=Path(raw))
                profile = SimpleNamespace(azure_openai_chat_ready=True, azure_openai_chat_deployment="configured-model",
                    dashscope_chat_ready=True, chat_model="configured-model", screening_model="screening",
                    uses_api_key_for_openai=True, azure_openai_endpoint="https://synthetic.openai.azure.com",
                    azure_openai_api_key="synthetic-test-key", azure_openai_api_version="2024-10-21",
                    azure_allow_interactive_login=False, dashscope_api_key="synthetic-test-key",
                    dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    llm_timeout_seconds=60, openai_max_retries=2)
                completions = _Completions(); clients = []
                def respond(request):
                    payload = json.loads(request.content)
                    reply = completions.create(**payload)
                    reply.update(id="synthetic-completion", created=0, object="chat.completion", model="configured-model")
                    return httpx.Response(200, json=reply)
                def construct(cls, **kwargs):
                    http = httpx.Client(transport=httpx.MockTransport(respond)); clients.append(http)
                    return cls(http_client=http, **kwargs)
                with patch("reqpilotx_server.services.ai_copilot.load_ai_copilot_config", return_value=profile), \
                     patch("reqpilotx_server.services.ai_copilot.get_runtime_ai_config_value", return_value=""), \
                     patch("reqpilotx_server.services.ai_copilot._azure_openai_client", side_effect=AssertionError("cached client borrowed")), \
                     patch("reqpilotx_server.services.ai_copilot._aliyun_openai_client", side_effect=AssertionError("cached client borrowed")), \
                     patch("openai.AzureOpenAI", side_effect=lambda **kwargs: construct(AzureOpenAI, **kwargs)), \
                     patch("openai.OpenAI", side_effect=lambda **kwargs: construct(OpenAI, **kwargs)):
                    adapter = _build_source_requirement_window_executor(settings)
                    self.assertEqual(clients, [])
                    with _table_task() as task:
                        for _ in range(2):
                            adapter.execute(task, resume_checkpoint=None, checkpoint_sink=lambda _: None,
                                            cancellation_requested=lambda: False)
                    self.assertEqual(len(clients), 6)
                    self.assertTrue(all(client.is_closed for client in clients))
                    self.assertEqual(len({id(client) for client in clients}), 6)

    def test_both_providers_complete_and_restart_actual_table_and_text_stages(self):
        for provider in ("azure", "aliyun"):
            for text in (False, True):
                with self.subTest(provider=provider, text=text), (_scenario() if text else _table_task()) as value:
                    task = value.task if text else value
                    model = _Completions()
                    checkpoints = []
                    first = _adapter(provider, model, text=text).execute(
                        task, resume_checkpoint=None, checkpoint_sink=lambda row: checkpoints.append(dict(row)),
                        cancellation_requested=lambda: False,
                    )
                    candidate = first.batch if text else first.candidate
                    self.assertEqual(len(candidate["candidates"] if text else candidate["records"]), 2 if text else 6)
                    self.assertTrue(first.run_receipt["codex_version"].startswith(f"provider-structured-stage:{provider}:"))
                    self.assertEqual(first.run_receipt["reasoning_effort"], "provider-default")
                    self.assertIn("read_source_page", first.run_receipt["tool_names"])
                    self.assertTrue(all(call["model"] == "configured-model" for call in model.calls))
                    self.assertTrue(all(call["timeout"] > 0 for call in model.calls))
                    self.assertTrue(all(option == {"max_retries": 0} for option in model.options))
                    self.assertTrue(any(item["role"] == "user" and isinstance(item["content"], list)
                                        for call in model.calls for item in call["messages"]))
                    self.assertEqual(model.closed, 4 if text else 3)
                    restarted = _Completions()
                    result = _adapter(provider, restarted, text=text).execute(
                        task, resume_checkpoint=checkpoints[-1], checkpoint_sink=lambda _: None,
                        cancellation_requested=lambda: False,
                    )
                    self.assertEqual(result.batch if text else result.candidate, candidate)
                    self.assertEqual(restarted.calls, [])

    def test_provider_table_resume_rejects_source_model_provider_and_context_drift(self):
        with _table_task() as task:
            saved = []
            _adapter("azure", _Completions()).execute(task, resume_checkpoint=None,
                checkpoint_sink=lambda row: saved.append(dict(row)), cancellation_requested=lambda: False)
            for provider, model, changed in (
                ("aliyun", "configured-model", task), ("azure", "different-model", task),
                ("azure", "configured-model", replace(task, semantic_context={"changed": True})),
                ("azure", "configured-model", replace(task, document_id="different-document")),
            ):
                with self.subTest(provider=provider, model=model, document=changed.document_id):
                    calls = _Completions()
                    with self.assertRaisesRegex(ValueError, "identity drifted"):
                        _adapter(provider, calls, model=model).execute(changed, resume_checkpoint=saved[-1],
                            checkpoint_sink=lambda _: None, cancellation_requested=lambda: False)
                    self.assertEqual(calls.calls, [])
            task.source_path.write_bytes(b"changed-source")
            with self.assertRaisesRegex(ValueError, "source identity drifted"):
                _adapter("azure", _Completions()).execute(task, resume_checkpoint=saved[-1],
                    checkpoint_sink=lambda _: None, cancellation_requested=lambda: False)

    def test_both_providers_resume_only_failed_text_window_and_retain_feedback(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), _scenario() as scenario:
                checkpoints = []
                failed = _Completions(fail_window="text-window-02")
                with self.assertRaisesRegex(RuntimeError, "text-window-02") as error:
                    _adapter(provider, failed, text=True).execute(scenario.task,
                        checkpoint_sink=lambda row: checkpoints.append(dict(row)), cancellation_requested=lambda: False)
                self.assertNotIn("secret-token", str(error.exception.__cause__))
                self.assertEqual(len(checkpoints[-1]["accepted_windows"]), 1)
                resumed = _Completions(wrong_key_once=True)
                _adapter(provider, resumed, text=True).execute(scenario.task, resume_checkpoint=checkpoints[-1],
                    cancellation_requested=lambda: False)
                self.assertEqual(set(resumed.phases), {("SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW", "text-window-02")})
                self.assertEqual(sum(resumed.phases.values()), 2)
                self.assertTrue(any("validation-feedback.json" in json.loads(call["messages"][1]["content"])["files"]
                                    for call in resumed.calls))

    def test_json_schema_and_cancellation_fail_closed_for_both_providers(self):
        for provider in ("azure", "aliyun"):
            for bad, expected in (({"invalid_json": True}, "JSON_INVALID"),
                                  ({"invalid_schema": True}, "OUTPUT_SCHEMA_INVALID")):
                with self.subTest(provider=provider, bad=bad), _table_task() as task:
                    checkpoints = []
                    with self.assertRaisesRegex(ProviderStructuredStageError, expected):
                        _adapter(provider, _Completions(**bad)).execute(task, resume_checkpoint=None,
                            checkpoint_sink=checkpoints.append, cancellation_requested=lambda: False)
                    self.assertEqual(checkpoints, [])
            with _table_task() as task:
                for before in (True, False):
                    cancelled = [before]
                    model = _Completions(on_call=lambda: cancelled.__setitem__(0, True))
                    checkpoints = []
                    with self.assertRaisesRegex(RuntimeError, "CANCELLED"):
                        _adapter(provider, model).execute(task, resume_checkpoint=None,
                            checkpoint_sink=checkpoints.append, cancellation_requested=lambda: cancelled[0])
                    self.assertEqual(len(model.calls), 0 if before else 1)
                    self.assertEqual(checkpoints, [])

    def test_runtime_selection_uses_existing_configured_clients_without_codex(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as raw:
                settings = RuntimeSettings.from_environment(environment={
                    "REQPILOT_ENV": "test", "REQPILOT_AGENT_RUNTIME": "provider-tool-loop",
                    "REQPILOT_AGENT_PROVIDER": provider, "REQPILOT_AGENT_CUSTOMER_DATA_ENABLED": "1",
                    "REQPILOT_STRUCTURED_AGENT_KIT_ROOT": str(ROOT / "agent-kit"),
                }, prototype_dir=Path(raw))
                profile = SimpleNamespace(azure_openai_chat_ready=True, azure_openai_chat_deployment="configured-model",
                                          dashscope_chat_ready=True, chat_model="configured-model", screening_model="screening")
                calls = _Completions()
                with patch("reqpilotx_server.services.ai_copilot.load_ai_copilot_config", return_value=profile), \
                     patch("reqpilotx_server.services.ai_copilot.get_runtime_ai_config_value", return_value=""), \
                     patch("reqpilotx_server.services.ai_copilot._new_azure_openai_client", side_effect=lambda _: calls.client()) as azure, \
                     patch("reqpilotx_server.services.ai_copilot._new_aliyun_openai_client", side_effect=lambda _: calls.client()) as aliyun:
                    table = _build_source_requirement_window_executor(settings)
                    text = _build_source_item_text_executor(settings)
                    self.assertIsInstance(table, ProviderSourceRequirementWindowExecutor)
                    self.assertIsInstance(text, ProviderSourceItemTextExecutor)
                    self.assertIsNone(settings.codex_bin)
                    self.assertIsNone(settings.development_root)
                    self.assertIsNone(table.readiness_reason())
                    self.assertFalse(azure.called or aliyun.called)
                    with _table_task() as task:
                        table.execute(task, resume_checkpoint=None, checkpoint_sink=lambda _: None,
                                      cancellation_requested=lambda: False)
                    self.assertEqual(azure.call_count, 3 if provider == "azure" else 0)
                    self.assertEqual(aliyun.call_count, 3 if provider == "aliyun" else 0)

    def test_read_tools_are_bounded_and_cannot_read_arbitrary_files(self):
        with _table_task() as task:
            result, image = ProviderStructuredStageTransport._read(
                "read_stage_file", {"name": "../../secret"}, source=task.source_path, files={"allowed": "x" * 33000})
            self.assertEqual(result, {"error": "STAGE_FILE_NOT_FOUND"})
            self.assertIsNone(image)
            result, _ = ProviderStructuredStageTransport._read(
                "read_stage_file", {"name": "allowed"}, source=task.source_path, files={"allowed": "x" * 33000})
            self.assertEqual(len(result["text"]), 32000)
            self.assertEqual(result["next_offset"], 32000)
            result, _ = ProviderStructuredStageTransport._read(
                "read_source_page", {"page": 999}, source=task.source_path, files={})
            self.assertEqual(result["error"], "SOURCE_PAGE_OUT_OF_RANGE")

    def test_expired_response_cannot_be_accepted(self):
        for provider in ("azure", "aliyun"):
            with self.subTest(provider=provider), _table_task() as task:
                clock = [0.0]
                calls = _Completions(on_call=lambda: clock.__setitem__(0, 99.0))
                transport = ProviderStructuredStageTransport(provider=provider, model_id="model",
                    client_factory=calls.client, timeout_seconds=5, cancellation_requested=lambda: False)
                invocation = ProviderStructuredStageInvocation(source_path=task.source_path,
                    source_sha256=task.document_map["source_sha256"], execution_mode="test",
                    files=(("phase-input.json", {}),), instructions="Test only",
                    output_schema={"type": "object"}, binding={"scope": "synthetic"})
                with patch("reqpilotx_server.adapters.agents.provider_structured_stage.time.monotonic", side_effect=lambda: clock[0]), \
                     self.assertRaisesRegex(ProviderStructuredStageError, "TIMEOUT"):
                    transport.invoke(invocation)
                self.assertEqual(len(calls.calls), 1)
                self.assertEqual(calls.closed, 1)

    def test_older_kit_checkpoint_and_schema_drift_fail_without_model_calls(self):
        with _table_task() as task:
            checkpoints = []
            _adapter("azure", _Completions()).execute(task, resume_checkpoint=None,
                checkpoint_sink=lambda row: checkpoints.append(dict(row)), cancellation_requested=lambda: False)
            checkpoint = dict(checkpoints[-1])
            checkpoint["kit_version"] = "0.15.2"
            from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
            checkpoint["checkpoint_sha256"] = canonical_json_sha256({k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"})
            calls = _Completions()
            with self.assertRaisesRegex(ValueError, "identity drifted"):
                _adapter("azure", calls).execute(task, resume_checkpoint=checkpoint,
                    checkpoint_sink=lambda _: None, cancellation_requested=lambda: False)
            self.assertEqual(calls.calls, [])
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "agent-kit"
                shutil.copytree(ROOT / "agent-kit", root)
                schema = root / "schemas/source-requirement-boundary-agent-candidate.v1.schema.json"
                schema.write_text('{"type":"object"}')
                (root / "agent-kit.lock.json").write_text(compute_agent_kit_lock(root).model_dump_json(indent=2) + "\n")
                executor = _adapter("azure", calls)
                executor._kit_root = root
                with self.assertRaisesRegex(ProviderStructuredStageError, "OUTPUT_SCHEMA_DRIFTED"):
                    executor.execute(task, resume_checkpoint=None,
                        checkpoint_sink=lambda _: None, cancellation_requested=lambda: False)
                self.assertEqual(calls.calls, [])

    def test_changed_configured_model_is_rejected_before_first_stage(self):
        calls = _Completions()
        executor = _adapter("azure", calls)
        executor._configuration_check = lambda: ("azure", "different-model", calls.client)
        self.assertEqual(executor.readiness_reason(), "PROVIDER_STAGE_CONFIGURATION_DRIFTED")
        with _table_task() as task, self.assertRaisesRegex(ProviderStructuredStageError, "CONFIGURATION_DRIFTED"):
            executor.execute(task, resume_checkpoint=None, checkpoint_sink=lambda _: None,
                             cancellation_requested=lambda: False)
        self.assertEqual(executor.model_id, "configured-model")
        self.assertEqual(calls.calls, [])

    def test_data_opt_in_and_missing_kit_block_before_client_creation(self):
        calls = _Completions()
        executor = _adapter("azure", calls, enabled=False)
        self.assertEqual(executor.readiness_reason(), "PROVIDER_STAGE_CUSTOMER_DATA_DISABLED")
        with _table_task() as task, self.assertRaisesRegex(ProviderStructuredStageError, "CUSTOMER_DATA_DISABLED"):
            executor.execute(task, resume_checkpoint=None, checkpoint_sink=lambda _: None, cancellation_requested=lambda: False)
        self.assertEqual(calls.calls, [])
        executor = _adapter("azure", calls)
        executor._kit_root = ROOT / "missing-agent-kit"
        self.assertEqual(executor.readiness_reason(), "PROVIDER_STAGE_CONFIGURATION_OR_KIT_UNAVAILABLE")
        with self.assertRaisesRegex(RuntimeConfigurationError, "must be absolute"):
            RuntimeSettings.from_environment(environment={"REQPILOT_ENV": "test", "REQPILOT_STRUCTURED_AGENT_KIT_ROOT": "relative"})
