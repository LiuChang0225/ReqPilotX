"""Synthetic full worker path for multiple tables and their shared text scope."""
from __future__ import annotations
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow import source_requirement_jobs as jobs
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from app.adapters.storage.local_object_store import LocalObjectStore
from app.adapters.agents.codex_app_server.native_source_requirement_reference import NativeSourceRequirementRunReceiptV1
from app.adapters.agents.codex_app_server.native_source_item_text_reference import NativeSourceItemTextRunReceiptV1, SourceItemTextBatchCandidateV1
from app.adapters.agents.codex_app_server.native_source_item_text_windowed import source_item_text_request_set_sha256
from app.ports.source_requirement_window_executor import SourceRequirementWindowExecutionV1
from app.ports.source_item_text_executor import SourceItemTextExecutionV1
from app.ports.job_queue import NonRetryableJobError
from .test_source_requirement_baseline import _public_reference, _table_preview, _fragment, _record, _candidate
from .test_source_requirement_jobs import _current, _job, _Context


def fixture(source_sha256=None):
    ref = _public_reference()
    if source_sha256 is not None:
        ref["source_sha256"] = source_sha256
    node = ref["document_tree"]["nodes"][1]
    node["content_blocks"].append({"kind": "LOGICAL_TABLE", "logical_table_scope_id": "lt-pack"})
    text = "Packaging shall contain the items in the second table."
    node["source_content"].append({**node["source_content"][0], "content_id": "pack-context",
        "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "bbox": [0.0, 120.0, 100.0, 140.0]})
    table = deepcopy(ref["logical_tables"][0])
    table.update(scope_id="lt-pack", title="Packaging", fragments=[{"table_token": "T0004", "page": 2, "row_range": [0, 3]}])
    ref["logical_tables"].append(table)
    document_map = core.compile_document_map_artifact(ref)
    previews = {
        "lt-bom": _table_preview(scope_id="lt-bom", title="Products", fragments=(
            _fragment("bom-fragment", "T0001", page=2, rows=(("Item", "Name"), ("1", "A"), ("2", "B"), ("3", "C"), ("4", "D"))),)),
        "lt-pack": _table_preview(scope_id="lt-pack", title="Packaging", fragments=(
            _fragment("pack-fragment", "T0004", page=2, rows=(("Item", "Name"), ("1", "Box"), ("2", "Label"))),)),
        "lt-dvp": _table_preview(scope_id="lt-dvp", title="DVP", fragments=(
            _fragment("dvp-one", "T0002", page=10, rows=(("Test", "Criteria"), ("1", "A"), ("2", "B"), ("3", "C"))),
            _fragment("dvp-two", "T0003", page=11, rows=(("4", "D"), ("5", "E"))))),
    }
    if source_sha256 is not None:
        for preview in previews.values():
            preview["source_sha256"] = source_sha256
    requests = {key: core.build_source_requirement_scope_request(document_map, value) for key, value in previews.items()}
    candidates = {}
    for scope, fragment, end, content in (("lt-bom", "bom-fragment", 5, node["source_content"][0]),
            ("lt-pack", "pack-fragment", 3, node["source_content"][1]), ("lt-dvp", "dvp-one", 4, None)):
        groups = (core.SourceRequirementRowGroupV1(fragment_id=fragment, row_start=1, row_end=end),)
        if scope == "lt-dvp":
            groups += (core.SourceRequirementRowGroupV1(fragment_id="dvp-two", row_start=0, row_end=2),)
        record = _record(scope + "-record", scope, core.SourceRecordForm.TABLE_RECORD, groups)
        if content:
            record = record.model_copy(update={"owning_text_spans": (core.SourceItemTextSpanV1(
                content_id=content["content_id"], start_offset=0, end_offset=len(content["text"])),)})
        candidates[scope] = _candidate(requests[scope], records=(record,), context=(core.SourceRequirementContextRowGroupV1(
            role="HEADER", row_group=core.SourceRequirementRowGroupV1(fragment_id=fragment, row_start=0, row_end=1), reason_codes=("header",)),))
    previous, validation = core.compile_source_requirement_baseline(document_map=document_map,
        requests=(requests["lt-dvp"],), candidates=(candidates["lt-dvp"],))
    return document_map, previews, requests, candidates, previous, validation


class TableExecutor:
    adapter_id = "synthetic-table"
    adapter_version = "1"
    transport_kind = "SYNTHETIC"
    model_id = "synthetic"
    reasoning_effort = "high"
    def __init__(self, candidates):
        self.candidates, self.calls = candidates, []
    def execute(self, task, **kwargs):
        request = core.SourceRequirementScopeRequestV1.model_validate(task.scope_request)
        self.calls.append(task)
        candidate = self.candidates[request.logical_table_scope_id]
        receipt = NativeSourceRequirementRunReceiptV1(source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id, document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256, logical_table_scope_id=request.logical_table_scope_id,
            model_id=self.model_id, reasoning_effort=self.reasoning_effort, codex_version="synthetic",
            elapsed_milliseconds=1, event_counts=(), tool_names=(), record_count=len(candidate.records),
            context_row_group_count=len(candidate.context_row_groups),
            candidate_sha256=core.canonical_json_sha256(candidate.model_dump(mode="json")))
        return SourceRequirementWindowExecutionV1(candidate=candidate.model_dump(mode="json"), run_receipt=receipt.model_dump(mode="json"))


class TextExecutor:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []
    def readiness_reason(self):
        return None
    def execute(self, task, **kwargs):
        self.calls.append(task)
        if self.fail:
            raise RuntimeError("synthetic text interruption")
        request = core.SourceItemTextScopeRequestV1.model_validate(task.scope_requests[0])
        candidate = core.SourceItemTextSliceCandidateV1(source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id, document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256, scope_id=request.scope_id,
            table_owned_spans=tuple(core.SourceItemTableOwnedTextSpanV1.model_validate(item) for item in task.execution_context["table_owned_spans"]))
        batch = SourceItemTextBatchCandidateV1(source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id, document_map_artifact_sha256=request.document_map_artifact_sha256,
            candidates=(candidate,))
        receipt = NativeSourceItemTextRunReceiptV1(source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id, document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_set_sha256=source_item_text_request_set_sha256((request,), execution_context=task.execution_context),
            model_id="synthetic", reasoning_effort="high", codex_version="synthetic", elapsed_milliseconds=1,
            event_counts=(), tool_names=(), scope_count=1, scope_ids=(request.scope_id,), source_item_count=0,
            structure_only_span_count=0, candidate_sha256=core.canonical_json_sha256(batch.model_dump(mode="json")))
        return SourceItemTextExecutionV1(batch=batch.model_dump(mode="json"), run_receipt=receipt.model_dump(mode="json"))


class JointMixedSourceJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.document_map, self.previews, self.requests, self.candidates, self.previous, self.validation = fixture()
        self.table = TableExecutor(self.candidates)
        self.text = TextExecutor()
        self.container = SimpleNamespace(object_store=LocalObjectStore(Path(self.temp.name) / "objects"),
            source_requirement_window_executor=self.table, source_item_text_executor=self.text)
        self.job = _job(self.document_map, self.requests["lt-bom"])
        self.job.payload.update(executor_id=self.table.adapter_id, executor_version=self.table.adapter_version,
            transport_kind=self.table.transport_kind, model_id=self.table.model_id)
        self.current = {scope: {**_current(self.document_map, request), "source_requirement_base_bundle": {"synthetic": True}}
            for scope, request in self.requests.items()}
        self.enterContext(patch("app.application.runtime.RUNTIME_STATE.container", self.container))
        self.enterContext(patch.object(jobs, "_current_scope_context", side_effect=lambda **kwargs: self.current[kwargs["logical_table_scope_id"]]))
        self.enterContext(patch.object(jobs, "resolve_effective_source_requirement_bundle", return_value={
            "baseline": self.previous, "receipt": self.validation, "product_run_receipts": ()}))
        self.enterContext(patch("app.application.agent_workflow.source_item_manual_publication.attach_rebound_baseline_manual_contexts"))

    def test_interrupted_joint_group_resumes_tables_once_and_exports_same_objects(self):
        self.text.fail = True
        context = _Context(self.job)
        with self.assertRaisesRegex(RuntimeError, "synthetic text interruption"):
            jobs.run_durable_source_requirement_baseline(self.job, context)
        self.assertEqual(context.publication_count, 0)
        self.assertEqual(len(self.table.calls), 2)
        self.assertEqual(set(context.current_progress["source_requirement_completed_scopes"]), {"lt-bom", "lt-pack"})
        self.text.fail = False
        resumed = replace(self.job, progress=context.current_progress)
        context = _Context(resumed)
        result = jobs.run_durable_source_requirement_baseline(resumed, context)
        self.assertEqual(context.publication_count, 1)
        self.assertEqual(len(self.table.calls), 2)
        self.assertEqual(result["joint_table_scope_ids"], ["lt-bom", "lt-pack"])
        self.assertEqual(result["baseline_revision_artifact"]["record_count"], 3)
        self.assertEqual(len(self.text.calls[-1].execution_context["table_owned_spans"]), 2)
        self.assertIn("joint_table_plan", self.table.calls[-1].semantic_context)
        with self.container.object_store.open(result["baseline_revision_artifact"]["object_key"]) as stream:
            product = jobs.SourceRequirementProductBaselineArtifactV1.model_validate_json(stream.read())
        self.assertEqual(set(product.baseline.covered_scope_ids), {"lt-bom", "lt-pack", "lt-dvp", "text:bom-node"})
        self.assertEqual(product.baseline.revision, self.previous.revision + 1)
        self.assertEqual(len(product.product_run_receipts), 3)
        prior = next(item for item in product.baseline.records if item.source_record_id == self.previous.records[0].source_record_id)
        self.assertEqual(prior.source_display_id, self.previous.records[0].source_display_id)
        snapshot = build_delivery_snapshot(self.document_map, product.baseline, self.previews)
        book = load_workbook(BytesIO(export_delivery_xlsx(snapshot)))
        text = " ".join(str(cell.value) for row in book["ComplianceMatrix"] for cell in row if cell.value)
        for record in product.baseline.records:
            for binding in record.text_bindings:
                self.assertIn(binding.text, text)

    def test_conflicting_table_ownership_stops_before_text_or_publication(self):
        second = self.candidates["lt-pack"]
        self.candidates["lt-pack"] = second.model_copy(update={"records": (second.records[0].model_copy(update={
            "owning_text_spans": self.candidates["lt-bom"].records[0].owning_text_spans}),)})
        context = _Context(self.job)
        with self.assertRaises(NonRetryableJobError) as caught:
            jobs.run_durable_source_requirement_baseline(self.job, context)
        self.assertEqual(caught.exception.error_code, "source_requirement_mixed_joint_ownership_overlap")
        self.assertEqual(context.publication_count, 0)
        self.assertEqual(self.text.calls, [])

    def test_corrupt_completed_checkpoint_does_not_repeat_model_or_publish(self):
        self.text.fail = True
        context = _Context(self.job)
        with self.assertRaises(RuntimeError):
            jobs.run_durable_source_requirement_baseline(self.job, context)
        progress = deepcopy(context.current_progress)
        progress["source_requirement_completed_scopes"]["lt-pack"]["object_sha256"] = "0" * 64
        resumed = replace(self.job, progress=progress)
        context = _Context(resumed)
        with self.assertRaises(NonRetryableJobError) as caught:
            jobs.run_durable_source_requirement_baseline(resumed, context)
        self.assertEqual(caught.exception.error_code, "source_requirement_joint_result_checkpoint_invalid")
        self.assertEqual(len(self.table.calls), 2)
        self.assertEqual(context.publication_count, 0)

    def test_changed_sibling_source_fails_before_its_model_call(self):
        self.current["lt-pack"] = {**self.current["lt-pack"], "source_sha256": "b" * 64}
        context = _Context(self.job)
        with self.assertRaises(NonRetryableJobError) as caught:
            jobs.run_durable_source_requirement_baseline(self.job, context)
        self.assertEqual(caught.exception.error_code, "source_requirement_joint_identity_changed")
        self.assertEqual(len(self.table.calls), 1)
        self.assertEqual(context.publication_count, 0)

    def test_mixed_scope_without_product_predecessor_never_loses_text_in_scope_only_result(self):
        self.current["lt-bom"].pop("source_requirement_base_bundle")
        context = _Context(self.job)
        with self.assertRaises(NonRetryableJobError) as caught:
            jobs.run_durable_source_requirement_baseline(self.job, context)
        self.assertEqual(caught.exception.error_code, "source_requirement_mixed_product_baseline_required")
        self.assertEqual(context.publication_count, 0)
        self.assertEqual(self.text.calls, [])

    def test_cancellation_after_text_keeps_candidates_but_publishes_nothing(self):
        class CancelAfterText(_Context):
            def cancellation_requested(context):
                return bool(self.text.calls)
        context = CancelAfterText(self.job)
        with self.assertRaises(NonRetryableJobError) as caught:
            jobs.run_durable_source_requirement_baseline(self.job, context)
        self.assertEqual(caught.exception.error_code, "source_requirement_baseline_cancelled_before_publication")
        self.assertEqual(context.publication_count, 0)
        self.assertEqual(len(self.table.calls), 2)
        self.assertEqual(len(self.text.calls), 1)

    def test_completed_checkpoint_rejects_provider_or_context_change(self):
        self.table.provider = "azure"
        self.text.fail = True
        context = _Context(self.job)
        with self.assertRaises(RuntimeError):
            jobs.run_durable_source_requirement_baseline(self.job, context)
        resumed = replace(self.job, progress=context.current_progress)
        for provider, semantic in (("aliyun", None), ("azure", {"new": "feedback"})):
            with self.subTest(provider=provider, semantic=semantic):
                self.table.provider = provider
                self.current["lt-bom"]["semantic_context"] = semantic
                context = _Context(resumed)
                with self.assertRaises(NonRetryableJobError) as caught:
                    jobs.run_durable_source_requirement_baseline(resumed, context)
                self.assertEqual(caught.exception.error_code, "source_requirement_joint_result_checkpoint_invalid")
                self.assertEqual(context.publication_count, 0)
                self.assertEqual(len(self.table.calls), 2)
