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
from unittest import TestCase
from unittest.mock import Mock, patch

from app.adapters.agents.codex_app_server.native_loop_driver import NativeLoopToolSpec, _digest
from reqpilotx_server.adapters.agents.provider_tool_loop import (
    ADAPTER_ID, TRANSPORT_KIND, ProviderToolLoopDriver,
    build_provider_tool_loop_driver,
)
from app.infrastructure.runtime_settings import (
    AgentProvider, AgentRuntimeBackend, RuntimeSettings, RuntimeConfigurationError,
)
from app.application.agent_workflow import source_document_loop_jobs as document_jobs
from app.application.agent_workflow import source_item_loop_jobs as item_jobs
from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from app.application.agent_workflow.native_loop_finalization import (
    verified_finalization_inputs,
)


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


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
    def _run(self, root, provider, responses, host=None, **kwargs):
        fake = FakeCompletions(responses)
        host = host or ToolHost()
        skill = root / "SKILL.md"
        skill.write_text("Registered extraction skill", encoding="utf-8")
        driver = ProviderToolLoopDriver(provider, "synthetic-model", lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=fake)))
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
                driver.client_factory = lambda: SimpleNamespace(chat=SimpleNamespace(completions=followup))
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
            checks = iter([False, True])
            result, fake, host, driver, skill = self._run(
                root, "aliyun", [tool_choice()],
                cancellation_requested=lambda: next(checks, False))
            self.assertEqual(result.status, "INTERRUPTED")
            self.assertEqual(len(host.invocations), 0)
            resumed_model = FakeCompletions([final_choice()])
            driver.client_factory = lambda: SimpleNamespace(chat=SimpleNamespace(completions=resumed_model))
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
            checks = iter([False, True])
            result, _, host, driver, skill = self._run(
                root, "azure", [tool_choice()],
                cancellation_requested=lambda: next(checks, False))
            thread = result.receipt["thread_id"]
            identity = {"thread_id": thread, "turn_id": result.receipt["turn_id"],
                        "call_id": "call-1", "name": "reqpilot_read",
                        "arguments_sha256": _digest({"path": "source"})}
            receipt_path = root / "runtime/calls" / (hashlib.sha256((thread + ":" + result.receipt["turn_id"] + ":call-1").encode()).hexdigest() + ".json")
            receipt_path.write_text(json.dumps({"identity": identity, "status": "INVOKING"}))
            model = FakeCompletions([final_choice()])
            driver.client_factory = lambda: SimpleNamespace(chat=SimpleNamespace(completions=model))
            continued = driver.run(session_dir=root / "runtime", goal="Read synthetic source",
                skill_paths={"reqpilot": skill}, tool_host=host,
                binding={"task_id": "synthetic-task", "source_sha256": "a" * 64})
            self.assertEqual(continued.status, "COMPLETED")
            self.assertEqual(host.invocations, [])
            self.assertIn("OUTCOME_UNKNOWN", model.calls[0]["messages"][-1]["content"])

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
            driver = ProviderToolLoopDriver("aliyun", "synthetic-model", lambda: SimpleNamespace(
                chat=SimpleNamespace(completions=model)))
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
            driver = ProviderToolLoopDriver("azure", "synthetic-model", lambda: SimpleNamespace(
                chat=SimpleNamespace(completions=model)))
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
