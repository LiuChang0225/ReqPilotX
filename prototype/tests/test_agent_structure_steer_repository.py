from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import unittest

from app.adapters.persistence.memory_agent_structure_steers import (
    InMemoryAgentStructureSteerRepository,
)
from app.application.agent_workflow.contracts import (
    AgentStructureProposalSteerRequest,
)
from app.application.agent_workflow.structure_steers import (
    create_agent_structure_steer,
)
from app.ports.agent_structure_steer_repository import (
    AgentStructureSteerConflict,
    AgentStructureSteerStatus,
)


class AgentStructureSteerRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryAgentStructureSteerRepository()
        self.request = AgentStructureProposalSteerRequest(
            job_id="job-agent-1",
            command_id="steer-1",
            expected_job_generation=2,
            expected_source_sha256="a" * 64,
            expected_revision_number=0,
            expected_effective_snapshot_id="effective-structure-" + "b" * 32,
            intent="REVIEW_PARENTAGE",
            focus_structure_node_ids=("structure-node-" + "c" * 32,),
        )

    def test_exact_replay_claim_and_delivery_are_idempotent(self) -> None:
        created = create_agent_structure_steer(
            self.repository,
            project_id="project-1",
            document_id="document-1",
            payload=self.request,
        )
        replay = create_agent_structure_steer(
            self.repository,
            project_id="project-1",
            document_id="document-1",
            payload=self.request,
        )
        claimed = self.repository.claim_next(
            job_id=self.request.job_id,
            expected_job_generation=2,
        )
        delivered = self.repository.mark_delivered(
            steer_command_id=claimed.steer_command_id,
            expected_payload_sha256=claimed.payload_sha256,
            runtime_thread_id="thread-1",
            runtime_turn_id="turn-1",
        )
        delivered_replay = self.repository.mark_delivered(
            steer_command_id=claimed.steer_command_id,
            expected_payload_sha256=claimed.payload_sha256,
            runtime_thread_id="thread-1",
            runtime_turn_id="turn-1",
        )

        self.assertEqual(replay, created)
        self.assertEqual(claimed.status, AgentStructureSteerStatus.DELIVERING)
        self.assertEqual(delivered.status, AgentStructureSteerStatus.DELIVERED)
        self.assertEqual(delivered_replay, delivered)
        self.assertEqual(delivered.runtime_turn_id, "turn-1")

    def test_replay_drift_and_wrong_generation_fail_closed(self) -> None:
        create_agent_structure_steer(
            self.repository,
            project_id="project-1",
            document_id="document-1",
            payload=self.request,
        )
        drifted = self.request.model_copy(update={"intent": "REVIEW_ORDER"})
        with self.assertRaisesRegex(
            AgentStructureSteerConflict,
            "replay changed payload",
        ):
            create_agent_structure_steer(
                self.repository,
                project_id="project-1",
                document_id="document-1",
                payload=drifted,
            )
        self.assertIsNone(
            self.repository.claim_next(
                job_id=self.request.job_id,
                expected_job_generation=3,
            )
        )
        rejected = self.repository.reject_other_generations(
            job_id=self.request.job_id,
            current_job_generation=3,
            reason_code="job_generation_superseded",
        )
        self.assertEqual(rejected, 1)
        self.assertEqual(
            self.repository.get_latest_for_job(self.request.job_id).status,
            AgentStructureSteerStatus.REJECTED,
        )

    def test_migration_persists_only_structured_direction(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations/postgresql/0031_agent_structure_steers.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("agent_structure_steer_commands", migration)
        self.assertIn("expected_job_generation", migration)
        self.assertIn("suggested_operations", migration)
        self.assertIn("runtime_turn_id", migration)
        self.assertNotIn("instruction_text", migration)
        self.assertNotIn("prompt", migration.lower())


if __name__ == "__main__":
    unittest.main()
