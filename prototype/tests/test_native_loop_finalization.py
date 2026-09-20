"""A completed native turn can publish after recovery without another model."""

from __future__ import annotations

# ruff: noqa: E402 -- isolate application runtime before importing its adapters.
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.native_loop_driver import NativeLoopResult
from app.application.agent_workflow import native_loop_finalization as finalization
from app.application.agent_workflow import source_document_loop_jobs as raw
from app.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256 as sha,
)
from app.application.agent_workflow.source_item_loop_jobs import LoopControlRequestV1


class NativeFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.review = self.workspace / "review"
        for path in ["review/versions/final", "review/deliveries/final", "inputs"]:
            (self.workspace / path).mkdir(parents=True)
        (self.root / "runtime/calls").mkdir(parents=True)
        self.write(self.review / "versions/final/baseline.json", {"synthetic": True})
        self.review_sha = "e" * 64
        (self.review / "deliveries/final/matrix.xlsx").write_bytes(
            b"synthetic Excel bytes"
        )
        xlsx_sha = hashlib.sha256(b"synthetic Excel bytes").hexdigest()
        self.write(
            self.review / "deliveries/final/delivery-receipt.json",
            {
                "source_sha256": "a" * 64,
                "baseline_artifact_sha256": "f" * 64,
                "xlsx_sha256": xlsx_sha,
            },
        )
        self.write(
            self.review / "workspace-manifest.json",
            {
                "review_sha256": self.review_sha,
                "current": {
                    "version_path": "versions/final",
                    "baseline_sha256": "f" * 64,
                    "files": {
                        "baseline.json": hashlib.sha256(
                            (self.review / "versions/final/baseline.json").read_bytes()
                        ).hexdigest()
                    },
                },
                "delivery": {"path": "deliveries/final", "xlsx_sha256": xlsx_sha},
            },
        )
        self.write(
            self.workspace / "inputs/close.json",
            {"candidate_path": "versions/final", "delivery_path": "deliveries/final"},
        )
        self.write(self.root / "controls.json", {"steering": []})
        self.meta = {
            "task_id": "task",
            "project_id": "p",
            "document_id": "d",
            "source_sha256": "a" * 64,
            "job_kind": raw.EXTRACTION_JOB_KIND,
            "latest_job_id": "job-original",
        }
        self.write(self.root / "task.json", self.meta)
        self.original = SimpleNamespace(
            job_id="job-original",
            kind=raw.EXTRACTION_JOB_KIND,
            status=SimpleNamespace(value="FAILED"),
            result=None,
            payload={
                "task_id": "task",
                "project_id": "p",
                "document_id": "d",
                "source_sha256": "a" * 64,
                "phase": "REVIEW",
                "application_entrypoint": raw.ENTRYPOINT,
                "native_graph_predecessor": {
                    "graph_pointer": {"object_sha256": "b" * 64}
                },
            },
        )
        self.write(
            self.root / "publish-request-job-original.json",
            {"job_id": self.original.job_id, "review_sha256": self.review_sha},
        )
        self.tools = []
        self.add_call(
            "deliver",
            "reqpilot_run",
            {
                "operation": "deliver",
                "arguments": {
                    "baseline_dir": "review/versions/final",
                    "output_dir": "review/deliveries/final",
                },
            },
            {"status": "DRAFT_EXPORTED"},
        )
        self.add_call(
            "close",
            "reqpilot_run",
            {"operation": "review-close", "arguments": {"input": "inputs/close.json"}},
            {
                "status": "REVIEW_ADVANCED",
                "review_sha256": self.review_sha,
                "current_version_path": "versions/final",
                "baseline_sha256": "f" * 64,
            },
        )
        self.add_call(
            "publish",
            "reqpilot_publish",
            {"review_sha256": self.review_sha},
            {"status": "PUBLICATION_REQUEST_RECORDED"},
        )
        self.result = NativeLoopResult(
            status="COMPLETED",
            final_message="Completed original Agent turn",
            receipt={
                "status": "COMPLETED",
                "thread_id": "thread",
                "turn_id": "turn",
                "tools": self.tools,
                "terminal_snapshot_verified": True,
            },
        )
        self.recover = patch(
            "app.adapters.agents.codex_app_server.native_loop_history.recover_completed_native_result",
            return_value=self.result,
        )
        self.recover.start()

    def tearDown(self):
        self.recover.stop()
        self.temp.cleanup()

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def add_call(self, call_id, name, args, value):
        response = {
            "success": True,
            "contentItems": [{"type": "inputText", "text": json.dumps(value)}],
        }
        identity = {
            "thread_id": "thread",
            "turn_id": "turn",
            "call_id": call_id,
            "name": name,
            "arguments_sha256": sha(args),
        }
        self.write(
            self.root / "runtime/calls" / (sha(["thread", call_id]) + ".json"),
            {
                "identity": identity,
                "status": "RETURNED",
                "arguments": args,
                "response": response,
            },
        )
        self.tools.append(
            {**identity, "success": True, "response_sha256": sha(response)}
        )

    def test_missing_tools_changed_bytes_and_new_feedback_cannot_finalize(self):
        finalization.verified_finalization_inputs(self.root, self.meta, self.original)
        saved = self.tools.pop()
        with self.assertRaisesRegex(ValueError, "publish and close"):
            finalization.verified_finalization_inputs(
                self.root, self.meta, self.original
            )
        self.tools.append(saved)
        self.write(self.root / "controls.json", {"steering": [{"status": "PENDING"}]})
        with self.assertRaisesRegex(ValueError, "New feedback"):
            finalization.verified_finalization_inputs(
                self.root, self.meta, self.original
            )
        self.write(self.root / "controls.json", {"steering": []})
        (self.review / "versions/final/baseline.json").write_text("changed")
        with self.assertRaisesRegex(ValueError, "file SHA"):
            finalization.verified_finalization_inputs(
                self.root, self.meta, self.original
            )

    def test_same_kind_host_job_preserves_all_original_pins_and_skips_model(self):
        jobs = {self.original.job_id: self.original}

        def enqueue(**kwargs):
            job = SimpleNamespace(
                job_id="job-host-finalize",
                kind=kwargs["kind"],
                aggregate_id="d",
                payload=kwargs["payload"],
                status=SimpleNamespace(value="PENDING"),
                result=None,
            )
            jobs[job.job_id] = job
            return job

        container = SimpleNamespace(jobs=SimpleNamespace(get=jobs.get, enqueue=enqueue))

        def projection(*args):
            return {
                "job_id": json.loads((self.root / "task.json").read_text())[
                    "latest_job_id"
                ]
            }

        with (
            patch.object(
                finalization,
                "_existing",
                return_value=(container, self.root, self.meta),
            ),
            patch.object(finalization, "get_source_item_loop", side_effect=projection),
        ):
            first = finalization.finalize_source_item_loop(
                "p",
                "d",
                "task",
                LoopControlRequestV1(command_id="save-completed"),
                SimpleNamespace(headers={}),
            )
            again = finalization.finalize_source_item_loop(
                "p",
                "d",
                "task",
                LoopControlRequestV1(command_id="save-completed"),
                SimpleNamespace(headers={}),
            )
        self.assertEqual(first, again)
        job = jobs[first["job_id"]]
        for key, value in self.original.payload.items():
            self.assertEqual(job.payload[key], value)
        self.assertEqual(job.kind, self.original.kind)
        self.assertEqual(len(jobs), 2)
        meta = json.loads((self.root / "task.json").read_text())
        context = SimpleNamespace(heartbeat=lambda **kwargs: None)
        with (
            patch.object(
                raw, "_raw_existing", return_value=(container, self.root, meta)
            ),
            patch.object(
                raw.LocalCodexAppServerNativeLoopDriver,
                "run",
                side_effect=AssertionError("No new model allowed"),
            ),
            patch.object(
                raw,
                "_publish_initial_graph",
                return_value={
                    "application_saved": True,
                    "native_source_graph_artifact": {"object_sha256": "c" * 64},
                },
            ) as publisher,
        ):
            result = raw.run_durable_source_document_loop(job, context)
        self.assertTrue(result["application_saved"])
        self.assertFalse(result["host_finalization"]["model_turn_started"])
        self.assertEqual(publisher.call_args.args[3], job)
        self.assertIsNone(self.original.result)
