from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import UTC, datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import Response

from app.adapters.persistence.memory_agent_structure_proposals import (
    InMemoryAgentStructureProposalRepository,
)
from app.adapters.persistence.memory_agent_structure_steers import (
    InMemoryAgentStructureSteerRepository,
)
from app.application import runtime as application_runtime
from app.application.errors import ApplicationError
from app.application.agent_workflow.contracts import (
    AgentRuntimeReceiptV1,
    AgentStructureApplyReceiptV1,
    AgentStructureProposalApplyRequest,
    AgentStructureProposalRejectRequest,
    AgentStructureProposalReviseRequest,
    AgentStructureProposalStartRequest,
    AgentStructureProposalSteerRequest,
)
from app.application.agent_workflow import (
    structure_proposal_actions,
    structure_proposal_jobs,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import (
    create_structure_patch_proposal,
    get_base_effective_structure,
)
from app.domain.extraction.structure import StructureNodeKind
from app.domain.extraction.structure_revision import ReparentStructureOperationV1
from app.ports.agent_runtime import AgentStructureProposalResultV1
from app.ports.agent_structure_steer_repository import AgentStructureSteerStatus
from app.ports.job_queue import DurableJob, JobStatus, NonRetryableJobError
from tests.test_structure_revision import _source_ir_fixture


_NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


class _FakeRuntime:
    adapter_id = "fake-agent-runtime"
    adapter_version = "1.0.0"
    model_id = "synthetic-model"
    skill_id = "reqpilotx-structure-tree-generation"
    skill_version = "1.0.0"

    def __init__(self, proposal) -> None:
        self.proposal = proposal
        self.calls = 0
        self.consume_steering = False
        self.delivered_steers = []

    def propose_structure(
        self,
        task,
        *,
        progress,
        cancellation_requested,
        steering=None,
    ):
        self.calls += 1
        progress("MODEL_RUNNING")
        if cancellation_requested():
            raise AssertionError("runtime called after cancellation")
        if self.consume_steering and steering is not None:
            command = steering.next_command()
            if command is not None:
                self.delivered_steers.append(command)
                steering.mark_delivered(
                    command,
                    runtime_thread_id="synthetic-thread",
                    runtime_turn_id="synthetic-turn",
                )
        return AgentStructureProposalResultV1(
            proposal=self.proposal,
            runtime_receipt=AgentRuntimeReceiptV1(
                runtime_adapter_id=self.adapter_id,
                runtime_version=self.adapter_version,
                model_id=self.model_id,
                skill_id=self.skill_id,
                skill_version=self.skill_version,
                output_schema_sha256="1" * 64,
                request_sha256="2" * 64,
                response_sha256="3" * 64,
                tool_receipt_sha256="4" * 64,
            ),
            runtime_thread_id="synthetic-thread",
        )


class _FakeContext:
    def __init__(self, job: DurableJob, *, cancelled: bool = False) -> None:
        self.job = job
        self.cancelled = cancelled
        self.progress: list[str] = []
        self.publication_started = False

    def heartbeat(self, progress=None):
        if progress:
            self.progress.append(str(progress.get("stage") or ""))
        return self.job

    def cancellation_requested(self):
        return self.cancelled

    def begin_publication(self):
        self.publication_started = True
        return self.job


class _FakeQueue:
    def __init__(self, job: DurableJob) -> None:
        self.job = job
        self.enqueue_calls = []

    def enqueue(self, **kwargs):
        self.enqueue_calls.append(kwargs)
        return self.job

    def get_latest(self, **_kwargs):
        return self.job

    def get(self, job_id):
        return self.job if self.job.job_id == job_id else None


class AgentStructureProposalJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir = _source_ir_fixture()
        self.base_tree = build_structure_tree(self.source_ir)
        self.current = get_base_effective_structure(self.source_ir, self.base_tree)
        target = next(
            node
            for node in self.base_tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.parent_structure_node_id is not None
        )
        parent = next(
            node
            for node in self.base_tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.structure_node_id != target.parent_structure_node_id
        )
        self.proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=parent.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )
        self.runtime = _FakeRuntime(self.proposal)
        self.repository = InMemoryAgentStructureProposalRepository()
        self.steer_repository = InMemoryAgentStructureSteerRepository()
        self.job = _job(self._job_payload())
        self.container = SimpleNamespace(
            agent_runtime=self.runtime,
            agent_structure_proposal_repository=self.repository,
            agent_structure_steer_repository=self.steer_repository,
            jobs=_FakeQueue(self.job),
            settings=SimpleNamespace(agent_customer_data_enabled=True),
        )

    def _job_payload(self):
        facts_sha256 = structure_proposal_jobs._facts_sha256(
            self.source_ir,
            self.base_tree,
            self.current,
        )
        return {
            "schema_version": "agent-structure-proposal-job/v1",
            "project_id": "project-1",
            "document_id": "document-1",
            "command_id": "start-1",
            "source_sha256": self.source_ir.source_sha256,
            "facts_sha256": facts_sha256,
            "source_ir_id": self.source_ir.source_ir_id,
            "source_ir_canonical_sha256": (
                structure_proposal_jobs._source_ir_canonical_sha256(
                    self.source_ir
                )
            ),
            "base_structure_snapshot_id": self.base_tree.structure_snapshot_id,
            "base_structure_canonical_sha256": structure_proposal_jobs.canonical_model_sha256(
                self.base_tree
            ),
            "expected_revision_number": self.current.revision_number,
            "expected_effective_snapshot_id": self.current.effective_snapshot_id,
            "expected_effective_tree_sha256": self.current.effective_tree_sha256,
            "runtime_adapter_id": self.runtime.adapter_id,
            "runtime_adapter_version": self.runtime.adapter_version,
            "model_id": self.runtime.model_id,
            "skill_id": self.runtime.skill_id,
            "skill_version": self.runtime.skill_version,
            "reasoning_effort": "medium",
            "deadline_seconds": 180,
        }

    def _current_tuple(self):
        return (
            {"id": "document-1"},
            {},
            {},
            {},
            self.source_ir,
            self.base_tree,
            (),
            self.current,
        )

    def test_worker_validates_runtime_and_publishes_ready_proposal(self) -> None:
        context = _FakeContext(self.job)
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            result = structure_proposal_jobs.run_durable_agent_structure_proposal(
                self.job,
                context,
            )

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["operation_count"], 1)
        self.assertEqual(self.runtime.calls, 1)
        self.assertEqual(context.progress, ["MODEL_RUNNING"])
        self.assertTrue(context.publication_started)
        stored = self.repository.get(result["proposal_record_id"])
        self.assertEqual(stored.runtime_thread_id, "synthetic-thread")

    def test_worker_rejects_stale_payload_before_runtime(self) -> None:
        stale = {**self.job.payload, "facts_sha256": "0" * 64}
        job = _job(stale)
        context = _FakeContext(job)
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            with self.assertRaisesRegex(
                NonRetryableJobError,
                "Structure changed before Agent proposal execution",
            ):
                structure_proposal_jobs.run_durable_agent_structure_proposal(
                    job,
                    context,
                )
        self.assertEqual(self.runtime.calls, 0)

    def test_worker_currentness_ignores_non_fact_source_ir_timestamp(self) -> None:
        rebuilt_source = self.source_ir.model_copy(
            update={
                "created_at": self.source_ir.created_at + timedelta(seconds=1)
            },
        )
        rebuilt_tree = build_structure_tree(rebuilt_source)
        rebuilt_current = get_base_effective_structure(
            rebuilt_source,
            rebuilt_tree,
        )

        self.assertNotEqual(
            structure_proposal_jobs.canonical_model_sha256(self.source_ir),
            structure_proposal_jobs.canonical_model_sha256(rebuilt_source),
        )
        self.assertEqual(
            structure_proposal_jobs._source_ir_canonical_sha256(self.source_ir),
            structure_proposal_jobs._source_ir_canonical_sha256(rebuilt_source),
        )
        self.assertTrue(
            structure_proposal_jobs._payload_matches_current(
                self.job.payload,
                rebuilt_source,
                rebuilt_tree,
                rebuilt_current,
            )
        )

    def test_worker_honors_cancellation_before_inference(self) -> None:
        context = _FakeContext(self.job, cancelled=True)
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            with self.assertRaisesRegex(
                NonRetryableJobError,
                "cancelled before inference",
            ):
                structure_proposal_jobs.run_durable_agent_structure_proposal(
                    self.job,
                    context,
                )
        self.assertEqual(self.runtime.calls, 0)

    def test_start_freezes_current_identity_and_enqueues_durable_job(self) -> None:
        response = Response()
        request = AgentStructureProposalStartRequest(
            command_id="start-1",
            expected_source_sha256=self.source_ir.source_sha256,
            expected_revision_number=0,
            expected_effective_snapshot_id=self.current.effective_snapshot_id,
        )
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            result = structure_proposal_jobs.start_agent_structure_proposal(
                "project-1",
                "document-1",
                request,
                response,
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(result["job_id"], self.job.job_id)
        enqueue = self.container.jobs.enqueue_calls[0]
        self.assertEqual(enqueue["kind"], "agent.structure.propose")
        self.assertNotIn("source_ir", enqueue["payload"])
        self.assertNotIn("prompt", enqueue["payload"])

    def test_start_without_runtime_is_controlled_503_and_enqueues_nothing(self) -> None:
        container = SimpleNamespace(
            agent_runtime=None,
            agent_structure_proposal_repository=self.repository,
            agent_structure_steer_repository=self.steer_repository,
            jobs=self.container.jobs,
            settings=SimpleNamespace(agent_customer_data_enabled=False),
        )
        request = AgentStructureProposalStartRequest(
            command_id="start-no-runtime",
            expected_source_sha256=self.source_ir.source_sha256,
            expected_revision_number=0,
            expected_effective_snapshot_id=self.current.effective_snapshot_id,
        )
        with patch.object(application_runtime.RUNTIME_STATE, "container", container):
            with self.assertRaises(ApplicationError) as caught:
                structure_proposal_jobs.start_agent_structure_proposal(
                    "project-1",
                    "document-1",
                    request,
                    Response(),
                )
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"],
            "agent_runtime_not_configured",
        )
        self.assertEqual(container.jobs.enqueue_calls, [])

    def test_start_with_customer_data_disabled_enqueues_nothing(self) -> None:
        container = SimpleNamespace(
            agent_runtime=self.runtime,
            agent_structure_proposal_repository=self.repository,
            agent_structure_steer_repository=self.steer_repository,
            jobs=_FakeQueue(self.job),
            settings=SimpleNamespace(agent_customer_data_enabled=False),
        )
        request = AgentStructureProposalStartRequest(
            command_id="start-disabled",
            expected_source_sha256=self.source_ir.source_sha256,
            expected_revision_number=0,
            expected_effective_snapshot_id=self.current.effective_snapshot_id,
        )
        with patch.object(application_runtime.RUNTIME_STATE, "container", container):
            with self.assertRaises(ApplicationError) as caught:
                structure_proposal_jobs.start_agent_structure_proposal(
                    "project-1",
                    "document-1",
                    request,
                    Response(),
                )
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"],
            "agent_customer_data_disabled",
        )
        self.assertEqual(container.jobs.enqueue_calls, [])

    def test_first_party_apply_action_dispatches_typed_writer(self) -> None:
        context = _FakeContext(self.job)
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            result = structure_proposal_jobs.run_durable_agent_structure_proposal(
                self.job,
                context,
            )
        proposal = self.repository.get(result["proposal_record_id"])
        apply_request = AgentStructureProposalApplyRequest(
            proposal_record_id=proposal.proposal_record_id,
            command_id="user-apply-1",
            expected_proposal_payload_sha256=proposal.proposal_payload_sha256,
        )
        writer_calls = []

        def writer(project_id, document_id, patch_proposal, *, command_id):
            writer_calls.append((project_id, document_id, patch_proposal, command_id))
            return AgentStructureApplyReceiptV1(
                structure_revision_id="structure-revision-action",
                structure_revision_number=1,
                effective_structure_snapshot_id="effective-structure-" + ("c" * 32),
                appended=True,
            )

        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={"id": "document-1"},
            ),
            patch(
                "app.application.extraction.structure_workspace.apply_stored_agent_structure_patch",
                side_effect=writer,
            ),
        ):
            response = structure_proposal_actions.apply_agent_structure_proposal_action(
                "project-1",
                "document-1",
                apply_request,
                Response(),
            )

        self.assertEqual(response["proposal"]["status"], "APPLIED")
        self.assertEqual(response["apply_receipt"]["structure_revision_number"], 1)
        self.assertEqual(len(writer_calls), 1)

    def test_ready_proposal_can_start_a_structured_revision(self) -> None:
        first_context = _FakeContext(self.job)
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            first_result = (
                structure_proposal_jobs.run_durable_agent_structure_proposal(
                    self.job,
                    first_context,
                )
            )
        first = self.repository.get(first_result["proposal_record_id"])
        first_target_id = first.structure_patch_proposal.operations[0].structure_node_id
        second_target = next(
            node
            for node in self.base_tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.parent_structure_node_id is not None
            and node.structure_node_id != first_target_id
        )
        new_parent_id = (
            first.structure_patch_proposal.operations[0].new_parent_structure_node_id
        )
        revised_operation = ReparentStructureOperationV1(
            structure_node_id=second_target.structure_node_id,
            new_parent_structure_node_id=new_parent_id,
            new_sibling_index=0,
        )
        self.runtime.proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (revised_operation,),
        )
        request = AgentStructureProposalReviseRequest(
            proposal_record_id=first.proposal_record_id,
            command_id="revise-1",
            expected_proposal_payload_sha256=first.proposal_payload_sha256,
            expected_source_sha256=self.source_ir.source_sha256,
            expected_revision_number=0,
            expected_effective_snapshot_id=self.current.effective_snapshot_id,
            intent="REVIEW_PARENTAGE",
            focus_structure_node_ids=(second_target.structure_node_id,),
            suggested_operations=(revised_operation,),
            suggested_operations_authority="USER_REQUESTED",
        )
        self.container.jobs = _FakeQueue(self.job)
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            started = (
                structure_proposal_jobs.start_agent_structure_proposal_revision(
                    "project-1",
                    "document-1",
                    request,
                    Response(),
                )
            )
            self.assertEqual(started["status"], JobStatus.PENDING.value)
            enqueued_payload = self.container.jobs.enqueue_calls[0]["payload"]
            self.assertEqual(
                enqueued_payload["supersedes_proposal_record_id"],
                first.proposal_record_id,
            )
            revised_job = _job(enqueued_payload)
            revised_result = (
                structure_proposal_jobs.run_durable_agent_structure_proposal(
                    revised_job,
                    _FakeContext(revised_job),
                )
            )

        revised = self.repository.get(revised_result["proposal_record_id"])
        self.assertEqual(revised.proposal_revision, 2)
        self.assertEqual(revised.supersedes_proposal_record_id, first.proposal_record_id)
        self.assertEqual(
            self.repository.get(first.proposal_record_id).status.value,
            "STALE",
        )

    def test_running_job_accepts_and_delivers_exact_structured_steer(self) -> None:
        running_job = replace(
            self.job,
            status=JobStatus.RUNNING,
            generation=2,
            lease_owner="worker-1",
        )
        self.container.jobs = _FakeQueue(running_job)
        self.runtime.consume_steering = True
        request = AgentStructureProposalSteerRequest(
            job_id=running_job.job_id,
            command_id="steer-1",
            expected_job_generation=2,
            expected_source_sha256=self.source_ir.source_sha256,
            expected_revision_number=self.current.revision_number,
            expected_effective_snapshot_id=self.current.effective_snapshot_id,
            intent="REVIEW_PARENTAGE",
            focus_structure_node_ids=(
                self.proposal.operations[0].structure_node_id,
            ),
        )
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            accepted = structure_proposal_jobs.steer_active_agent_structure_proposal(
                "project-1",
                "document-1",
                request,
                Response(),
            )
            result = structure_proposal_jobs.run_durable_agent_structure_proposal(
                running_job,
                _FakeContext(running_job),
            )

        self.assertEqual(accepted["status"], "PENDING")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(len(self.runtime.delivered_steers), 1)
        delivered = self.steer_repository.get_latest_for_job(running_job.job_id)
        self.assertEqual(delivered.status, AgentStructureSteerStatus.DELIVERED)
        self.assertEqual(delivered.runtime_thread_id, "synthetic-thread")
        self.assertEqual(delivered.runtime_turn_id, "synthetic-turn")

    def test_first_party_reject_action_never_calls_writer(self) -> None:
        context = _FakeContext(self.job)
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch.object(
                structure_proposal_jobs,
                "_load_current_structure",
                return_value=self._current_tuple(),
            ),
        ):
            result = structure_proposal_jobs.run_durable_agent_structure_proposal(
                self.job,
                context,
            )
        proposal = self.repository.get(result["proposal_record_id"])
        request = AgentStructureProposalRejectRequest(
            proposal_record_id=proposal.proposal_record_id,
            command_id="user-reject-1",
            expected_proposal_payload_sha256=proposal.proposal_payload_sha256,
        )
        with (
            patch.object(
                application_runtime.RUNTIME_STATE, "container", self.container
            ),
            patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={"id": "document-1"},
            ),
        ):
            response = (
                structure_proposal_actions.reject_agent_structure_proposal_action(
                    "project-1",
                    "document-1",
                    request,
                    Response(),
                )
            )

        self.assertEqual(response["status"], "REJECTED")


def _job(payload) -> DurableJob:
    return DurableJob(
        job_id="job-agent-structure-1",
        kind="agent.structure.propose",
        aggregate_type="sor-document",
        aggregate_id="document-1",
        idempotency_key="a" * 64,
        payload=payload,
        status=JobStatus.PENDING,
        priority=20,
        attempts=0,
        max_attempts=2,
        generation=1,
        cancel_requested=False,
        lease_owner="",
        lease_expires_at=None,
        available_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        progress={},
        result=None,
        error_code="",
        error_message="",
    )


if __name__ == "__main__":
    unittest.main()
