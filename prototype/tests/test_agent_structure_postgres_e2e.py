from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from fastapi import Response

from app.adapters.persistence.postgres_agent_structure_proposals import (
    PostgresAgentStructureProposalRepository,
)
from app.adapters.persistence.postgres_agent_structure_steers import (
    PostgresAgentStructureSteerRepository,
)
from app.adapters.persistence.postgres_jobs import PostgresJobQueue
from app.application import runtime as application_runtime
from app.application.agent_workflow.contracts import (
    AgentRuntimeReceiptV1,
    AgentStructureApplyReceiptV1,
    AgentStructureProposalApplyRequest,
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
from app.infrastructure.postgres import PostgresDatabase
from app.ports.agent_runtime import AgentStructureProposalResultV1
from app.ports.job_queue import JobStatus
from app.worker.runtime import WorkerRuntime
from tests.test_structure_revision import _source_ir_fixture


_POSTGRES_E2E_ENABLED = os.environ.get("REQPILOT_RUN_POSTGRES_INTEGRATION") == "1"


class _SyntheticRuntime:
    adapter_id = "synthetic-agent-runtime"
    adapter_version = "1.0.0"
    model_id = "synthetic-model"
    skill_id = "reqpilotx-structure-tree-generation"
    skill_version = "1.0.0"

    def __init__(self, proposal) -> None:
        self.proposal = proposal
        self.on_propose = None
        self.delivered_steers = []

    def propose_structure(
        self,
        task,
        *,
        progress,
        cancellation_requested,
        steering=None,
    ):
        progress("SYNTHETIC_RUNTIME")
        if cancellation_requested():
            raise AssertionError("synthetic runtime observed unexpected cancellation")
        if self.on_propose is not None:
            self.on_propose()
        if steering is not None:
            command = steering.next_command()
            if command is not None:
                self.delivered_steers.append(command)
                steering.mark_delivered(
                    command,
                    runtime_thread_id="synthetic-postgres-thread",
                    runtime_turn_id="synthetic-postgres-turn",
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
            runtime_thread_id="synthetic-postgres-thread",
        )


@unittest.skipUnless(
    _POSTGRES_E2E_ENABLED,
    "set REQPILOT_RUN_POSTGRES_INTEGRATION=1 for the local PostgreSQL E2E",
)
class AgentStructurePostgresE2ETests(unittest.TestCase):
    def test_api_job_worker_proposal_and_apply_replay(self) -> None:
        database_url = os.environ.get(
            "REQPILOT_POSTGRES_INTEGRATION_URL",
            "postgresql://localhost/reqpilotx",
        )
        database = PostgresDatabase(database_url)
        database.apply_migrations(
            Path(__file__).resolve().parents[1] / "migrations" / "postgresql"
        )
        queue = PostgresJobQueue(database)
        repository = PostgresAgentStructureProposalRepository(database)
        steer_repository = PostgresAgentStructureSteerRepository(database)
        suffix = uuid.uuid4().hex
        project_id = f"agent-e2e-project-{suffix}"
        document_id = f"agent-e2e-document-{suffix}"
        source_ir = _source_ir_fixture()
        base_tree = build_structure_tree(source_ir)
        current = get_base_effective_structure(source_ir, base_tree)
        target = next(
            node
            for node in base_tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.parent_structure_node_id is not None
        )
        parent = next(
            node
            for node in base_tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.structure_node_id != target.parent_structure_node_id
        )
        proposal = create_structure_patch_proposal(
            source_ir,
            base_tree,
            (),
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=parent.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )
        second_target = next(
            node
            for node in base_tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.parent_structure_node_id is not None
            and node.structure_node_id != target.structure_node_id
        )
        revised_operation = ReparentStructureOperationV1(
            structure_node_id=second_target.structure_node_id,
            new_parent_structure_node_id=parent.structure_node_id,
            new_sibling_index=0,
        )
        revised_patch = create_structure_patch_proposal(
            source_ir,
            base_tree,
            (),
            (revised_operation,),
        )
        runtime = _SyntheticRuntime(proposal)
        container = SimpleNamespace(
            agent_runtime=runtime,
            agent_structure_proposal_repository=repository,
            agent_structure_steer_repository=steer_repository,
            jobs=queue,
            settings=SimpleNamespace(agent_customer_data_enabled=True),
        )
        current_tuple = (
            {"id": document_id},
            {},
            {},
            {},
            source_ir,
            base_tree,
            (),
            current,
        )
        now = datetime.now(timezone.utc).isoformat()
        job_ids: list[str] = []
        storage_key = f"synthetic/{document_id}.pdf"
        try:
            with database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO projects (id, name, customer_name, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (project_id, "Agent E2E", "Synthetic", now),
                )
                connection.execute(
                    """
                    INSERT INTO stored_objects (
                        storage_key, sha256, size_bytes, media_type, object_kind
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        storage_key,
                        source_ir.source_sha256,
                        1,
                        "application/pdf",
                        "sor-source",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sor_documents (
                        id, project_id, file_name, file_type, storage_key,
                        extracted_text, source_sha256, source_size, created_at,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        document_id,
                        project_id,
                        "synthetic.pdf",
                        "pdf",
                        storage_key,
                        "",
                        source_ir.source_sha256,
                        1,
                        now,
                        now,
                    ),
                )

            request = AgentStructureProposalStartRequest(
                command_id="postgres-e2e-start",
                expected_source_sha256=source_ir.source_sha256,
                expected_revision_number=current.revision_number,
                expected_effective_snapshot_id=current.effective_snapshot_id,
                intent="REVIEW_PARENTAGE",
                focus_structure_node_ids=(target.structure_node_id,),
                suggested_operations=(proposal.operations[0],),
                suggested_operations_authority="DETERMINISTIC_FINDING",
            )
            with (
                patch.object(application_runtime.RUNTIME_STATE, "container", container),
                patch.object(
                    structure_proposal_jobs,
                    "_load_current_structure",
                    return_value=current_tuple,
                ),
            ):
                started = structure_proposal_jobs.start_agent_structure_proposal(
                    project_id,
                    document_id,
                    request,
                    Response(),
                )
                job_id = started["job_id"]
                job_ids.append(job_id)
                worker = WorkerRuntime(
                    queue=queue,
                    handlers={
                        structure_proposal_jobs.AGENT_STRUCTURE_PROPOSAL_JOB_KIND: (
                            structure_proposal_jobs.run_durable_agent_structure_proposal
                        )
                    },
                    worker_id=f"agent-e2e-worker-{suffix}",
                    lease_seconds=30,
                    poll_seconds=0.1,
                )
                def submit_active_steer() -> None:
                    active = queue.get(job_id)
                    structure_proposal_jobs.steer_active_agent_structure_proposal(
                        project_id,
                        document_id,
                        AgentStructureProposalSteerRequest(
                            job_id=job_id,
                            command_id="postgres-e2e-steer",
                            expected_job_generation=active.generation,
                            expected_source_sha256=source_ir.source_sha256,
                            expected_revision_number=current.revision_number,
                            expected_effective_snapshot_id=(
                                current.effective_snapshot_id
                            ),
                            intent="REVIEW_PARENTAGE",
                            focus_structure_node_ids=(target.structure_node_id,),
                        ),
                        Response(),
                    )

                runtime.on_propose = submit_active_steer
                self.assertTrue(worker.run_once())
                runtime.on_propose = None
                completed = queue.get(job_id)
                self.assertIsNotNone(completed)
                self.assertEqual(completed.status, JobStatus.SUCCEEDED)
                self.assertEqual(completed.result["status"], "READY")
                persisted = repository.get(completed.result["proposal_record_id"])
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.runtime_thread_id, "synthetic-postgres-thread")
                delivered_steer = steer_repository.get_latest_for_job(job_id)
                self.assertEqual(delivered_steer.status.value, "DELIVERED")
                self.assertEqual(
                    delivered_steer.runtime_turn_id,
                    "synthetic-postgres-turn",
                )
                self.assertEqual(len(runtime.delivered_steers), 1)

                runtime.proposal = revised_patch
                revised_start = (
                    structure_proposal_jobs.start_agent_structure_proposal_revision(
                        project_id,
                        document_id,
                        AgentStructureProposalReviseRequest(
                            proposal_record_id=persisted.proposal_record_id,
                            command_id="postgres-e2e-revise",
                            expected_proposal_payload_sha256=(
                                persisted.proposal_payload_sha256
                            ),
                            expected_source_sha256=source_ir.source_sha256,
                            expected_revision_number=current.revision_number,
                            expected_effective_snapshot_id=(
                                current.effective_snapshot_id
                            ),
                            intent="REVIEW_PARENTAGE",
                            focus_structure_node_ids=(
                                second_target.structure_node_id,
                            ),
                            suggested_operations=(revised_operation,),
                            suggested_operations_authority="USER_REQUESTED",
                        ),
                        Response(),
                    )
                )
                revised_job_id = revised_start["job_id"]
                job_ids.append(revised_job_id)
                self.assertTrue(worker.run_once())
                revised_job = queue.get(revised_job_id)
                self.assertEqual(revised_job.status, JobStatus.SUCCEEDED)
                revised = repository.get(revised_job.result["proposal_record_id"])
                self.assertEqual(revised.proposal_revision, 2)
                self.assertEqual(
                    revised.supersedes_proposal_record_id,
                    persisted.proposal_record_id,
                )
                self.assertEqual(
                    repository.get(persisted.proposal_record_id).status.value,
                    "STALE",
                )
                persisted = revised

                apply_request = AgentStructureProposalApplyRequest(
                    proposal_record_id=persisted.proposal_record_id,
                    command_id="postgres-e2e-apply",
                    expected_proposal_payload_sha256=(
                        persisted.proposal_payload_sha256
                    ),
                )
                apply_receipt = AgentStructureApplyReceiptV1(
                    structure_revision_id=f"structure-revision-{suffix}",
                    structure_revision_number=1,
                    effective_structure_snapshot_id=(
                        "effective-structure-" + "a" * 32
                    ),
                    appended=True,
                )
                with (
                    patch(
                        "app.application.extraction.review_support._require_project_document",
                        return_value={"id": document_id},
                    ),
                    patch(
                        "app.application.extraction.structure_workspace.apply_stored_agent_structure_patch",
                        return_value=apply_receipt,
                    ) as writer,
                ):
                    applied = (
                        structure_proposal_actions.apply_agent_structure_proposal_action(
                            project_id,
                            document_id,
                            apply_request,
                            Response(),
                        )
                    )
                    replayed = (
                        structure_proposal_actions.apply_agent_structure_proposal_action(
                            project_id,
                            document_id,
                            apply_request,
                            Response(),
                        )
                    )
                self.assertEqual(applied["proposal"]["status"], "APPLIED")
                self.assertTrue(applied["apply_receipt"]["appended"])
                self.assertFalse(replayed["apply_receipt"]["appended"])
                writer.assert_called_once()
                self.assertEqual(
                    repository.get(persisted.proposal_record_id).status.value,
                    "APPLIED",
                )
        finally:
            try:
                with database.connection() as connection:
                    for job_id in job_ids:
                        connection.execute(
                            "DELETE FROM runtime_jobs WHERE id = %s",
                            (job_id,),
                        )
                    connection.execute(
                        "DELETE FROM projects WHERE id = %s",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM stored_objects WHERE storage_key = %s",
                        (storage_key,),
                    )
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
