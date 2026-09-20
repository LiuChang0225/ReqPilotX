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
import json
import tempfile
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    NativeSourceRequirementWindowedCheckpointV1,
    run_native_source_requirement_windowed_reference,
)
from app.adapters.agents.codex_app_server.native_source_requirement_reference import (
    NativeSourceRequirementRunReceiptV1,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.source_requirement_jobs import (
    SOURCE_REQUIREMENT_BASELINE_JOB_KIND,
    SourceRequirementBaselineStartRequestV1,
    SourceRequirementScopeResultArtifactV1,
    _append_product_baseline_artifact,
    _current_scope_context,
    get_source_requirement_baseline_run_status,
    run_durable_source_requirement_baseline,
    start_source_requirement_baseline,
)
from app.application.agent_workflow.source_requirement_baseline import (
    ContextRowRole,
    SourceRecordForm,
    SourceRequirementContextRowGroupV1,
    SourceRequirementRowGroupV1,
    canonical_json_sha256,
    compile_source_requirement_baseline,
)
from app.application.errors import ApplicationError
from app.ports.job_queue import DurableJob, JobExecutionContext, JobStatus
from app.ports.source_requirement_window_executor import (
    SourceRequirementWindowExecutionV1,
)
from tests.test_native_source_requirement_windowed_reference import (
    _FakeWindowedCommandRunner,
    _windowed_fixture,
)
from tests.test_source_requirement_baseline import (
    SourceRequirementBaselineTests,
    _candidate,
    _public_reference,
    _record,
)


class SourceRequirementJobTests(unittest.TestCase):
    def test_product_artifact_falls_back_to_source_order_growth(self) -> None:
        fixture = SourceRequirementBaselineTests()
        fixture.setUp()
        dvp_candidate = _candidate(
            fixture.dvp_request,
            records=(
                _record(
                    "dvp-one-record",
                    "1 晚页试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        previous, previous_validation = compile_source_requirement_baseline(
            document_map=fixture.document_map,
            requests=(fixture.dvp_request,),
            candidates=(dvp_candidate,),
        )
        bom_candidate = _candidate(
            fixture.bom_request,
            records=(
                _record(
                    "bom-early-record",
                    "早页 BOM",
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
        run_receipt = NativeSourceRequirementRunReceiptV1(
            source_sha256=fixture.bom_request.source_sha256,
            document_map_artifact_id=(
                fixture.bom_request.document_map_artifact_id
            ),
            document_map_artifact_sha256=(
                fixture.bom_request.document_map_artifact_sha256
            ),
            request_sha256=fixture.bom_request.request_sha256,
            logical_table_scope_id=fixture.bom_request.logical_table_scope_id,
            model_id="gpt-5.6-sol",
            reasoning_effort="high",
            codex_version="codex-cli test",
            elapsed_milliseconds=1,
            event_counts=(),
            tool_names=(),
            record_count=1,
            context_row_group_count=1,
            candidate_sha256=canonical_json_sha256(
                bom_candidate.model_dump(mode="json")
            ),
        )

        product = _append_product_baseline_artifact(
            document_map=fixture.document_map,
            previous_artifact=previous,
            previous_validation_receipt=previous_validation,
            request=fixture.bom_request,
            candidate=bom_candidate,
            run_receipt=run_receipt,
            previous_product_run_receipts=(),
        )

        self.assertIsNone(product.append_receipt)
        self.assertIsNotNone(product.growth_receipt)
        self.assertEqual([item.label for item in product.baseline.records], [
            "早页 BOM",
            "1 晚页试验",
        ])
        self.assertEqual(
            product.baseline.records[1].source_record_id,
            previous.records[0].source_record_id,
        )
        self.assertEqual(
            product.baseline.records[1].source_display_id,
            previous.records[0].source_display_id,
        )

    def test_start_rejects_protocol_drift_before_enqueue(self) -> None:
        document_map, request = _windowed_fixture()
        executor = _FakeWindowExecutor(
            runtime_reason="CODEX_BINARY_HASH_MISMATCH"
        )
        queue = _Queue(_job(document_map, request, status=JobStatus.PENDING))
        with patch(
            "app.application.runtime.RUNTIME_STATE.container",
            SimpleNamespace(
                jobs=queue,
                source_requirement_window_executor=executor,
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                start_source_requirement_baseline(
                    "project-1",
                    "document-1",
                    SourceRequirementBaselineStartRequestV1(
                        command_id="source-baseline-start-drifted",
                        expected_source_sha256=document_map.source_sha256,
                        expected_artifact_id="native-artifact-1",
                        logical_table_scope_id=request.logical_table_scope_id,
                    ),
                    _Response(),
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail["reason_code"],
            "CODEX_BINARY_HASH_MISMATCH",
        )
        self.assertEqual(queue.enqueue_calls, 0)

    def test_scope_context_materializes_the_same_logical_preview_as_the_ui(self) -> None:
        document_map, request = _windowed_fixture()
        public_reference = _public_reference()
        public_reference["logical_tables"][0]["fragments"][0]["row_range"] = [
            0,
            7,
        ]
        preview = {
            "schema_version": "native-structure-table-preview/v1",
            "artifact_id": "native-reference-test",
            "source_sha256": request.source_sha256,
            "logical_table_scope_id": request.logical_table_scope_id,
            "title": request.logical_table_title,
            "fragments": [
                item.model_dump(mode="json") for item in request.fragments
            ],
            "summary": {
                "fragment_count": len(request.fragments),
                "row_count": sum(item.row_count for item in request.fragments),
                "cell_count": sum(len(item.cells) for item in request.fragments),
            },
        }
        flat_physical_fragment = request.fragments[0].model_dump(mode="json") | {
            "logical_table_scope_id": request.logical_table_scope_id,
        }
        current = {
            "document": {"id": "document-1"},
            "source_sha256": request.source_sha256,
            "artifact_id": "native-reference-test",
            "public_reference": public_reference,
            "table_previews": (flat_physical_fragment,),
            "bundle": {"identity": "current-reference"},
            "handoff_attempts": (),
        }
        with (
            patch(
                "app.application.agent_workflow.source_requirement_jobs._current_reference_context",
                return_value=current,
            ),
            patch(
                "app.application.agent_workflow.source_requirement_jobs._verified_projection_operations",
                return_value=(),
            ) as operations,
            patch(
                "app.application.agent_workflow.source_requirement_jobs.materialize_native_structure_table_preview",
                return_value=preview,
            ) as materialize,
            patch(
                "app.application.agent_workflow.source_requirement_jobs.resolve_source_record_path",
                return_value=Path(__file__),
            ),
            patch(
                    "app.application.agent_workflow.source_requirement_reference_bridge._load_base_reference",
                return_value={"identity": "source-requirement-base"},
            ),
        ):
            result = _current_scope_context(
                project_id="project-1",
                document_id="document-1",
                logical_table_scope_id=request.logical_table_scope_id,
            )

        self.assertEqual(result["document_map"], document_map)
        self.assertEqual(result["request"], request)
        self.assertEqual(
            result["source_requirement_base_bundle"],
            {"identity": "source-requirement-base"},
        )
        operations.assert_called_once_with(())
        materialize.assert_called_once_with(
            bundle=current["bundle"],
            source_sha256=request.source_sha256,
            scope_id=request.logical_table_scope_id,
            projection_operations=(),
            source_path=Path(__file__).resolve(),
        )

    def test_start_enqueues_content_free_scope_identity(self) -> None:
        document_map, request = _windowed_fixture()
        executor = _FakeWindowExecutor()
        queue = _Queue(_job(document_map, request, status=JobStatus.PENDING))
        container = SimpleNamespace(
            jobs=queue,
            source_requirement_window_executor=executor,
        )
        with (
            patch(
                "app.application.runtime.RUNTIME_STATE.container",
                container,
            ),
            patch(
                "app.application.agent_workflow.source_requirement_jobs._current_scope_context",
                return_value=_current(document_map, request),
            ),
        ):
            result = start_source_requirement_baseline(
                "project-1",
                "document-1",
                SourceRequirementBaselineStartRequestV1(
                    command_id="source-baseline-start-1",
                    expected_source_sha256=document_map.source_sha256,
                    expected_artifact_id="native-artifact-1",
                    logical_table_scope_id=request.logical_table_scope_id,
                ),
                _Response(),
            )

        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(queue.enqueue_calls, 1)
        payload = queue.last_enqueue["payload"]
        self.assertEqual(payload["request_sha256"], request.request_sha256)
        self.assertNotIn("request", payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Record 1", serialized)
        self.assertNotIn("Header", serialized)

    def test_worker_persists_private_scope_result_and_public_summary(self) -> None:
        document_map, request = _windowed_fixture()
        executor = _FakeWindowExecutor()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.pdf"
            source.write_bytes(b"synthetic")
            store = LocalObjectStore(root / "objects")
            context = _Context(_job(document_map, request))
            container = SimpleNamespace(
                object_store=store,
                source_requirement_window_executor=executor,
            )
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    container,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_context",
                    return_value=_current(document_map, request),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_source_record_path",
                    return_value=source,
                ),
            ):
                result = run_durable_source_requirement_baseline(
                    context.job,
                    context,
                )

            self.assertEqual(result["status"], "SCOPE_COMPLETE")
            self.assertEqual(result["record_count"], 6)
            self.assertEqual(context.publication_count, 1)
            self.assertIn(
                "source_requirement_window_checkpoint",
                context.current_progress,
            )
            self.assertEqual(context.current_progress["total_window_count"], 2)
            with store.open(result["private_artifact"]["object_key"]) as stream:
                artifact = SourceRequirementScopeResultArtifactV1.model_validate_json(
                    stream.read()
                )
            self.assertEqual(artifact.request.request_sha256, request.request_sha256)
            self.assertEqual(len(artifact.candidate.records), 6)
            self.assertNotIn("candidate", result)

            resumed_executor = _FakeWindowExecutor()
            retry_job = replace(
                context.job,
                attempts=2,
                generation=2,
                progress=context.current_progress,
            )
            retry_context = _Context(retry_job)
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_requirement_window_executor=resumed_executor,
                    ),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_context",
                    return_value=_current(document_map, request),
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_source_record_path",
                    return_value=source,
                ),
            ):
                replay = run_durable_source_requirement_baseline(
                    retry_job,
                    retry_context,
                )

            self.assertEqual(replay["candidate_sha256"], result["candidate_sha256"])
            self.assertEqual(resumed_executor.runner.boundary_calls, 0)
            self.assertEqual(resumed_executor.runner.classification_calls, 0)

    def test_retry_resumes_boundary_and_accepted_sibling_only(self) -> None:
        document_map, request = _windowed_fixture()
        first_executor = _FakeWindowExecutor(
            always_invalid_window_id="window-02"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.pdf"
            source.write_bytes(b"synthetic")
            store = LocalObjectStore(root / "objects")
            first_job = _job(document_map, request)
            first_context = _Context(first_job)
            current = _current(document_map, request)
            with (
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_context",
                    return_value=current,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_source_record_path",
                    return_value=source,
                ),
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_requirement_window_executor=first_executor,
                    ),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "window-02"):
                    run_durable_source_requirement_baseline(
                        first_job,
                        first_context,
                    )

            receipt = first_context.current_progress[
                "source_requirement_window_checkpoint"
            ]
            self.assertEqual(receipt["accepted_window_ids"], ["window-01"])
            resumed_executor = _FakeWindowExecutor()
            retry_job = replace(
                first_job,
                attempts=2,
                generation=2,
                progress=first_context.current_progress,
            )
            retry_context = _Context(retry_job)
            with (
                patch(
                    "app.application.agent_workflow.source_requirement_jobs._current_scope_context",
                    return_value=current,
                ),
                patch(
                    "app.application.agent_workflow.source_requirement_jobs.resolve_source_record_path",
                    return_value=source,
                ),
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    SimpleNamespace(
                        object_store=store,
                        source_requirement_window_executor=resumed_executor,
                    ),
                ),
            ):
                result = run_durable_source_requirement_baseline(
                    retry_job,
                    retry_context,
                )

        self.assertEqual(result["status"], "SCOPE_COMPLETE")
        self.assertEqual(resumed_executor.runner.boundary_calls, 0)
        self.assertNotIn("window-01", resumed_executor.runner.window_call_counts)
        self.assertEqual(resumed_executor.runner.window_call_counts["window-02"], 1)

    def test_status_projection_does_not_expose_private_checkpoint_receipt(self) -> None:
        document_map, request = _windowed_fixture()
        job = replace(
            _job(document_map, request),
            progress={
                "stage": "CLASSIFICATION_WINDOW_CHECKPOINTED",
                "message": "已保存 1/2 个分类窗口。",
                "strategy": "BOUNDARY_WINDOWS",
                "completed_window_count": 1,
                "total_window_count": 2,
                "source_requirement_window_checkpoint": {
                    "object_key": "private/customer/content.json",
                    "accepted_window_ids": ["window-01"],
                },
            },
        )
        queue = _Queue(job)
        container = SimpleNamespace(jobs=queue)
        with (
            patch(
                "app.application.runtime.RUNTIME_STATE.container",
                container,
            ),
            patch(
                "app.application.agent_workflow.source_requirement_jobs._require_project_document",
                return_value={"id": "document-1"},
            ),
        ):
            result = get_source_requirement_baseline_run_status(
                "project-1",
                "document-1",
                _Response(),
            )

        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["job"]["progress"]["completed_window_count"], 1)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("source_requirement_window_checkpoint", serialized)
        self.assertNotIn("private/customer", serialized)


class _FakeWindowExecutor:
    adapter_id = "fake-source-requirement-window"
    adapter_version = "1.0.0"
    transport_kind = "TEST_CANONICAL_STAGE"
    model_id = "gpt-5.6-sol"
    reasoning_effort = "high"

    def readiness_reason(self) -> str | None:
        return self._runtime_reason

    def __init__(
        self,
        *,
        always_invalid_window_id: str = "",
        runtime_reason: str | None = None,
    ) -> None:
        self._runtime_reason = runtime_reason
        self.runner = _FakeWindowedCommandRunner(
            always_invalid_window_id=always_invalid_window_id,
        )

    def execute(
        self,
        task,  # type: ignore[no-untyped-def]
        *,
        resume_checkpoint,  # type: ignore[no-untyped-def]
        checkpoint_sink,  # type: ignore[no-untyped-def]
        cancellation_requested,  # type: ignore[no-untyped-def]
    ) -> SourceRequirementWindowExecutionV1:
        if cancellation_requested():
            raise AssertionError("executor called after cancellation")
        document_map, request = _models(task)
        checkpoint = (
            None
            if resume_checkpoint is None
            else NativeSourceRequirementWindowedCheckpointV1.model_validate(
                resume_checkpoint
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            result = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=Path(raw) / "result",
                model_id=self.model_id,
                reasoning_effort=self.reasoning_effort,
                timeout_seconds=task.timeout_seconds,
                max_concurrency=task.max_concurrency,
                command_runner=self.runner,
                resume_checkpoint=checkpoint,
                checkpoint_sink=lambda value: checkpoint_sink(
                    value.model_dump(mode="json")
                ),
            )
        return SourceRequirementWindowExecutionV1(
            candidate=result.candidate.model_dump(mode="json"),
            run_receipt=result.run_receipt.model_dump(mode="json"),
        )


def _models(task):  # type: ignore[no-untyped-def]
    from app.application.agent_workflow.source_requirement_baseline import (
        DocumentMapArtifactV1,
        SourceRequirementScopeRequestV1,
    )

    return (
        DocumentMapArtifactV1.model_validate(task.document_map),
        SourceRequirementScopeRequestV1.model_validate(task.scope_request),
    )


def _current(document_map, request):  # type: ignore[no-untyped-def]
    return {
        "document": {"id": "document-1", "project_id": "project-1"},
        "source_path": Path(__file__).resolve(),
        "source_sha256": document_map.source_sha256,
        "native_artifact_id": "native-artifact-1",
        "native_projection_sha256": document_map.native_projection_sha256,
        "document_map": document_map,
        "request": request,
        "evidence_recovery": {},
    }


def _job(document_map, request, *, status=JobStatus.RUNNING):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return DurableJob(
        job_id="job-source-requirement-1",
        kind=SOURCE_REQUIREMENT_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id="document-1",
        idempotency_key="source-requirement-key-1",
        payload={
            "schema_version": "source-requirement-baseline-job/v1",
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": document_map.source_sha256,
            "native_artifact_id": "native-artifact-1",
            "native_projection_sha256": document_map.native_projection_sha256,
            "document_map_artifact_id": document_map.artifact_id,
            "document_map_artifact_sha256": document_map.artifact_sha256,
            "logical_table_scope_id": request.logical_table_scope_id,
            "request_sha256": request.request_sha256,
            "evidence_recovery_receipt_sha256": "",
            "executor_id": _FakeWindowExecutor.adapter_id,
            "executor_version": _FakeWindowExecutor.adapter_version,
            "transport_kind": _FakeWindowExecutor.transport_kind,
            "model_id": _FakeWindowExecutor.model_id,
            "reasoning_effort": _FakeWindowExecutor.reasoning_effort,
            "timeout_seconds": 1200,
            "max_concurrency": 2,
            "command_id": "source-baseline-start-1",
        },
        status=status,
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
        self.enqueue_calls = 0
        self.last_enqueue = None

    def get_latest_succeeded(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def enqueue(self, **kwargs):  # type: ignore[no-untyped-def]
        self.enqueue_calls += 1
        self.last_enqueue = kwargs
        return self.job

    def get_latest(self, **_kwargs):  # type: ignore[no-untyped-def]
        return self.job


class _Response:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


if __name__ == "__main__":
    unittest.main()
