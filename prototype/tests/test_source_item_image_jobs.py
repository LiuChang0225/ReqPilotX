from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pymupdf

from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow import document_map_jobs as maps
from app.application.agent_workflow import source_item_image_jobs as images
from app.application.agent_workflow import source_requirement_baseline as core
from app.ports.job_queue import DurableJob, JobStatus, NonRetryableJobError


def job_value(kind, payload, job_id="job-" + "1" * 32):
    now = datetime.now(UTC)
    return DurableJob(job_id=job_id, kind=kind, aggregate_type="sor-document", aggregate_id="doc",
        idempotency_key="test", payload=payload, status=JobStatus.PENDING, priority=22, attempts=0,
        max_attempts=3, generation=1, cancel_requested=False, lease_owner="test", lease_expires_at=None,
        available_at=now, created_at=now, updated_at=now, progress={}, result=None, error_code="", error_message="")


class Queue:
    def __init__(self):
        self.jobs = {}
        self.identities = {}

    def get(self, job_id):
        return self.jobs.get(job_id)

    def get_by_idempotency(self, *, kind, idempotency_key):
        return self.identities.get((kind, idempotency_key))

    def enqueue(self, **values):
        job = job_value(values["kind"], values["payload"], f"job-{len(self.jobs)+1:032x}")
        self.jobs[job.job_id] = job
        self.identities[(values["kind"], values["idempotency_key"])] = job
        return job


class Context:
    def __init__(self):
        self.progress = {}
        self.publications = 0

    def heartbeat(self, progress=None):
        if progress is not None: self.progress = progress
        return SimpleNamespace(progress=self.progress)

    def cancellation_requested(self): return False
    def begin_publication(self): self.publications += 1
    def lease_fence(self): return None


class ImageJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reqpilot-image-job-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        doc = pymupdf.open(); page = doc.new_page(width=300, height=300)
        page.insert_text((20, 35), "Drawing")
        page.draw_rect(pymupdf.Rect(20, 70, 180, 150))
        page.insert_text((30, 110), "WIDTH <= 18 mm")
        doc.save(self.source); doc.close()
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        public = {"schema_version": "native-structure-reference-public/v1", "artifact_id": "native-test", "source_sha256": self.sha,
            "document_tree": {"root_ids": ["main"], "nodes": [{"node_id": "main", "parent_id": None, "order": 0, "kind": "DOCUMENT", "label": "", "title": "Drawing", "pages": [1],
                "source_anchors": [{"anchor_id": "drawing-anchor", "anchor_kind": "FIGURE", "page": 1, "bbox": [20.0,70.0,180.0,150.0], "source_bounds": [0.0,0.0,300.0,300.0], "source_handle": "drawing-source", "precision": "ORIGINAL_DRAWING_REGION"}]}]}, "logical_tables": [], "structure_proposals": []}
        self.document_map = core.compile_document_map_artifact(public)
        self.public = public
        baseline, receipt = core.create_empty_source_requirement_baseline(self.document_map)
        self.bundle = {"document_map": self.document_map, "baseline": baseline, "receipt": receipt}
        self.current = {"source_sha256": self.sha, "artifact_id": "native-test", "document": {}}
        self.container = SimpleNamespace(jobs=Queue(), object_store=LocalObjectStore(self.root / "objects"), agent_structure_loop_repository=object(),
            settings=SimpleNamespace(codex_bin=self.root / "codex", codex_home=self.root, scratch_root=self.root, agent_model_id="test-double"))

    def tearDown(self): self.temp.cleanup()

    def start(self):
        return images.start_source_item_images("project", "doc", images.SourceItemImagesStartRequestV1(
            command_id="image-test", expected_source_sha256=self.sha, expected_artifact_id="native-test", node_ids=("main",)), SimpleNamespace(headers={}))

    def test_real_pixels_shared_compiler_publisher_and_resume_contract(self):
        calls = []
        def complete(**kwargs):
            calls.append(kwargs)
            self.assertEqual(kwargs["packet"]["document_context"]["source_sha256"], self.sha)
            self.assertTrue(kwargs["packet"]["document_context"]["document_text"])
            self.assertTrue(kwargs["image_inputs"])
            for image in kwargs["image_inputs"]:
                self.assertEqual(hashlib.sha256(image.path.read_bytes()).hexdigest(), image.expected_sha256)
                self.assertEqual(image.source_sha256, self.sha)
            candidate = {"items": [{"item_key": "drawing", "display_label": "Dimensional drawing", "image_anchor_ids": ["drawing-anchor"],
                "requirementness": "REQUIREMENT", "requirement_kinds": ["QUALITY"], "non_requirement_role": None,
                "confidence": 0.9, "decision_summary": "The drawing specifies a dimensional limit.", "reason_codes": ["normative_drawing"], "visual_interpretation": "Agent reading, separate from the pixels."}]}
            return SimpleNamespace(candidate=candidate, receipt={"status": "CANDIDATE_RETURNED", "thread_cleanup_verified": True, "model_id": "test-double", "test_double": True})
        with patch.object(images, "_container", return_value=self.container), patch.object(images, "_current_reference_context", return_value=self.current), patch.object(images, "_load_effective_reference", return_value=self.bundle), patch.object(images, "resolve_source_record_path", return_value=self.source), patch.object(images, "LocalCodexAppServerJsonStageDriver", return_value=SimpleNamespace(complete=complete)), patch("app.application.agent_workflow.source_requirement_jobs.publish_agent_source_item_revision", return_value={"status":"SCOPE_COMPLETE"}) as publish:
            started = self.start()
            self.assertEqual(self.start()["job_id"], started["job_id"])
            job = self.container.jobs.get(started["job_id"])
            context = Context()
            result = images.run_durable_source_item_images(job, context)
            self.assertEqual((result["image_count"], result["completed_scope_count"]), (1,1))
            artifact = publish.call_args.kwargs["baseline"]
            self.assertEqual(artifact.records[0].source_display_id, "SRC-0001")
            self.assertEqual(artifact.records[0].source_form, core.SourceRecordForm.IMAGE)
            self.assertFalse(artifact.records[0].text_bindings)
            self.assertEqual(publish.call_args.kwargs["operation"], "IMAGE_SCOPE")
            self.assertEqual(publish.call_args.kwargs["runtime_receipts"][0]["test_double"], True)
            images.run_durable_source_item_images(replace(job, progress=context.progress), Context())
            self.assertEqual(len(calls), 1, "A verified checkpoint must not repeat the model call")

    def test_changed_map_stops_before_model_or_publication(self):
        with patch.object(images, "_container", return_value=self.container), patch.object(images, "_current_reference_context", return_value=self.current), patch.object(images, "_load_effective_reference", return_value=self.bundle), patch.object(images, "resolve_source_record_path", return_value=self.source):
            started = self.start(); job = self.container.jobs.get(started["job_id"])
            changed = dict(self.current, artifact_id="another-map")
            with patch.object(images, "_current_reference_context", return_value=changed), patch.object(images, "LocalCodexAppServerJsonStageDriver") as driver:
                with self.assertRaises(NonRetryableJobError): images.run_durable_source_item_images(job, Context())
                driver.assert_not_called()

    def test_image_context_contains_actual_related_table_and_reports_missing_preview(self):
        from copy import deepcopy
        public = deepcopy(self.public)
        public["document_tree"]["nodes"][0]["content_blocks"] = [{"kind": "TABLE", "logical_table_scope_id": "legend"}]
        document_map = core.compile_document_map_artifact(public)
        preview = {"logical_table_scope_id": "legend", "rows": [["S", "This customer's specific responsibility"]]}
        with patch.object(images, "materialize_native_structure_table_preview", return_value=preview) as materialize:
            result = images._context(document_map, "main", {"bundle": {}}, self.source, {})
            self.assertEqual(result["complete_related_tables"], [preview])
            self.assertEqual(result["unavailable_tables"], [])
            self.assertEqual(materialize.call_args.kwargs["source_path"], self.source)
            self.assertEqual(materialize.call_args.kwargs["source_sha256"], self.sha)
        with patch.object(images, "materialize_native_structure_table_preview", side_effect=ValueError("unreadable")):
            result = images._context(document_map, "main", {"bundle": {}}, self.source, {})
            self.assertEqual(result["complete_related_tables"], [])
            self.assertEqual(result["unavailable_tables"], [{"scope_id": "legend", "reason_code": "ValueError"}])

    def test_all_runs_includes_title_content_and_image_scopes(self):
        content = lambda text: SimpleNamespace(text=text, is_title=True)
        nodes = [SimpleNamespace(node_id="pure-title", source_content=[content("Overview")], source_anchors=[]),
                 SimpleNamespace(node_id="normative-title", source_content=[content("The valve shall open.")], source_anchors=[]),
                 SimpleNamespace(node_id="image-owner", source_content=[], source_anchors=[SimpleNamespace(anchor_kind="IMAGE")])]
        document_map = SimpleNamespace(nodes=nodes, logical_tables=[], artifact_id="map")
        bundle = {"document_map":document_map, "baseline":SimpleNamespace(covered_scope_ids=[])}
        with patch("app.application.agent_workflow.native_structure_bridge.get_native_structure_reference", return_value={"source_sha256":self.sha,"artifact_id":"native-test"}), patch("app.application.agent_workflow.source_requirement_reference_bridge._load_effective_reference", return_value=bundle), patch("app.application.agent_workflow.source_requirement_jobs.start_source_item_text_baseline", return_value={"job_id":"text-job"}) as text_start, patch.object(images, "start_source_item_images", return_value={"job_id":"image-job"}) as image_start:
            result = maps.start_document_source_items("project","doc", maps.SourceItemDocumentStartRequestV1(command_id="all",expected_source_sha256=self.sha,expected_artifact_id="native-test"), SimpleNamespace(headers={}))
            self.assertEqual(text_start.call_args.args[2].node_ids, ("pure-title","normative-title"))
            self.assertEqual(image_start.call_args.args[2].node_ids, ("image-owner",))
            self.assertIn("image:image-owner", result["expected_scope_ids"])

    def test_auto_source_items_is_durable_and_keeps_partial_status(self):
        payload={"project_id":"project","source_sha256":self.sha,"run_id":"run", "kit_bundle_sha256":"a"*64,"timeout_seconds":1200,"auto_source_items":True}
        job=job_value(maps.DOCUMENT_MAP_JOB_KIND,payload)
        output={"status":"PARTIAL","artifact_id":"native-test","working_revision":1,"revision_sha256":"b"*64,"unresolved":[{"code":"readable_scope_only"}]}
        reference={"source_sha256":self.sha,"artifact_id":"native-test","working_revision_head":{"revision_sha256":"b"*64}}
        with patch.object(maps,"_container",return_value=self.container), patch.object(maps,"_require_project_document",return_value={}), patch.object(maps,"_document_source_sha256",return_value=self.sha), patch.object(maps,"resolve_source_record_path",return_value=self.source), patch("app.adapters.agents.codex_app_server.document_map_executor.execute_document_map",return_value=output), patch("app.application.agent_workflow.native_structure_bridge.get_native_structure_reference",return_value=reference), patch.object(maps,"start_document_source_items",return_value={"status":"QUEUED","jobs":[{"job_id":"child"}],"expected_scope_ids":["image:main"]}) as start:
            result=maps.run_durable_document_map(job,Context())
            self.assertEqual(result["status"],"PARTIAL")
            self.assertEqual(result["source_item_job_ids"],["child"])
            self.assertEqual(start.call_args.args[2].command_id,job.job_id+":source-items")
            output_without_map={"status":"PARTIAL","unresolved":[{"code":"no_publishable_scope"}]}
            with patch("app.adapters.agents.codex_app_server.document_map_executor.execute_document_map",return_value=output_without_map):
                start.reset_mock(); result=maps.run_durable_document_map(job,Context()); start.assert_not_called()
                self.assertEqual(result["source_items_status"],"NOT_STARTED_NO_PUBLISHABLE_MAP")


if __name__ == "__main__": unittest.main()
