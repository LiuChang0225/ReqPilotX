from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.native_source_item_text_reference import (
    NativeSourceItemTextRunReceiptV1,
    SourceItemTextBatchCandidateV1,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.source_requirement_baseline import (
    ContextRowRole,
    RequirementKind,
    Requirementness,
    SourceItemTextBoundaryCandidateV1,
    SourceItemStructureOnlySpanV1,
    SourceItemTextSliceCandidateV1,
    SourceItemTextSpanV1,
    SourceRecordForm,
    SourceRequirementContextRowGroupV1,
    SourceRequirementRowGroupV1,
    build_source_item_text_scope_request,
    build_source_requirement_scope_request,
    canonical_json_sha256,
    compile_document_map_artifact,
    compile_source_requirement_baseline,
    grow_source_requirement_baseline,
)
from app.application.agent_workflow.source_requirement_jobs import (
    SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
    SOURCE_ITEM_TEXT_BASELINE_JOB_KIND,
    SourceItemScopeRevisionAgentContextV2,
    SourceItemScopeRevisionGuidanceV1,
    SourceItemScopeRevisionStartRequestV1,
    SourceItemScopeUndoStartRequestV1,
    SourceItemTextBaselineStartRequestV1,
    SourceRequirementProductBaselineArtifactV1,
    _current_affected_scope_boundaries,
    _current_scope_revision_context,
    _load_product_baseline_artifact,
    _job_projection,
    run_durable_source_item_text_baseline,
    run_durable_source_item_scope_revision,
    start_source_item_scope_undo,
    start_source_item_scope_revision,
    start_source_item_text_baseline,
)
from app.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    JobStatus,
    NonRetryableJobError,
)
from app.ports.source_item_text_executor import SourceItemTextExecutionV1
from app.adapters.agents.codex_app_server.native_source_item_text_windowed import (
    source_item_text_request_set_sha256,
)
from tests.test_source_item_text_app_server_executor import (
    _WindowedTextTransportFactory,
    _scenario,
)
from tests.test_source_requirement_baseline import (
    _candidate,
    _fragment,
    _record,
    _reference_with_text_node,
    _table_preview,
)


class SourceItemTextJobTests(unittest.TestCase):
    def test_start_enqueues_only_content_free_text_scope_identity(self) -> None:
        fixture = _fixture("a" * 64)
        executor = _FakeTextExecutor(fixture.batch, fixture.run_receipt)
        queue = _Queue(_job(fixture))
        container = SimpleNamespace(
            jobs=queue,
            source_item_text_executor=executor,
        )
        with (
            patch("app.application.runtime.RUNTIME_STATE.container", container),
            patch(
                "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                return_value=fixture.current(Path(__file__)),
            ),
        ):
            result = start_source_item_text_baseline(
                "project-1",
                "document-1",
                SourceItemTextBaselineStartRequestV1(
                    command_id="text-start-1",
                    expected_source_sha256=fixture.document_map.source_sha256,
                    expected_artifact_id="native-artifact-1",
                    node_ids=(fixture.request.node_id,),
                ),
                SimpleNamespace(headers={}),
            )

        self.assertEqual(result["status"], "PENDING")
        payload = queue.last_enqueue["payload"]
        self.assertEqual(payload["scope_ids"], [fixture.request.scope_id])
        self.assertEqual(payload["request_set_sha256"], fixture.request_set_sha256)
        self.assertEqual(payload["max_concurrency"], 2)
        self.assertNotIn("requests", payload)
        self.assertNotIn("供应商应提交", str(payload))

    def test_worker_publishes_growth_and_retry_reuses_private_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.pdf"
            source.write_bytes(b"synthetic-source")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            fixture = _fixture(source_sha256)
            store = LocalObjectStore(root / "objects")
            executor = _FakeTextExecutor(fixture.batch, fixture.run_receipt)
            current = fixture.current(source)
            context = _Context(_job(fixture))
            container = SimpleNamespace(
                object_store=store,
                source_item_text_executor=executor,
            )
            predecessor = {
                "baseline": fixture.previous_baseline,
                "receipt": fixture.previous_receipt,
                "product_run_receipts": (),
            }
            with (
                patch("app.application.runtime.RUNTIME_STATE.container", container),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                    return_value=current,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_effective_source_requirement_bundle",
                    return_value=predecessor,
                ),
            ):
                result = run_durable_source_item_text_baseline(context.job, context)

            pointer = result["baseline_revision_artifact"]
            product = _load_product_baseline_artifact(
                store,
                pointer,
                project_id="project-1",
                document_id="document-1",
            )
            self.assertEqual(product.baseline.revision, 2)
            self.assertEqual(len(product.baseline.records), 2)
            self.assertEqual(product.baseline.records[0].source_display_id, "SRC-0002")
            self.assertEqual(product.baseline.records[1].source_display_id, "SRC-0001")
            self.assertIsNotNone(product.growth_receipt)
            self.assertEqual(executor.calls, 1)
            self.assertIn(
                "source_item_text_result_checkpoint",
                context.current_progress,
            )

            replay_executor = _FailIfCalledTextExecutor()
            replay_context = _Context(
                replace(
                    context.job,
                    attempts=2,
                    generation=2,
                    progress=context.current_progress,
                )
            )
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_item_text_executor=replay_executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                    return_value=current,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_effective_source_requirement_bundle",
                    return_value=predecessor,
                ),
            ):
                replay = run_durable_source_item_text_baseline(
                    replay_context.job,
                    replay_context,
                )

            self.assertEqual(replay["candidate_sha256"], result["candidate_sha256"])
            self.assertEqual(replay_executor.calls, 0)

    def test_worker_resumes_only_missing_text_window_and_then_reuses_final_result(
        self,
    ) -> None:
        with _scenario() as scenario:
            store = LocalObjectStore(scenario.root / "job-objects")
            current = _windowed_current(scenario)
            failed_transport = _WindowedTextTransportFactory(
                always_invalid_window="text-window-02"
            )
            failed_executor = scenario.executor(failed_transport)
            job = _windowed_job(current, failed_executor)
            first_context = _Context(job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_item_text_executor=failed_executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                    return_value=current,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "text-window-02"):
                    run_durable_source_item_text_baseline(job, first_context)

            self.assertIn(
                "source_item_text_window_checkpoint",
                first_context.current_progress,
            )
            self.assertNotIn(
                "source_item_text_result_checkpoint",
                first_context.current_progress,
            )
            self.assertEqual(
                failed_transport.boundary_call_counts,
                {request.scope_id: 1 for request in scenario.requests},
            )
            self.assertEqual(
                failed_transport.classification_call_counts["text-window-01"],
                1,
            )

            resumed_transport = _WindowedTextTransportFactory()
            resumed_executor = scenario.executor(resumed_transport)
            retry_job = replace(
                job,
                attempts=2,
                generation=2,
                progress=first_context.current_progress,
            )
            retry_context = _Context(retry_job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_item_text_executor=resumed_executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                    return_value=current,
                ),
            ):
                result = run_durable_source_item_text_baseline(
                    retry_job,
                    retry_context,
                )

            self.assertEqual(result["status"], "SCOPE_COMPLETE")
            self.assertEqual(resumed_transport.boundary_call_counts, {})
            self.assertNotIn(
                "text-window-01",
                resumed_transport.classification_call_counts,
            )
            self.assertEqual(
                resumed_transport.classification_call_counts["text-window-02"],
                1,
            )
            self.assertIn(
                "source_item_text_result_checkpoint",
                retry_context.current_progress,
            )

            replay_transport = _WindowedTextTransportFactory()
            replay_executor = scenario.executor(replay_transport)
            replay_job = replace(
                job,
                attempts=3,
                generation=3,
                progress=retry_context.current_progress,
            )
            replay_context = _Context(replay_job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_item_text_executor=replay_executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                    return_value=current,
                ),
            ):
                replay = run_durable_source_item_text_baseline(
                    replay_job,
                    replay_context,
                )

            self.assertEqual(replay["candidate_sha256"], result["candidate_sha256"])
            self.assertEqual(replay_transport.total_calls, 0)

    def test_worker_rejects_tampered_text_window_checkpoint_without_agent(self) -> None:
        with _scenario() as scenario:
            store = LocalObjectStore(scenario.root / "tamper-objects")
            current = _windowed_current(scenario)
            failed_transport = _WindowedTextTransportFactory(
                always_invalid_window="text-window-02"
            )
            failed_executor = scenario.executor(failed_transport)
            job = _windowed_job(current, failed_executor)
            first_context = _Context(job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_item_text_executor=failed_executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                    return_value=current,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    run_durable_source_item_text_baseline(job, first_context)

            receipt = first_context.current_progress[
                "source_item_text_window_checkpoint"
            ]
            target = store.root / str(receipt["object_key"])
            target.write_bytes(b"{}")
            transport = _WindowedTextTransportFactory()
            executor = scenario.executor(transport)
            retry_job = replace(
                job,
                attempts=2,
                generation=2,
                progress=first_context.current_progress,
            )
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_item_text_executor=executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_text_scope_context",
                    return_value=current,
                ),
            ):
                with self.assertRaises(NonRetryableJobError) as caught:
                    run_durable_source_item_text_baseline(
                        retry_job,
                        _Context(retry_job),
                    )

            self.assertEqual(
                caught.exception.error_code,
                "source_item_text_window_checkpoint_invalid",
            )
            self.assertEqual(transport.total_calls, 0)

    def test_public_projection_hides_text_window_checkpoint_receipt(self) -> None:
        fixture = _fixture("a" * 64)
        job = replace(
            _job(fixture),
            progress={
                "stage": "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW_CHECKPOINTED",
                "message": "已保存 1/2 个分类窗口。",
                "strategy": "TEXT_BOUNDARY_WINDOWS",
                "completed_window_count": 1,
                "total_window_count": 2,
                "source_item_text_window_checkpoint": {
                    "object_key": "private/customer/text-window.json",
                    "accepted_window_ids": ["text-window-01"],
                },
            },
        )

        projection = _job_projection(job)
        serialized = json.dumps(projection, ensure_ascii=False)

        self.assertEqual(projection["progress"]["completed_window_count"], 1)
        self.assertNotIn("source_item_text_window_checkpoint", serialized)
        self.assertNotIn("private/customer", serialized)

    def test_scope_revision_start_enqueues_only_feedback_and_scope_identities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source.pdf"
            source.write_bytes(b"synthetic-source")
            fixture = _fixture(hashlib.sha256(source.read_bytes()).hexdigest())
            current, _replacement_batch, _replacement_receipt = (
                _scope_revision_fixture(fixture, source)
            )
            executor = _FakeTextExecutor(
                _replacement_batch,
                _replacement_receipt,
            )
            queue = _Queue(_scope_revision_job(current, executor))
            container = SimpleNamespace(
                jobs=queue,
                object_store=LocalObjectStore(Path(raw) / "objects"),
                source_item_text_executor=executor,
            )
            with (
                patch("app.application.runtime.RUNTIME_STATE.container", container),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_revision_context",
                    return_value=current,
                ),
            ):
                result = start_source_item_scope_revision(
                    "project-1",
                    "document-1",
                    SourceItemScopeRevisionStartRequestV1(
                        command_id="scope-revision-start-1",
                        expected_source_sha256=current["source_sha256"],
                        expected_document_map_artifact_sha256=current[
                            "document_map"
                        ].artifact_sha256,
                        expected_baseline_artifact_id=current[
                            "baseline"
                        ].artifact_id,
                        expected_baseline_artifact_sha256=current[
                            "baseline"
                        ].artifact_sha256,
                        expected_baseline_revision=current["baseline"].revision,
                        affected_scope_ids=current["affected_scope_ids"],
                        comparison_scope_ids=current["comparison_scope_ids"],
                        feedback_ids=current["feedback_ids"],
                    ),
                    SimpleNamespace(headers={}),
                )

        self.assertEqual(result["status"], "PENDING")
        payload = queue.last_enqueue["payload"]
        self.assertEqual(
            payload["schema_version"],
            "source-item-scope-revision-job/v1",
        )
        self.assertEqual(payload["affected_scope_ids"], list(current["affected_scope_ids"]))
        self.assertEqual(payload["feedback_ids"], list(current["feedback_ids"]))
        self.assertEqual(payload["max_concurrency"], 2)
        self.assertNotIn("feedbacks", payload)
        self.assertNotIn("current_affected_scopes", payload)
        self.assertNotIn("message", payload)
        self.assertNotIn("移除重复标题", str(payload))

    def test_scope_revision_context_binds_feedback_and_read_only_comparison(
        self,
    ) -> None:
        with _scenario() as scenario:
            execution = scenario.executor(_WindowedTextTransportFactory()).execute(
                scenario.task,
                cancellation_requested=lambda: False,
            )
            batch = SourceItemTextBatchCandidateV1.model_validate(execution.batch)
            baseline, receipt = compile_source_requirement_baseline(
                document_map=scenario.document_map,
                requests=(),
                candidates=(),
                text_requests=scenario.requests,
                text_candidates=batch.candidates,
            )
            affected_scope = scenario.requests[0].scope_id
            comparison_scope = scenario.requests[1].scope_id
            affected_record = next(
                item
                for item in baseline.records
                if (item.logical_table_scope_id or f"text:{item.node_id}")
                == affected_scope
            )
            feedback_id = "source-requirement-feedback-" + "e" * 32
            with (
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_reference_context",
                    return_value={
                        "public_reference": {},
                        "source_sha256": scenario.source_sha256,
                        "artifact_id": "native-artifact-context",
                        "document": {"id": "document-1"},
                    },
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.compile_document_map_artifact",
                    return_value=scenario.document_map,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_reference_bridge._load_base_reference",
                    return_value={"identity": "base"},
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_effective_source_requirement_bundle",
                    return_value={
                        "baseline": baseline,
                        "receipt": receipt,
                        "product_run_receipts": (),
                    },
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_reference_bridge._read_feedbacks",
                    return_value=[
                        {
                            "feedback_id": feedback_id,
                            "source_record_id": affected_record.source_record_id,
                            "message": "参照相邻条款，重新判断完整来源边界。",
                        }
                    ],
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_source_record_path",
                    return_value=scenario.source,
                ),
            ):
                current = _current_scope_revision_context(
                    project_id="project-1",
                    document_id="document-1",
                    affected_scope_ids=(affected_scope,),
                    comparison_scope_ids=(comparison_scope,),
                    feedback_ids=(feedback_id,),
                    container=SimpleNamespace(),
                )
                automatic = _current_scope_revision_context(
                    project_id="project-1",
                    document_id="document-1",
                    affected_scope_ids=(comparison_scope,),
                    comparison_scope_ids=(affected_scope,),
                    feedback_ids=(),
                    container=SimpleNamespace(),
                )

        self.assertEqual(current["affected_scope_ids"], (affected_scope,))
        self.assertEqual(current["comparison_scope_ids"], (comparison_scope,))
        self.assertEqual(
            tuple(item.scope_id for item in current["comparison_requests"]),
            (comparison_scope,),
        )
        self.assertEqual(
            current["agent_context"].guidance_items[0].guidance_id,
            feedback_id,
        )
        self.assertEqual(
            tuple(
                item.scope_id
                for item in current["agent_context"].current_affected_scopes
            ),
            (affected_scope,),
        )
        self.assertEqual(
            current["agent_context"].current_affected_scopes[0].records[0].source_record_id,
            affected_record.source_record_id,
        )
        self.assertEqual(
            current["agent_context"].current_affected_scopes[0].records[0].text_spans,
            tuple(
                SourceItemTextSpanV1(
                    content_id=item.content_id,
                    start_offset=item.start_offset,
                    end_offset=item.end_offset,
                )
                for item in affected_record.text_bindings
            ),
        )
        self.assertNotEqual(
            current["request_set_sha256"],
            canonical_json_sha256(
                [
                    item.model_dump(mode="json")
                    for item in current["affected_requests"]
                ]
            ),
        )
        self.assertEqual(automatic["feedback_ids"], ())
        self.assertEqual(
            automatic["agent_context"].guidance_items[0].origin,
            "HOST_REVIEW_REQUEST",
        )
        self.assertTrue(
            automatic["guidance_ids"][0].startswith("source-item-scope-review-")
        )

    def test_scope_revision_worker_publishes_tombstone_and_preserves_peer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.pdf"
            source.write_bytes(b"synthetic-source")
            fixture = _fixture(hashlib.sha256(source.read_bytes()).hexdigest())
            current, replacement_batch, replacement_receipt = (
                _scope_revision_fixture(fixture, source)
            )
            store = LocalObjectStore(root / "objects")
            executor = _FakeTextExecutor(replacement_batch, replacement_receipt)
            job = _scope_revision_job(current, executor)
            context = _Context(job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        jobs=SimpleNamespace(),
                        object_store=store,
                        source_item_text_executor=executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_revision_context",
                    return_value=current,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_reference_bridge._carry_feedbacks_to_baseline",
                ),
            ):
                result = run_durable_source_item_scope_revision(job, context)

            product = _load_product_baseline_artifact(
                store,
                result["baseline_revision_artifact"],
                project_id="project-1",
                document_id="document-1",
            )

        self.assertEqual(result["status"], "REVISION_COMPLETE")
        self.assertEqual(product.baseline.revision, current["baseline"].revision + 1)
        self.assertIsNotNone(product.scope_revision_receipt)
        revision_receipt = product.scope_revision_receipt
        self.assertEqual(len(revision_receipt.tombstoned_identities), 1)
        self.assertEqual(revision_receipt.created_identities, ())
        self.assertEqual(
            revision_receipt.successor_relations[0].relation,
            "STRUCTURE_ONLY",
        )
        unaffected_before = next(
            item
            for item in current["baseline"].records
            if item.node_id == "bom-node"
        )
        unaffected_after = next(
            item for item in product.baseline.records if item.node_id == "bom-node"
        )
        self.assertEqual(
            unaffected_before.model_dump(mode="json", exclude={"source_order"}),
            unaffected_after.model_dump(mode="json", exclude={"source_order"}),
        )
        self.assertEqual(executor.last_task.execution_context, current["agent_context"].model_dump(mode="json"))
        public = _job_projection(
            replace(job, status=JobStatus.SUCCEEDED, result=result)
        )
        self.assertEqual(public["result"]["revision_summary"]["tombstoned_count"], 1)
        self.assertNotIn("feedbacks", json.dumps(public, ensure_ascii=False))

    def test_scope_undo_appends_revision_and_restores_direct_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.pdf"
            source.write_bytes(b"synthetic-source")
            fixture = _fixture(hashlib.sha256(source.read_bytes()).hexdigest())
            current, replacement_batch, replacement_receipt = (
                _scope_revision_fixture(fixture, source)
            )
            store = LocalObjectStore(root / "objects")
            executor = _FakeTextExecutor(replacement_batch, replacement_receipt)
            revision_job = _scope_revision_job(current, executor)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        jobs=SimpleNamespace(),
                        object_store=store,
                        source_item_text_executor=executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_revision_context",
                    return_value=current,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_reference_bridge._carry_feedbacks_to_baseline",
                ),
            ):
                revision_result = run_durable_source_item_scope_revision(
                    revision_job,
                    _Context(revision_job),
                )
            revised_product = _load_product_baseline_artifact(
                store,
                revision_result["baseline_revision_artifact"],
                project_id="project-1",
                document_id="document-1",
            )
            undo_current = {
                "document": {"id": "document-1"},
                "source_sha256": current["source_sha256"],
                "document_map": current["document_map"],
                "baseline": revised_product.baseline,
                "validation_receipt": revised_product.validation_receipt,
                "scope_revision_receipt": revised_product.scope_revision_receipt,
                "affected_scope_ids": revised_product.scope_revision_receipt.affected_scope_ids,
                "comparison_scope_ids": revised_product.scope_revision_receipt.comparison_scope_ids,
                "feedback_ids": revised_product.scope_revision_receipt.feedback_ids,
                "guidance_ids": revised_product.scope_revision_receipt.feedback_ids,
                "undo_target": current["product_baseline_artifact"],
                "product_run_receipts": revised_product.product_run_receipts,
                "product_baseline_pointer": revision_result[
                    "baseline_revision_artifact"
                ],
            }
            undo_job = _scope_undo_job(undo_current)
            undo_queue = _Queue(undo_job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        jobs=undo_queue,
                        object_store=store,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_undo_context",
                    return_value=undo_current,
                ),
            ):
                undo_start = start_source_item_scope_undo(
                    "project-1",
                    "document-1",
                    SourceItemScopeUndoStartRequestV1(
                        command_id="scope-undo-start-1",
                        expected_source_sha256=undo_current["source_sha256"],
                        expected_document_map_artifact_sha256=undo_current[
                            "document_map"
                        ].artifact_sha256,
                        expected_baseline_artifact_id=undo_current[
                            "baseline"
                        ].artifact_id,
                        expected_baseline_artifact_sha256=undo_current[
                            "baseline"
                        ].artifact_sha256,
                        expected_baseline_revision=undo_current["baseline"].revision,
                        expected_scope_revision_receipt_sha256=undo_current[
                            "scope_revision_receipt"
                        ].receipt_sha256,
                    ),
                    SimpleNamespace(headers={}),
                )
            self.assertEqual(undo_start["status"], "PENDING")
            self.assertNotIn("undo_target", undo_queue.last_enqueue["payload"])
            undo_context = _Context(undo_job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        jobs=SimpleNamespace(),
                        object_store=store,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_undo_context",
                    return_value=undo_current,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_reference_bridge._carry_feedbacks_to_baseline",
                ),
            ):
                undo_result = run_durable_source_item_scope_revision(
                    undo_job,
                    undo_context,
                )
            restored_product = _load_product_baseline_artifact(
                store,
                undo_result["baseline_revision_artifact"],
                project_id="project-1",
                document_id="document-1",
            )

        self.assertEqual(
            restored_product.baseline.revision,
            revised_product.baseline.revision + 1,
        )
        self.assertEqual(
            restored_product.scope_revision_receipt.operation.value,
            "UNDO",
        )
        self.assertEqual(
            {
                item.source_record_id: item.source_display_id
                for item in restored_product.baseline.records
            },
            {
                item.source_record_id: item.source_display_id
                for item in current["baseline"].records
            },
        )
        self.assertEqual(
            restored_product.scope_revision_receipt.source_display_id_high_watermark_after,
            revised_product.scope_revision_receipt.source_display_id_high_watermark_after,
        )
        self.assertEqual(
            undo_result["predecessor_baseline_revision_artifact"],
            revision_result["baseline_revision_artifact"],
        )
        self.assertEqual(executor.calls, 1)


class _Fixture:
    def __init__(
        self,
        *,
        document_map,
        request,
        batch,
        run_receipt,
        previous_baseline,
        previous_receipt,
    ) -> None:  # type: ignore[no-untyped-def]
        self.document_map = document_map
        self.request = request
        self.batch = batch
        self.run_receipt = run_receipt
        self.previous_baseline = previous_baseline
        self.previous_receipt = previous_receipt
        self.request_set_sha256 = run_receipt.request_set_sha256

    def current(self, source_path: Path) -> dict[str, object]:
        return {
            "document": {"id": "document-1"},
            "source_path": source_path.resolve(),
            "source_sha256": self.document_map.source_sha256,
            "native_artifact_id": "native-artifact-1",
            "native_projection_sha256": self.document_map.native_projection_sha256,
            "document_map": self.document_map,
            "node_ids": (self.request.node_id,),
            "scope_ids": (self.request.scope_id,),
            "requests": (self.request,),
            "request_set_sha256": self.request_set_sha256,
            "batch_scope_id": self.request.scope_id,
            "source_requirement_base_bundle": {"identity": "base"},
        }


class _FakeTextExecutor:
    adapter_id = "fake-text-executor"
    adapter_version = "1.0.0"
    transport_kind = "FAKE"
    model_id = "gpt-5.6-sol"
    reasoning_effort = "high"

    def __init__(self, batch, run_receipt) -> None:  # type: ignore[no-untyped-def]
        self.batch = batch
        self.run_receipt = run_receipt
        self.calls = 0
        self.last_task = None

    def readiness_reason(self):  # type: ignore[no-untyped-def]
        return None

    def execute(
        self,
        task,
        *,
        cancellation_requested,
        resume_checkpoint=None,
        checkpoint_sink=None,
    ):  # type: ignore[no-untyped-def]
        del resume_checkpoint, checkpoint_sink
        self.calls += 1
        self.last_task = task
        if cancellation_requested():
            raise RuntimeError("cancelled")
        return SourceItemTextExecutionV1(
            batch=self.batch.model_dump(mode="json"),
            run_receipt=self.run_receipt.model_dump(mode="json"),
        )


class _FailIfCalledTextExecutor(_FakeTextExecutor):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("checkpoint replay must not call Agent")


class _Context(JobExecutionContext):
    def __init__(self, job: DurableJob) -> None:
        self.job = job
        self.current_progress = dict(job.progress)
        self.publication_count = 0

    def heartbeat(self, progress=None):  # type: ignore[no-untyped-def]
        if progress is not None:
            self.current_progress = dict(progress)
        return replace(self.job, progress=self.current_progress)

    def cancellation_requested(self) -> bool:
        return False

    def lease_fence(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(job_id=self.job.job_id, generation=1, worker_id="worker")

    def begin_publication(self):  # type: ignore[no-untyped-def]
        self.publication_count += 1
        return self.job


class _Queue:
    def __init__(self, job: DurableJob) -> None:
        self.job = job
        self.last_enqueue = None

    def get_latest_succeeded(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def enqueue(self, **kwargs):  # type: ignore[no-untyped-def]
        self.last_enqueue = kwargs
        return self.job


def _job(fixture: _Fixture) -> DurableJob:
    now = datetime.now(UTC)
    return DurableJob(
        job_id="job-source-item-text-1",
        kind=SOURCE_ITEM_TEXT_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id="document-1",
        idempotency_key="text-key-1",
        payload={
            "schema_version": "source-item-text-baseline-job/v1",
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": fixture.document_map.source_sha256,
            "native_artifact_id": "native-artifact-1",
            "native_projection_sha256": fixture.document_map.native_projection_sha256,
            "document_map_artifact_id": fixture.document_map.artifact_id,
            "document_map_artifact_sha256": fixture.document_map.artifact_sha256,
            "node_ids": [fixture.request.node_id],
            "scope_ids": [fixture.request.scope_id],
            "scope_id": fixture.request.scope_id,
            "request_set_sha256": fixture.request_set_sha256,
            "executor_id": _FakeTextExecutor.adapter_id,
            "executor_version": _FakeTextExecutor.adapter_version,
            "transport_kind": _FakeTextExecutor.transport_kind,
            "model_id": _FakeTextExecutor.model_id,
            "reasoning_effort": _FakeTextExecutor.reasoning_effort,
            "timeout_seconds": 1200,
            "max_concurrency": 2,
            "command_id": "text-start-1",
        },
        status=JobStatus.PENDING,
        priority=23,
        attempts=1,
        max_attempts=3,
        generation=1,
        cancel_requested=False,
        lease_owner="worker-1",
        lease_expires_at=now,
        available_at=now,
        created_at=now,
        updated_at=now,
        progress={},
        result=None,
        error_code="",
        error_message="",
    )


def _windowed_current(scenario):  # type: ignore[no-untyped-def]
    request_set_sha256 = canonical_json_sha256(
        [item.model_dump(mode="json") for item in scenario.requests]
    )
    scope_ids = tuple(item.scope_id for item in scenario.requests)
    return {
        "document": {"id": "document-1"},
        "source_path": scenario.source.resolve(),
        "source_sha256": scenario.document_map.source_sha256,
        "native_artifact_id": "native-artifact-windowed",
        "native_projection_sha256": scenario.document_map.native_projection_sha256,
        "document_map": scenario.document_map,
        "node_ids": tuple(item.node_id for item in scenario.requests),
        "scope_ids": scope_ids,
        "requests": scenario.requests,
        "request_set_sha256": request_set_sha256,
        "batch_scope_id": f"text-batch-{request_set_sha256[:32]}",
    }


def _windowed_job(current, executor) -> DurableJob:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return DurableJob(
        job_id="job-source-item-text-windowed",
        kind=SOURCE_ITEM_TEXT_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id="document-1",
        idempotency_key="text-windowed-key-1",
        payload={
            "schema_version": "source-item-text-baseline-job/v1",
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": current["source_sha256"],
            "native_artifact_id": current["native_artifact_id"],
            "native_projection_sha256": current["native_projection_sha256"],
            "document_map_artifact_id": current["document_map"].artifact_id,
            "document_map_artifact_sha256": current["document_map"].artifact_sha256,
            "node_ids": list(current["node_ids"]),
            "scope_ids": list(current["scope_ids"]),
            "scope_id": current["batch_scope_id"],
            "request_set_sha256": current["request_set_sha256"],
            "executor_id": executor.adapter_id,
            "executor_version": executor.adapter_version,
            "transport_kind": executor.transport_kind,
            "model_id": executor.model_id,
            "reasoning_effort": executor.reasoning_effort,
            "timeout_seconds": 1200,
            "max_concurrency": 2,
            "command_id": "text-windowed-start-1",
        },
        status=JobStatus.PENDING,
        priority=23,
        attempts=1,
        max_attempts=3,
        generation=1,
        cancel_requested=False,
        lease_owner="worker-1",
        lease_expires_at=now,
        available_at=now,
        created_at=now,
        updated_at=now,
        progress={},
        result=None,
        error_code="",
        error_message="",
    )


def _scope_revision_fixture(
    fixture: _Fixture,
    source_path: Path,
):  # type: ignore[no-untyped-def]
    current_baseline, current_receipt, growth_receipt = (
        grow_source_requirement_baseline(
            document_map=fixture.document_map,
            previous_artifact=fixture.previous_baseline,
            previous_validation_receipt=fixture.previous_receipt,
            text_requests=(fixture.request,),
            text_candidates=(fixture.batch.candidates[0],),
        )
    )
    current_record = next(
        item for item in current_baseline.records if item.node_id == fixture.request.node_id
    )
    feedback = SourceItemScopeRevisionGuidanceV1(
        guidance_id="source-requirement-feedback-" + "a" * 32,
        origin="USER_FEEDBACK",
        source_record_id=current_record.source_record_id,
        scope_id=fixture.request.scope_id,
        message="该内容是重复可见标题，请依据原文结构移除条目副本。",
    )
    context_values = {
        "source_sha256": fixture.document_map.source_sha256,
        "document_map_artifact_id": fixture.document_map.artifact_id,
        "document_map_artifact_sha256": fixture.document_map.artifact_sha256,
        "current_baseline_artifact_id": current_baseline.artifact_id,
        "current_baseline_artifact_sha256": current_baseline.artifact_sha256,
        "current_baseline_revision": current_baseline.revision,
        "affected_scope_ids": (fixture.request.scope_id,),
        "comparison_scope_ids": (),
        "guidance_items": (feedback,),
        "current_affected_scopes": _current_affected_scope_boundaries(
            baseline=current_baseline,
            affected_scope_ids=(fixture.request.scope_id,),
        ),
        "comparison_scopes": (),
    }
    context_draft = SourceItemScopeRevisionAgentContextV2.model_construct(
        **context_values,
        context_sha256="0" * 64,
    )
    agent_context = SourceItemScopeRevisionAgentContextV2(
        **context_values,
        context_sha256=canonical_json_sha256(
            context_draft.model_dump(mode="json", exclude={"context_sha256"})
        ),
    )
    source_content = fixture.request.source_content[0]
    replacement_candidate = SourceItemTextSliceCandidateV1(
        source_sha256=fixture.document_map.source_sha256,
        document_map_artifact_id=fixture.document_map.artifact_id,
        document_map_artifact_sha256=fixture.document_map.artifact_sha256,
        request_sha256=fixture.request.request_sha256,
        scope_id=fixture.request.scope_id,
        items=(),
        structure_only_spans=(
            SourceItemStructureOnlySpanV1(
                text_span=SourceItemTextSpanV1(
                    content_id=source_content.content_id,
                    start_offset=0,
                    end_offset=len(source_content.text),
                ),
                reason_codes=("source_hierarchy_title",),
            ),
        ),
    )
    replacement_batch = SourceItemTextBatchCandidateV1(
        source_sha256=fixture.document_map.source_sha256,
        document_map_artifact_id=fixture.document_map.artifact_id,
        document_map_artifact_sha256=fixture.document_map.artifact_sha256,
        candidates=(replacement_candidate,),
    )
    request_set_sha256 = source_item_text_request_set_sha256(
        (fixture.request,),
        execution_context=agent_context.model_dump(mode="json"),
    )
    replacement_receipt = NativeSourceItemTextRunReceiptV1(
        source_sha256=fixture.document_map.source_sha256,
        document_map_artifact_id=fixture.document_map.artifact_id,
        document_map_artifact_sha256=fixture.document_map.artifact_sha256,
        request_set_sha256=request_set_sha256,
        model_id="gpt-5.6-sol",
        reasoning_effort="high",
        codex_version="codex-cli 0.151.0",
        kit_version="0.15.2",
        kit_bundle_sha256="c" * 64,
        visual_source_sha256=fixture.document_map.source_sha256,
        visual_page_numbers=(1,),
        elapsed_milliseconds=1000,
        event_counts=("event_counts:turn.completed=1",),
        tool_names=(),
        scope_count=1,
        scope_ids=(fixture.request.scope_id,),
        source_item_count=0,
        structure_only_span_count=1,
        candidate_sha256=canonical_json_sha256(
            replacement_batch.model_dump(mode="json")
        ),
    )
    product_values = {
        "source_sha256": fixture.document_map.source_sha256,
        "document_map_artifact_id": fixture.document_map.artifact_id,
        "document_map_artifact_sha256": fixture.document_map.artifact_sha256,
        "baseline": current_baseline,
        "validation_receipt": current_receipt,
        "append_receipt": None,
        "growth_receipt": growth_receipt,
        "product_run_receipts": (fixture.run_receipt,),
    }
    product_draft = SourceRequirementProductBaselineArtifactV1.model_construct(
        **product_values,
        artifact_sha256="0" * 64,
    )
    product_artifact = SourceRequirementProductBaselineArtifactV1(
        **product_values,
        artifact_sha256=canonical_json_sha256(
            product_draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )
    product_pointer = {
        "object_key": "runtime/agent-source-requirement-baselines/predecessor.json",
        "object_sha256": "f" * 64,
        "artifact_sha256": product_artifact.artifact_sha256,
        "baseline_artifact_id": current_baseline.artifact_id,
        "baseline_artifact_sha256": current_baseline.artifact_sha256,
        "baseline_revision": current_baseline.revision,
        "record_count": len(current_baseline.records),
        "size": 1,
        "media_type": "application/vnd.reqpilotx.source-requirement-product-baseline+json",
    }
    current = {
        "document": {"id": "document-1"},
        "source_path": source_path.resolve(),
        "source_sha256": fixture.document_map.source_sha256,
        "native_artifact_id": "native-artifact-revision",
        "native_projection_sha256": fixture.document_map.native_projection_sha256,
        "document_map": fixture.document_map,
        "baseline": current_baseline,
        "validation_receipt": current_receipt,
        "affected_scope_ids": (fixture.request.scope_id,),
        "comparison_scope_ids": (),
        "feedback_ids": (feedback.guidance_id,),
        "guidance_ids": (feedback.guidance_id,),
        "affected_requests": (fixture.request,),
        "comparison_requests": (),
        "agent_context": agent_context,
        "request_set_sha256": request_set_sha256,
        "product_run_receipts": (fixture.run_receipt,),
        "product_baseline_artifact": product_artifact,
        "product_baseline_pointer": product_pointer,
    }
    return current, replacement_batch, replacement_receipt


def _scope_revision_job(current, executor) -> DurableJob:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return DurableJob(
        job_id="job-source-item-scope-revision",
        kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id="document-1",
        idempotency_key="scope-revision-key-1",
        payload={
            "schema_version": "source-item-scope-revision-job/v1",
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": current["source_sha256"],
            "native_artifact_id": current["native_artifact_id"],
            "native_projection_sha256": current["native_projection_sha256"],
            "document_map_artifact_id": current["document_map"].artifact_id,
            "document_map_artifact_sha256": current["document_map"].artifact_sha256,
            "current_baseline_artifact_id": current["baseline"].artifact_id,
            "current_baseline_artifact_sha256": current["baseline"].artifact_sha256,
            "current_baseline_revision": current["baseline"].revision,
            "current_validation_receipt_sha256": current[
                "validation_receipt"
            ].receipt_sha256,
            "affected_scope_ids": list(current["affected_scope_ids"]),
            "scope_ids": list(current["affected_scope_ids"]),
            "scope_id": current["affected_scope_ids"][0],
            "comparison_scope_ids": list(current["comparison_scope_ids"]),
            "feedback_ids": list(current["feedback_ids"]),
            "guidance_ids": list(current["guidance_ids"]),
            "node_ids": [item.node_id for item in current["affected_requests"]],
            "request_set_sha256": current["request_set_sha256"],
            "agent_context_sha256": current["agent_context"].context_sha256,
            "executor_id": executor.adapter_id,
            "executor_version": executor.adapter_version,
            "transport_kind": executor.transport_kind,
            "model_id": executor.model_id,
            "reasoning_effort": executor.reasoning_effort,
            "timeout_seconds": 1200,
            "max_concurrency": 2,
            "command_id": "scope-revision-start-1",
        },
        status=JobStatus.PENDING,
        priority=24,
        attempts=1,
        max_attempts=3,
        generation=1,
        cancel_requested=False,
        lease_owner="worker-1",
        lease_expires_at=now,
        available_at=now,
        created_at=now,
        updated_at=now,
        progress={},
        result=None,
        error_code="",
        error_message="",
    )


def _scope_undo_job(current) -> DurableJob:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    receipt = current["scope_revision_receipt"]
    target = current["undo_target"].baseline
    return DurableJob(
        job_id="job-source-item-scope-undo",
        kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id="document-1",
        idempotency_key="scope-undo-key-1",
        payload={
            "schema_version": "source-item-scope-undo-job/v1",
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": current["source_sha256"],
            "document_map_artifact_id": current["document_map"].artifact_id,
            "document_map_artifact_sha256": current["document_map"].artifact_sha256,
            "current_baseline_artifact_id": current["baseline"].artifact_id,
            "current_baseline_artifact_sha256": current["baseline"].artifact_sha256,
            "current_baseline_revision": current["baseline"].revision,
            "current_validation_receipt_sha256": current[
                "validation_receipt"
            ].receipt_sha256,
            "scope_revision_receipt_sha256": receipt.receipt_sha256,
            "affected_scope_ids": list(current["affected_scope_ids"]),
            "scope_ids": list(current["affected_scope_ids"]),
            "scope_id": current["affected_scope_ids"][0],
            "comparison_scope_ids": list(current["comparison_scope_ids"]),
            "feedback_ids": list(current["feedback_ids"]),
            "guidance_ids": list(current["guidance_ids"]),
            "undo_target_artifact_id": target.artifact_id,
            "undo_target_artifact_sha256": target.artifact_sha256,
            "undo_target_artifact_revision": target.revision,
            "command_id": "scope-undo-start-1",
        },
        status=JobStatus.PENDING,
        priority=24,
        attempts=1,
        max_attempts=3,
        generation=1,
        cancel_requested=False,
        lease_owner="worker-1",
        lease_expires_at=now,
        available_at=now,
        created_at=now,
        updated_at=now,
        progress={},
        result=None,
        error_code="",
        error_message="",
    )


def _fixture(source_sha256: str) -> _Fixture:
    text = "1.1 供应商应提交验证报告。"
    reference = _reference_with_text_node(
        node_id="clause-1.1",
        label="1.1",
        title="供应商应提交验证报告。",
        contents=(("content-1.1", text, True),),
    )
    reference["source_sha256"] = source_sha256
    document_map = compile_document_map_artifact(reference)
    preview = _table_preview(
        scope_id="lt-bom",
        title="产品信息",
        fragments=(
            _fragment(
                "bom-fragment",
                "T0001",
                page=2,
                rows=(
                    ("序号", "名称"),
                    ("1", "后减振器总成"),
                    ("2", "后减振器子件"),
                    ("", "前减振器"),
                    ("1", "前减振器总成"),
                ),
            ),
        ),
    )
    preview["source_sha256"] = source_sha256
    bom_request = build_source_requirement_scope_request(document_map, preview)
    bom_candidate = _candidate(
        bom_request,
        records=(
            _record(
                "bom-record",
                "产品结构",
                SourceRecordForm.TABLE_BRANCH,
                (
                    SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=1, row_end=5
                    ),
                ),
            ),
        ),
        context=(
            SourceRequirementContextRowGroupV1(
                role=ContextRowRole.HEADER,
                row_group=SourceRequirementRowGroupV1(
                    fragment_id="bom-fragment", row_start=0, row_end=1
                ),
                reason_codes=("shared_table_header",),
            ),
        ),
    )
    previous_baseline, previous_receipt = compile_source_requirement_baseline(
        document_map=document_map,
        requests=(bom_request,),
        candidates=(bom_candidate,),
    )
    request = build_source_item_text_scope_request(
        document_map,
        node_id="clause-1.1",
    )
    candidate = SourceItemTextSliceCandidateV1(
        source_sha256=source_sha256,
        document_map_artifact_id=document_map.artifact_id,
        document_map_artifact_sha256=document_map.artifact_sha256,
        request_sha256=request.request_sha256,
        scope_id=request.scope_id,
        items=(
            SourceItemTextBoundaryCandidateV1(
                item_key="verification-report",
                label="1.1 提交验证报告",
                source_form=SourceRecordForm.NUMBERED_CLAUSE,
                text_spans=(
                    SourceItemTextSpanV1(
                        content_id="content-1.1",
                        start_offset=0,
                        end_offset=len(text),
                    ),
                ),
                requirementness=Requirementness.REQUIREMENT,
                requirement_kinds=(RequirementKind.CONSTRAINT,),
                non_requirement_role=None,
                confidence=0.98,
                decision_summary="完整编号条款规定供应商提交验证报告。",
                reason_codes=("supplier_deliverable_constraint",),
            ),
        ),
    )
    batch = SourceItemTextBatchCandidateV1(
        source_sha256=source_sha256,
        document_map_artifact_id=document_map.artifact_id,
        document_map_artifact_sha256=document_map.artifact_sha256,
        candidates=(candidate,),
    )
    request_set_sha256 = canonical_json_sha256([request.model_dump(mode="json")])
    run_receipt = NativeSourceItemTextRunReceiptV1(
        source_sha256=source_sha256,
        document_map_artifact_id=document_map.artifact_id,
        document_map_artifact_sha256=document_map.artifact_sha256,
        request_set_sha256=request_set_sha256,
        model_id="gpt-5.6-sol",
        reasoning_effort="high",
        codex_version="codex-cli 0.151.0",
        kit_version="0.15.2",
        kit_bundle_sha256="b" * 64,
        visual_source_sha256=source_sha256,
        visual_page_numbers=(1,),
        elapsed_milliseconds=1000,
        event_counts=("event_counts:turn.completed=1",),
        tool_names=(),
        scope_count=1,
        scope_ids=(request.scope_id,),
        source_item_count=1,
        structure_only_span_count=0,
        candidate_sha256=canonical_json_sha256(batch.model_dump(mode="json")),
    )
    return _Fixture(
        document_map=document_map,
        request=request,
        batch=batch,
        run_receipt=run_receipt,
        previous_baseline=previous_baseline,
        previous_receipt=previous_receipt,
    )


if __name__ == "__main__":
    unittest.main()
