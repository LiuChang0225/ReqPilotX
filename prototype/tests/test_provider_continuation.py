"""Offline source-bound handoff: invented PDF, real compiler/review/export, fake SDK."""
from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from unittest.mock import Mock

import httpx
from openai import OpenAI

from .test_provider_tool_loop import FakeCompletions, ToolHost, tool_choice, final_choice
from reqpilotx_server.adapters.agents.provider_tool_loop import (
    ProviderToolLoopDriver, ProviderToolLoopError, _digest, _write_json, _paired_history,
)
from reqpilotx_server.application.agent_workflow.provider_continuation import (
    saved_predecessor, build_continuation_handoff,
)
from reqpilotx_server.application.agent_workflow.native_loop_tools import NativeLoopToolHost

PLUGIN = Path(__file__).resolve().parents[2] / "plugins/reqpilot"
sys.path[:0] = [str(PLUGIN / "tests"), str(PLUGIN / "scripts")]
from portable_fixtures import make_fixture, write, item_decisions
from test_review_loop import args
import artifact_tools
import revision_tools as review_tools


def serialized_size(messages):
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


class ProviderContinuationTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="reqpilotx-continuation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.source, self.capture, decisions = make_fixture(self.workspace)
        write(self.workspace / "map.json", decisions)
        self.mapping = self.workspace / "map"
        artifact_tools.compile_map(args(source=self.source, source_ir=self.capture / "source_ir.json",
            input=self.workspace / "map.json", output_dir=self.mapping))
        write(self.workspace / "items.json", item_decisions(self.mapping))
        self.baseline_dir = self.workspace / "baseline"
        artifact_tools.compile_items(args(map_dir=self.mapping, input=self.workspace / "items.json",
            output_dir=self.baseline_dir))
        self.review = self.workspace / "review"
        review_tools.open_review(args(review_dir=self.review, baseline_dir=self.baseline_dir, source=self.source))
        exported = self.workspace / "export"
        artifact_tools.deliver(args(baseline_dir=self.baseline_dir, source=self.source, output_dir=exported))
        head = review_tools.load_head(self.review)
        review_tools.attach_current_delivery(args(review_dir=self.review, delivery_dir=exported,
            expected_review_sha256=head["review_sha256"]))
        self.head = review_tools.load_head(self.review)
        self.baseline = review_tools.bundle(self.baseline_dir)[1]
        self.sid = self.baseline.records[0].source_record_id
        self.meta = {"task_id": "task", "project_id": "project", "document_id": "document",
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "source_path": str(self.source.relative_to(self.workspace)),
            "job_kind": "agent.source-item.extract-loop", "latest_job_id": "saved-job"}
        self.binding = {key: self.meta[key] for key in ("task_id", "project_id", "document_id", "source_sha256")}
        self.binding["mode"] = "RAW_SOURCE"
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("Synthetic registered skill", encoding="utf-8")
        self.host = ToolHost()
        self.driver = ProviderToolLoopDriver("aliyun", "qwen3.8-max-0902", lambda: None)
        self.finish_initial()
        self.feedback()

    def run_model(self, responses, goal="Initial source task", **kwargs):
        fake = FakeCompletions(responses)
        self.driver.client_factory = fake.client
        result = self.driver.run(session_dir=self.root / "runtime", goal=goal,
            skill_paths={"reqpilot": self.skill}, tool_host=self.host, binding=self.binding, **kwargs)
        return result, fake

    def finish_initial(self, responses=None):
        result, _ = self.run_model(responses or [tool_choice(), final_choice()])
        self.completed = result
        self.saved = SimpleNamespace(job_id="saved-job", status=SimpleNamespace(value="SUCCEEDED"),
            payload=dict(self.meta), result={"application_saved": True, "runtime_receipt": result.receipt,
            "baseline_sha256": self.baseline.artifact_sha256,
            "native_source_graph_artifact": {"synthetic": "saved-object"},
            "delivery_receipt": json.loads((self.review / self.head["delivery"]["path"] / "delivery-receipt.json").read_text())})
        self.container = SimpleNamespace(jobs=SimpleNamespace(get=lambda key: self.saved))
        self.job = SimpleNamespace(payload={"continuation_predecessor_job_id": "saved-job",
            "baseline_sha256": self.baseline.artifact_sha256})
        self.graph = {"source_sha256": self.meta["source_sha256"],
            "baseline": self.baseline.model_dump(mode="json"), "runtime_receipt": result.receipt,
            "host_verification": {"review": {"review_sha256": self.head["review_sha256"]}},
            "map_compilation_receipt": json.loads((self.mapping / "map-compilation-receipt.json").read_text())}
        (self.root / "producer-events.jsonl").write_text(json.dumps({"operation": "compile-map",
            "arguments": {"output_dir": "map", "source_ir": "capture/source_ir.json"}}) + "\n")

    def feedback(self):
        head = review_tools.load_head(self.review)
        path = self.workspace / "feedback.json"
        write(path, {"expected_review_sha256": head["review_sha256"], "feedback": [{"origin": "USER",
            "source_item_ids": [self.sid], "text": "Review the operating supply classification only."}]})
        self.feedback_result = review_tools.feedback_review(args(review_dir=self.review, input=path))

    def handoff(self):
        with patch("reqpilotx_server.application.agent_workflow.source_item_agent_jobs._read_private", return_value=self.graph):
            return build_continuation_handoff(self.container, self.root, self.meta, self.job,
                verify_review=lambda path: review_tools.verify_review(args(review_dir=path)))

    def test_terminal_feedback_projects_handoff_and_keeps_exact_source_and_audit(self):
        before = {p: p.read_bytes() for p in (self.root / "runtime").rglob("*") if p.is_file()}
        baseline_bytes = (self.baseline_dir / "source-requirement-baseline.json").read_bytes()
        source_bytes = self.source.read_bytes()
        handoff = self.handoff()
        self.assertEqual(handoff, self.handoff())
        self.assertEqual(handoff["target_record_ids"], [self.sid])
        self.assertEqual(handoff["baseline_sha256"], self.baseline.artifact_sha256)
        self.assertTrue(handoff["source_ir"])
        continued, fake = self.run_model([tool_choice("active-call"), final_choice("Revised")],
            goal="Apply the saved classification feedback", continuation_handoff=handoff)
        self.assertEqual(continued.status, "COMPLETED")
        self.assertEqual(len(fake.calls[0]["messages"]), 3)
        self.assertNotIn("tool_call_id", json.dumps(fake.calls[0]["messages"]))
        self.assertTrue(_paired_history(fake.calls[1]["messages"]))
        state = json.loads((self.root / "runtime/run-state.json").read_text())
        original = json.loads(before[self.root / "runtime/run-state.json"])
        self.assertEqual(state["messages"][:len(original["messages"])], original["messages"])
        for path, data in before.items():
            if path.name not in {"run-state.json", "events.jsonl", "messages.json", ".session.lock"}:
                self.assertEqual(path.read_bytes(), data)
        for ref in handoff["audit"].values():
            path = self.workspace / ref["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), ref["sha256"])
        tool_host = NativeLoopToolHost(workspace=self.workspace, review_dir=self.review, plugin_root=PLUGIN)
        audit = next(iter(handoff["audit"].values()))
        reply = tool_host.invoke("reqpilot_read", {"path": audit["path"], "json_pointer": ""})
        self.assertTrue(reply["success"])
        index = json.loads((self.workspace / handoff["audit"]["calls_index"]["path"]).read_text())
        self.assertEqual(len(index["calls"]), len(self.completed.receipt["tools"]))
        call = index["calls"][0]
        self.assertTrue(tool_host.invoke("reqpilot_read", {"path": call["path"], "json_pointer": "/response"})["success"])
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual((self.baseline_dir / "source-requirement-baseline.json").read_bytes(), baseline_bytes)
        self.assertEqual(review_tools.bundle(self.baseline_dir)[1].records, self.baseline.records)
        self.assertNotIn("max_tokens", fake.calls[0])
        self.assertNotIn("max_completion_tokens", fake.calls[0])

    def test_classification_successor_preserves_other_records_text_hashes_and_references(self):
        handoff = self.handoff()
        original = {path: path.read_bytes() for path in self.baseline_dir.iterdir() if path.is_file()}
        source = self.source.read_bytes()
        head = review_tools.load_head(self.review)
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"],
            "expected_baseline_sha256": handoff["baseline_sha256"], "model_id": "offline-explicit-decision",
            "decisions": [{"source_record_id": self.sid, "requirementness": "REQUIREMENT",
                "requirement_kinds": ["CONSTRAINT", "QUALITY"], "confidence": 0.9,
                "decision_summary": "The target specifies timing and operating supply.", "reason_codes": ["complete_target"]}],
            "addressed_feedback_ids": handoff["feedback_ids"]}
        path = self.workspace / "revision.json"
        write(path, value)
        candidate = self.review / "versions/revised"
        review_tools.revise_review(args(review_dir=self.review, input=path, output_dir=candidate))
        after = review_tools.bundle(candidate)[1]
        self.assertEqual(after.records[1:], self.baseline.records[1:])
        before_record = self.baseline.records[0].model_dump(mode="json")
        after_record = after.records[0].model_dump(mode="json")
        changed = {key for key in before_record if before_record[key] != after_record[key]}
        self.assertLessEqual(changed, {"requirement_kinds", "confidence", "decision_summary", "reason_codes", "review_metadata"})
        self.assertEqual(after.records[0].text_bindings, self.baseline.records[0].text_bindings)
        for old_path, data in original.items():
            self.assertEqual(old_path.read_bytes(), data)
        self.assertEqual(self.source.read_bytes(), source)
        delivery = self.review / "deliveries/revised"
        artifact_tools.deliver(args(baseline_dir=candidate, source=self.source, output_dir=delivery))
        write(path, {"expected_review_sha256": head["review_sha256"], "candidate_path": "versions/revised",
            "delivery_path": "deliveries/revised", "resolutions": [{"feedback_id": handoff["feedback_ids"][0],
                "disposition": "ADDRESSED", "reason": "Explicit synthetic classification correction."}]})
        review_tools.close_review(args(review_dir=self.review, input=path))
        self.assertEqual(review_tools.verify_review(args(review_dir=self.review))["current"]["baseline_sha256"], after.artifact_sha256)

    def test_baseline_and_saved_proof_fail_closed_before_model(self):
        for change in ({"baseline_sha256": None}, {"baseline_sha256": "b" * 64}):
            with self.subTest(change=change):
                old = dict(self.job.payload)
                self.job.payload.update(change)
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    self.handoff()
                self.job.payload = old
        for key, value in (("application_saved", False), ("runtime_receipt", {})):
            old = self.saved.result[key]
            self.saved.result[key] = value
            with self.assertRaisesRegex(ValueError, "saved completed"):
                self.handoff()
            self.saved.result[key] = old
        path = self.review / self.head["current"]["version_path"] / "source-requirement-baseline.json"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises((ValueError, OSError)):
            self.handoff()
        path.write_bytes(original)
        self.source.write_bytes(b"changed original")
        with self.assertRaises(ValueError):
            self.handoff()
        # The retained review original must also remain the same source.
        original_path = self.review / self.head["source"]["path"]
        original_path.write_bytes(b"changed retained original")
        with self.assertRaises(ValueError):
            self.handoff()

    def test_source_ir_byte_drift_is_rejected(self):
        (self.mapping / "map-source-ir.json").write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            self.handoff()

    def test_unknown_original_tool_receipt_cannot_be_hidden_by_handoff(self):
        path = next((self.root / "runtime/calls").glob("*.json"))
        value = json.loads(path.read_text())
        value["status"] = "INVOKING"
        _write_json(path, value)
        with self.assertRaisesRegex(ValueError, "original tool audit"):
            self.handoff()

    def test_interrupted_active_chain_retains_pairs_and_qwen_reasoning(self):
        handoff = self.handoff()
        response = tool_choice("active-qwen")
        response["choices"][0]["message"]["reasoning_content"] = "Synthetic active reasoning"
        fake = FakeCompletions([response], on_call=lambda: cancel.update(value=True))
        cancel = {"value": False}
        self.driver.client_factory = fake.client
        result = self.driver.run(session_dir=self.root / "runtime", goal="Apply feedback",
            skill_paths={"reqpilot": self.skill}, tool_host=self.host, binding=self.binding,
            continuation_handoff=handoff, cancellation_requested=lambda: cancel["value"])
        self.assertEqual(result.status, "INTERRUPTED")
        self.assertEqual(self.handoff(), handoff)
        resumed, fake = self.run_model([final_choice()], goal="Apply feedback", continuation_handoff=handoff)
        self.assertEqual(resumed.status, "COMPLETED")
        messages = fake.calls[0]["messages"]
        self.assertTrue(_paired_history(messages))
        self.assertEqual(messages[-2]["tool_calls"][0]["id"], "active-qwen")
        self.assertEqual(messages[-2]["reasoning_content"], "Synthetic active reasoning")
        self.assertEqual(messages[-1]["tool_call_id"], "active-qwen")

    def test_active_recovery_revalidates_fixed_source_versions_and_saved_proof(self):
        handoff = self.handoff()
        cancel = {"value": False}
        fake = FakeCompletions([tool_choice("pending-drift-check")], on_call=lambda: cancel.update(value=True))
        self.driver.client_factory = fake.client
        self.driver.run(session_dir=self.root / "runtime", goal="Feedback", skill_paths={"reqpilot": self.skill},
            tool_host=self.host, binding=self.binding, continuation_handoff=handoff,
            cancellation_requested=lambda: cancel["value"])
        invocations = len(self.host.invocations)
        refs = [handoff["original_source"], handoff["source"], *handoff["source_ir"],
                *handoff["version_files"].values(), handoff["review"],
                *handoff["delivery"].values(), *handoff["audit"].values()]
        index = json.loads((self.workspace / handoff["audit"]["calls_index"]["path"]).read_text())
        paths = [self.workspace / ref["path"] for ref in refs + index["calls"]]
        paths += [self.root / "runtime" / self.completed.receipt["run_state_reference"],
                  self.root / "runtime" / ("receipt-" + self.completed.receipt["attempt_id"] + ".json"),
                  next((self.root / "runtime/calls").glob("*.json"))]
        for marker in (True, False):
            self.job.payload = {"baseline_sha256": self.baseline.artifact_sha256}
            if marker:
                self.job.payload["continuation_predecessor_job_id"] = "saved-job"
            for path in paths:
                original = path.read_bytes()
                for changed in (b"changed immutable evidence", None):
                    with self.subTest(marker=marker, path=path.name, deleted=changed is None):
                        if changed is None:
                            path.unlink()
                        else:
                            path.write_bytes(changed)
                        with self.assertRaises((ValueError, OSError, KeyError)):
                            self.handoff()
                        path.write_bytes(original)
            for field, value in (("application_saved", False), ("baseline_sha256", "b" * 64),
                                 ("runtime_receipt", {})):
                original = self.saved.result[field]
                self.saved.result[field] = value
                with self.subTest(marker=marker, field=field), self.assertRaises(ValueError):
                    self.handoff()
                self.saved.result[field] = original
            self.saved.status.value = "CANCELLED"
            with self.assertRaises(ValueError):
                self.handoff()
            self.saved.status.value = "SUCCEEDED"
        self.assertEqual(len(self.host.invocations), invocations)
        self.assertEqual(len(fake.calls), 1)
        # New mutable review feedback is allowed while old immutable evidence
        # stays fixed. Recovery must neither rebase nor reject valid steering.
        self.feedback()
        self.assertIsNone(self.handoff())
        self.job.payload["continuation_predecessor_job_id"] = "saved-job"
        self.assertEqual(self.handoff(), handoff)
        _, resumed = self.run_model([final_choice()], goal="Feedback", continuation_handoff=handoff)
        self.assertTrue(_paired_history(resumed.calls[0]["messages"]))
        self.assertEqual(resumed.calls[0]["messages"][-1]["tool_call_id"], "pending-drift-check")

    def test_unsaved_completed_suffix_is_kept_for_markerless_feedback(self):
        handoff = self.handoff()
        self.run_model([tool_choice("unsaved-call"), final_choice("Unsaved completion")],
            goal="Unsaved turn", continuation_handoff=handoff)
        state = json.loads((self.root / "runtime/run-state.json").read_text())
        suffix = state["messages"][state["continuation_context"]["start_index"]:]
        self.job.payload.pop("continuation_predecessor_job_id")
        self.assertIsNone(self.handoff())
        _, fake = self.run_model([final_choice()], goal="Next feedback")
        self.assertEqual(fake.calls[0]["messages"][2:-1], suffix)
        self.assertTrue(_paired_history(fake.calls[0]["messages"]))

    def test_unpaired_terminal_history_and_wrong_handoff_are_rejected(self):
        handoff = self.handoff()
        for key, value in (("baseline_sha256", None), ("binding_sha256", "b" * 64),
                           ("completed_run_state_sha256", "b" * 64)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "proof"):
                self.run_model([final_choice()], goal="Feedback", continuation_handoff={**handoff, key: value})
        state_path = self.root / "runtime/run-state.json"
        state = json.loads(state_path.read_text())
        state["messages"].insert(-1, {"role": "tool", "tool_call_id": "orphan", "content": "synthetic"})
        state["messages_sha256"] = _digest(state["messages"])
        _write_json(state_path, state)
        with self.assertRaisesRegex(ValueError, "proof"):
            self.run_model([final_choice()], goal="Feedback", continuation_handoff=handoff)

    def test_active_or_interrupted_turn_cannot_receive_a_terminal_handoff(self):
        handoff = self.handoff()
        state_path = self.root / "runtime/run-state.json"
        original = json.loads(state_path.read_text())
        for status in ("RUNNING", "UNKNOWN", "INTERRUPTED", "FAILED"):
            state = deepcopy(original)
            state["status"] = status
            _write_json(state_path, state)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "completed turn"):
                self.run_model([final_choice()], goal="Feedback", continuation_handoff=handoff)
        assistant = tool_choice()["choices"][0]["message"]
        self.assertFalse(_paired_history([assistant]))
        self.assertTrue(_paired_history([assistant, {"role": "tool", "tool_call_id": "call-1"}]))

    def test_audit_drift_and_context_drift_fail_before_pending_tool_replay(self):
        handoff = self.handoff()
        state_path = self.root / "runtime/run-state.json"
        original = json.loads(state_path.read_text())
        changed = deepcopy(original)
        changed["messages"][1]["content"] = "Changed prior audit"
        changed["messages_sha256"] = _digest(changed["messages"])
        _write_json(state_path, changed)
        with self.assertRaisesRegex(ValueError, "completed snapshot"):
            self.handoff()
        _write_json(state_path, original)
        response = tool_choice("pending-active")
        cancel = {"value": False}
        fake = FakeCompletions([response], on_call=lambda: cancel.update(value=True))
        self.driver.client_factory = fake.client
        self.driver.run(session_dir=self.root / "runtime", goal="Feedback", skill_paths={"reqpilot": self.skill},
            tool_host=self.host, binding=self.binding, continuation_handoff=handoff,
            cancellation_requested=lambda: cancel["value"])
        state = json.loads(state_path.read_text())
        original = deepcopy(state)
        state["continuation_context"]["handoff"]["source_sha256"] = "b" * 64
        _write_json(state_path, state)
        calls = len(self.host.invocations)
        with self.assertRaisesRegex(ValueError, "context changed"):
            self.run_model([final_choice()], goal="Feedback")
        self.assertEqual(len(self.host.invocations), calls)
        state = original
        state["messages"].append({"role": "user", "content": "Later active feedback"})
        state["messages_sha256"] = _digest(state["messages"])
        state["continuation_context"]["start_index"] = len(state["messages"]) - 1
        _write_json(state_path, state)
        with self.assertRaisesRegex(ValueError, "context changed"):
            self.run_model([final_choice()], goal="Feedback")
        self.assertEqual(len(self.host.invocations), calls)

    def test_review_task_uses_same_terminal_boundary_without_raw_graph(self):
        from shutil import move
        handoff = self.handoff()
        # Review-only tasks own review/ directly, with source and version files.
        move(str(self.review), str(self.root / "review"))
        self.meta["job_kind"] = "agent.source-item.loop"
        self.saved.result["review_sha256"] = handoff["saved_review_sha256"]
        self.saved.result.pop("native_source_graph_artifact")
        packet = self.handoff()
        self.assertEqual(packet["baseline_sha256"], handoff["baseline_sha256"])
        self.assertEqual(packet["target_record_ids"], [self.sid])
        self.assertTrue(packet["audit"])
        for name in ("source_ir.json", "source-ir.json", "map-source-ir.json"):
            path = self.root / "review" / name
            write(path, {"source_sha256": "b" * 64})
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "producer proof"):
                self.handoff()
            path.unlink()

    def test_durable_worker_passes_built_handoff_to_provider_only(self):
        from reqpilotx_server.application.agent_workflow import source_document_loop_jobs as jobs
        from reqpilotx_server.infrastructure.runtime_settings import AgentRuntimeBackend
        from reqpilotx.application.agent_workflow.native_task_brief import build_task_brief
        container = SimpleNamespace(settings=SimpleNamespace(agent_runtime_backend=AgentRuntimeBackend.PROVIDER_TOOL_LOOP))
        meta = {**self.meta, "latest_job_id": "current-job", "request": {}}
        job = SimpleNamespace(job_id="current-job", aggregate_id="document", payload={
            "project_id": "project", "task_id": "task", "application_entrypoint": jobs.ENTRYPOINT,
            "phase": "REVIEW", "goal": "Saved feedback", "origin": "USER_REQUEST",
            "continuation_predecessor_job_id": "saved-job"})
        handoff = self.handoff()
        with patch.object(jobs, "_raw_existing", return_value=(container, self.root, meta)), \
             patch("reqpilotx_server.application.agent_workflow.native_loop_capabilities.native_loop_plugin_for_job", return_value=PLUGIN), \
             patch.object(jobs, "SourceDocumentToolHost") as host, \
             patch("reqpilotx_server.application.agent_workflow.provider_continuation.build_continuation_handoff", return_value=handoff) as builder, \
             patch.object(jobs, "build_provider_tool_loop_driver") as factory:
            host.return_value.task_brief.return_value = build_task_brief(goal="Saved feedback", origin="USER_REQUEST")
            factory.return_value.run.return_value = SimpleNamespace(status="INTERRUPTED", receipt={}, final_message=None)
            result = jobs.run_durable_source_document_loop(job, Mock())
        self.assertFalse(result["application_saved"])
        self.assertEqual(builder.call_args.args[3], job)
        self.assertEqual(factory.return_value.run.call_args.kwargs["continuation_handoff"], handoff)

    def test_old_jobs_and_unsaved_or_interrupted_predecessors_keep_original_behavior(self):
        self.assertEqual(saved_predecessor(self.container, self.meta), {"continuation_predecessor_job_id": "saved-job"})
        for status, saved in (("FAILED", True), ("CANCELLED", True), ("SUCCEEDED", False), ("RUNNING", False)):
            self.saved.status.value = status
            self.saved.result["application_saved"] = saved
            self.assertEqual(saved_predecessor(self.container, self.meta), {})
        self.job.payload.pop("continuation_predecessor_job_id")
        self.assertIsNone(self.handoff())
        result, fake = self.run_model([final_choice()], goal="Old job feedback")
        self.assertEqual(result.status, "COMPLETED")
        self.assertGreater(len(fake.calls[0]["messages"]), 3)

    def test_synthetic_history_request_sizes(self):
        # Deterministic 35 tool responses, 43 tool calls + terminal response;
        # 8 paired batches carry a second call. This is not the private live run.
        state_path = self.root / "runtime/run-state.json"
        state_path.unlink()
        class LargeHost(ToolHost):
            def invoke(self, name, args):
                return {"success": True, "content_items": [{"type": "inputText", "text": "Synthetic source evidence " * 220}]}
        self.host = LargeHost()
        responses = []
        for index in range(35):
            response = tool_choice("synthetic-" + str(index))
            response["choices"][0]["message"]["reasoning_content"] = "Synthetic reasoning " * 100
            if index < 8:
                response["choices"][0]["message"]["tool_calls"].append(
                    tool_choice("extra-" + str(index))["choices"][0]["message"]["tool_calls"][0])
            responses.append(response)
        self.finish_initial([*responses, final_choice()])
        state = json.loads(state_path.read_text())
        old_request = [*state["messages"], {"role": "user", "content": "Apply saved classification feedback"}]
        handoff = self.handoff()
        _, fake = self.run_model([final_choice()], goal="Apply saved classification feedback", continuation_handoff=handoff)
        new_request = fake.calls[0]["messages"]
        comparison = {"fixture": "35 tool batches / 43 calls / 36 responses, invented evidence",
            "before": {"message_count": len(old_request), "serialized_bytes": serialized_size(old_request)},
            "after": {"message_count": len(new_request), "serialized_bytes": serialized_size(new_request)}}
        self.assertEqual(comparison["before"]["message_count"], 82)
        self.assertEqual(comparison["after"]["message_count"], 3)
        self.assertLess(comparison["after"]["serialized_bytes"], comparison["before"]["serialized_bytes"])
        # Report only synthetic counts/sizes, no request or source content.
        print("SYNTHETIC_CONTINUATION_COMPARISON=" + json.dumps(comparison, sort_keys=True))


class ProviderUsageEvidenceTests(TestCase):
    def test_real_sdk_mock_response_retains_usage_cache_and_request_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("Synthetic skill")
            response = {**final_choice(), "id": "synthetic-response", "model": "synthetic",
                "object": "chat.completion", "created": 0,
                "usage": {"prompt_tokens": 123, "completion_tokens": 4, "total_tokens": 127,
                          "prompt_tokens_details": {"cached_tokens": 13}}}
            client = OpenAI(api_key="synthetic-test-key", base_url="https://synthetic.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(lambda request:
                    httpx.Response(200, json=response, headers={"x-request-id": "synthetic-request"}))))
            self.addCleanup(client.close)
            result = ProviderToolLoopDriver("azure", "synthetic", lambda: client).run(session_dir=root / "runtime",
                goal="Synthetic request", skill_paths={"reqpilot": skill}, tool_host=ToolHost(), binding={"task_id": "synthetic"})
            metadata = result.receipt["provider_requests"][0]
            self.assertEqual(metadata["request_id"], "synthetic-request")
            self.assertEqual(metadata["cached_tokens"], 13)
            self.assertEqual(metadata["token_usage"]["total_tokens"], 127)
            self.assertNotIn("synthetic-test-key", json.dumps(result.receipt))

    def test_usage_precedes_length_and_parse_rejection_without_content_or_headers(self):
        for finish in ("length", "invalid"):
            with self.subTest(finish=finish), TemporaryDirectory() as directory:
                root = Path(directory)
                skill = root / "SKILL.md"
                skill.write_text("Synthetic skill")
                response = {"id": "synthetic-response", "usage": {"prompt_tokens": 111,
                    "completion_tokens": 7, "total_tokens": 118, "prompt_tokens_details": {"cached_tokens": 9}},
                    "choices": [{"finish_reason": finish, "message": {"content": "DO_NOT_LOG_BODY"}}],
                    "headers": {"authorization": "DO_NOT_LOG_AUTH"}, "other": "data:image/DO_NOT_LOG_IMAGE"}
                fake = FakeCompletions([response])
                driver = ProviderToolLoopDriver("azure", "synthetic", fake.client)
                kwargs = dict(session_dir=root / "runtime", goal="Synthetic request", skill_paths={"reqpilot": skill},
                    tool_host=ToolHost(), binding={"task_id": "synthetic"})
                if finish == "length":
                    result = driver.run(**kwargs)
                    receipt = result.receipt
                    self.assertEqual(result.status, "FAILED")
                else:
                    with self.assertRaises(ProviderToolLoopError) as error:
                        driver.run(**kwargs)
                    receipt = error.exception.runtime_receipt
                self.assertEqual(receipt["token_usage"], {"prompt_tokens": 111, "completion_tokens": 7, "total_tokens": 118})
                metadata = receipt["provider_requests"][0]
                self.assertEqual(metadata["cached_tokens"], 9)
                self.assertIsNone(metadata["request_id"])
                self.assertEqual(metadata["response_id"], "synthetic-response")
                self.assertEqual(json.loads(next((root / "runtime").glob("request-*.json")).read_text()), metadata)
                for path in (root / "runtime").glob("*.json*"):
                    text = path.read_text()
                    for forbidden in ("DO_NOT_LOG_BODY", "DO_NOT_LOG_AUTH", "DO_NOT_LOG_IMAGE"):
                        self.assertNotIn(forbidden, text)
                self.assertNotIn("extra_body", fake.calls[0])

    def test_missing_metadata_stays_null_and_usage_accumulates_once_per_response(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("Synthetic skill")
            first, second = tool_choice(), final_choice()
            first["usage"] = {"prompt_tokens": 101, "completion_tokens": 2, "total_tokens": 103}
            second["usage"] = {"prompt_tokens": 211, "completion_tokens": 3, "total_tokens": 214}
            fake = FakeCompletions([first, second])
            result = ProviderToolLoopDriver("azure", "synthetic", fake.client).run(session_dir=root / "runtime",
                goal="Synthetic request", skill_paths={"reqpilot": skill}, tool_host=ToolHost(), binding={"task_id": "synthetic"})
            self.assertEqual(result.receipt["token_usage"], {"prompt_tokens": 312, "completion_tokens": 5, "total_tokens": 317})
            for metadata in result.receipt["provider_requests"]:
                self.assertIsNone(metadata["request_id"])
                self.assertIsNone(metadata["cached_tokens"])
