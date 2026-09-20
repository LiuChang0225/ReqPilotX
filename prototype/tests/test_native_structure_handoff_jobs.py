from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import UTC, datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalStructureHandoffBindingV1,
)
from app.application.agent_workflow.native_structure_handoff_jobs import (
    AGENT_STRUCTURE_HANDOFF_JOB_KIND,
    _build_binding,
    enqueue_native_structure_handoff_job,
    run_durable_native_structure_handoff,
)
from app.ports.agent_structure_handoff_executor import (
    AgentStructureHandoffExecutionV1,
)
from app.ports.job_queue import DurableJob, JobExecutionContext, JobStatus


class NativeStructureHandoffJobTests(unittest.TestCase):
    def test_continue_enqueue_is_idempotent_and_binds_executor_identity(self) -> None:
        queue = _Queue()
        executor = _Executor()
        with (
            patch(
                "app.application.runtime.RUNTIME_STATE.container",
                SimpleNamespace(agent_structure_handoff_executor=executor),
            ),
            patch(
                "app.application.runtime._agent_customer_data_enabled",
                return_value=True,
            ),
            patch("app.application.runtime._durable_jobs", return_value=queue),
        ):
            first = enqueue_native_structure_handoff_job(
                project_id="project-1",
                document_id="document-1",
                source_sha256="a" * 64,
                artifact_id="artifact-1",
                handoff=_handoff(),
            )
            second = enqueue_native_structure_handoff_job(
                project_id="project-1",
                document_id="document-1",
                source_sha256="a" * 64,
                artifact_id="artifact-1",
                handoff=_handoff(),
            )

        self.assertEqual(first, second)
        self.assertEqual(queue.calls, 2)
        self.assertEqual(queue.last["kind"], AGENT_STRUCTURE_HANDOFF_JOB_KIND)
        self.assertEqual(queue.last["payload"]["executor_id"], executor.adapter_id)
        self.assertEqual(queue.last["payload"]["binding"]["status"], "ACKNOWLEDGED")

    def test_worker_acks_executes_and_fences_dynamic_publication(self) -> None:
        handoff = _handoff()
        binding = _build_binding(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            artifact_id="artifact-1",
            handoff=handoff,
        )
        executor = _Executor()
        context = _Context()
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source.pdf"
            source.write_bytes(b"synthetic")
            with (
                patch(
                    "app.application.runtime._agent_structure_handoff_executor",
                    return_value=executor,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs._require_project_document",
                    return_value={
                        "id": "document-1",
                        "project_id": "project-1",
                        "source_sha256": "a" * 64,
                    },
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs._read_handoffs",
                    return_value=[handoff],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs._read_all_handoff_attempts",
                    return_value=[],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs.resolve_source_record_path",
                    return_value=source,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs.acknowledge_native_structure_handoff"
                ) as acknowledge,
            ):
                result = run_durable_native_structure_handoff(
                    _job(binding),
                    context,
                )

        acknowledge.assert_called_once()
        self.assertEqual(executor.calls, 1)
        self.assertEqual(context.publication_count, 1)
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["attempt_status"], "VERIFICATION_REQUIRED")
        self.assertTrue(result["runtime_thread_id_sha256"])

    def test_worker_recovers_durable_attempt_without_second_inference(self) -> None:
        handoff = _handoff()
        binding = _build_binding(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            artifact_id="artifact-1",
            handoff=handoff,
        )
        executor = _Executor()
        with (
            patch(
                "app.application.runtime._agent_structure_handoff_executor",
                return_value=executor,
            ),
            patch(
                "app.application.agent_workflow.native_structure_handoff_jobs._require_project_document",
                return_value={
                    "id": "document-1",
                    "project_id": "project-1",
                    "source_sha256": "a" * 64,
                },
            ),
            patch(
                "app.application.agent_workflow.native_structure_handoff_jobs._read_handoffs",
                return_value=[handoff],
            ),
            patch(
                "app.application.agent_workflow.native_structure_handoff_jobs._read_all_handoff_attempts",
                return_value=[
                    {
                        "handoff_id": handoff["handoff_id"],
                        "attempt_id": "native-structure-handoff-attempt-" + "1" * 32,
                        "status": "VERIFICATION_REQUIRED",
                    }
                ],
            ),
        ):
            result = run_durable_native_structure_handoff(_job(binding), _Context())

        self.assertEqual(executor.calls, 0)
        self.assertEqual(result["reason_code"], "durable_attempt_recovered")

    def test_worker_records_non_publishing_outcome_without_publication_fence(
        self,
    ) -> None:
        handoff = _handoff()
        binding = _build_binding(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            artifact_id="artifact-1",
            handoff=handoff,
        )
        executor = _NoChangeExecutor()
        context = _Context()
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source.pdf"
            source.write_bytes(b"synthetic")
            with (
                patch(
                    "app.application.runtime._agent_structure_handoff_executor",
                    return_value=executor,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs._require_project_document",
                    return_value={
                        "id": "document-1",
                        "project_id": "project-1",
                        "source_sha256": "a" * 64,
                    },
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs._read_handoffs",
                    return_value=[handoff],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs._read_handoff_events",
                    return_value=[
                        {
                            "handoff_id": handoff["handoff_id"],
                            "status": "ACKNOWLEDGED",
                        }
                    ],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs._read_all_handoff_attempts",
                    return_value=[],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs.resolve_source_record_path",
                    return_value=source,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs.acknowledge_native_structure_handoff"
                ),
                patch(
                    "app.application.agent_workflow.native_structure_handoff_jobs.record_native_structure_handoff_outcome"
                ) as record_outcome,
            ):
                result = run_durable_native_structure_handoff(
                    _job(binding),
                    context,
                )

        self.assertEqual(result["status"], "NO_CHANGE")
        self.assertEqual(result["evidence_state"], "INSUFFICIENT")
        self.assertEqual(result["decision_policy"], "KEEP_CURRENT_STRUCTURE")
        self.assertEqual(context.publication_count, 0)
        record_outcome.assert_called_once_with(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            artifact_id="artifact-1",
            handoff_id=handoff["handoff_id"],
            execution_status="NO_CHANGE",
            reason_code="current_structure_selected",
            evidence_state="INSUFFICIENT",
            decision_policy="KEEP_CURRENT_STRUCTURE",
            public_summary="证据有限，已明确保留当前结构。",
        )

    def test_worker_recovers_terminal_outcome_without_second_inference(self) -> None:
        handoff = _handoff()
        binding = _build_binding(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            artifact_id="artifact-1",
            handoff=handoff,
        )
        executor = _Executor()
        with (
            patch(
                "app.application.runtime._agent_structure_handoff_executor",
                return_value=executor,
            ),
            patch(
                "app.application.agent_workflow.native_structure_handoff_jobs._require_project_document",
                return_value={
                    "id": "document-1",
                    "project_id": "project-1",
                    "source_sha256": "a" * 64,
                },
            ),
            patch(
                "app.application.agent_workflow.native_structure_handoff_jobs._read_handoffs",
                return_value=[handoff],
            ),
            patch(
                "app.application.agent_workflow.native_structure_handoff_jobs._read_handoff_events",
                return_value=[
                    {
                        "handoff_id": handoff["handoff_id"],
                        "status": "UNRESOLVED_CONTINUED",
                        "execution_status": "UNRESOLVED",
                        "reason_code": "evidence_insufficient",
                    }
                ],
            ),
        ):
            result = run_durable_native_structure_handoff(_job(binding), _Context())

        self.assertEqual(executor.calls, 0)
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["reason_code"], "evidence_insufficient")
        self.assertEqual(result["evidence_state"], "INSUFFICIENT")
        self.assertEqual(result["decision_policy"], "KEEP_CURRENT_STRUCTURE")


class _Executor:
    adapter_id = "canonical-structure-handoff"
    adapter_version = "0.1.0"

    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        task,  # type: ignore[no-untyped-def]
        *,
        progress,  # type: ignore[no-untyped-def]
        cancellation_requested,  # type: ignore[no-untyped-def]
        begin_publication,  # type: ignore[no-untyped-def]
    ) -> AgentStructureHandoffExecutionV1:
        self.calls += 1
        self.assert_task = task
        progress("CODEX_TURN_RUNNING")
        if cancellation_requested():
            raise AssertionError("synthetic handoff was unexpectedly cancelled")
        begin_publication()
        return AgentStructureHandoffExecutionV1(
            status="PUBLISHED",
            reason_code="revision_published",
            evidence_state="CONFLICTING",
            decision_policy="FIRST_HOST_VALIDATED_PROPOSAL",
            public_summary="published",
            publication={
                "attempt_id": "native-structure-handoff-attempt-" + "1" * 32,
                "status": "VERIFICATION_REQUIRED",
            },
            runtime_thread_id_sha256="2" * 64,
            runtime_turn_id_sha256="3" * 64,
            tool_activity_summary={"dynamic_tools": {"call_count": 2}},
        )


class _NoChangeExecutor(_Executor):
    def execute(
        self,
        task,  # type: ignore[no-untyped-def]
        *,
        progress,  # type: ignore[no-untyped-def]
        cancellation_requested,  # type: ignore[no-untyped-def]
        begin_publication,  # type: ignore[no-untyped-def]
    ) -> AgentStructureHandoffExecutionV1:
        self.calls += 1
        progress("CODEX_TURN_RUNNING")
        if cancellation_requested():
            raise AssertionError("synthetic handoff was unexpectedly cancelled")
        return AgentStructureHandoffExecutionV1(
            status="NO_CHANGE",
            reason_code="current_structure_selected",
            evidence_state="INSUFFICIENT",
            decision_policy="KEEP_CURRENT_STRUCTURE",
            public_summary="证据有限，已明确保留当前结构。",
            publication=None,
            runtime_thread_id_sha256="2" * 64,
            runtime_turn_id_sha256="3" * 64,
            tool_activity_summary={"dynamic_tools": {"call_count": 1}},
        )


class _Context(JobExecutionContext):
    def __init__(self) -> None:
        self.publication_count = 0

    def heartbeat(self, progress=None):  # type: ignore[no-untyped-def]
        return SimpleNamespace(progress=progress or {})

    def cancellation_requested(self) -> bool:
        return False

    def lease_fence(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(job_id="job-1", generation=1, worker_id="worker-1")

    def begin_publication(self):  # type: ignore[no-untyped-def]
        self.publication_count += 1
        return SimpleNamespace()


class _Queue:
    def __init__(self) -> None:
        self.calls = 0
        self.last = None

    def enqueue(self, **values):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.last = values
        binding = CanonicalStructureHandoffBindingV1.model_validate(
            values["payload"]["binding"]
        )
        return _job(binding)


def _handoff() -> dict[str, object]:
    return {
        "handoff_id": "native-structure-agent-handoff-" + "4" * 32,
        "input_fingerprint": "5" * 64,
        "affected_node_ids": ["scope-1"],
        "feedback_ids": ["feedback-1"],
        "proposal_decision_ids": [],
    }


def _job(binding) -> DurableJob:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return DurableJob(
        job_id="job-1",
        kind=AGENT_STRUCTURE_HANDOFF_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id="document-1",
        idempotency_key="key-1",
        payload={
            "schema_version": "agent-structure-handoff-job/v1",
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": "a" * 64,
            "artifact_id": "artifact-1",
            "handoff_id": binding.handoff_id,
            "input_fingerprint": binding.input_fingerprint,
            "binding": binding.model_dump(mode="json"),
            "executor_id": "canonical-structure-handoff",
            "executor_version": "0.1.0",
            "deadline_seconds": 300,
        },
        status=JobStatus.RUNNING,
        priority=25,
        attempts=1,
        max_attempts=2,
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


if __name__ == "__main__":
    unittest.main()
