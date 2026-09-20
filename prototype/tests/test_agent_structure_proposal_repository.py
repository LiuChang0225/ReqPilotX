from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import UTC, datetime, timedelta
from pathlib import Path
import unittest

from app.adapters.persistence.memory_agent_structure_proposals import (
    InMemoryAgentStructureProposalRepository,
)
from app.application.agent_workflow.contracts import (
    AgentRuntimeReceiptV1,
    AgentStructureApplyReceiptV1,
    AgentStructureProposalStatus,
    AgentStructureProposalV1,
    stable_agent_structure_proposal_record_id,
    structure_patch_proposal_sha256,
)
from app.application.agent_workflow.structure_proposals import (
    apply_agent_structure_proposal,
    create_ready_agent_structure_proposal,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import create_structure_patch_proposal
from app.domain.extraction.structure import StructureNodeKind
from app.domain.extraction.structure_revision import ReparentStructureOperationV1
from app.ports.agent_structure_proposal_repository import (
    AgentStructureProposalConflict,
)
from tests.test_structure_revision import _source_ir_fixture


_NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


class AgentStructureProposalRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        source_ir = _source_ir_fixture()
        base_tree = build_structure_tree(source_ir)
        self.source_ir = source_ir
        self.base_tree = base_tree
        target = next(
            node
            for node in base_tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.parent_structure_node_id is not None
        )
        new_parent = next(
            node
            for node in base_tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.structure_node_id != target.parent_structure_node_id
        )
        patch_proposal = create_structure_patch_proposal(
            source_ir,
            base_tree,
            (),
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=new_parent.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )
        proposal_id = stable_agent_structure_proposal_record_id(
            project_id="project-1",
            document_id="document-1",
            source_sha256=source_ir.source_sha256,
            proposal_revision=1,
            structure_patch_proposal_id=patch_proposal.proposal_id,
        )
        self.record = AgentStructureProposalV1(
            proposal_record_id=proposal_id,
            project_id="project-1",
            document_id="document-1",
            source_sha256=source_ir.source_sha256,
            facts_sha256="8" * 64,
            proposal_revision=1,
            structure_patch_proposal=patch_proposal,
            proposal_payload_sha256=structure_patch_proposal_sha256(patch_proposal),
            runtime_receipt=AgentRuntimeReceiptV1(
                runtime_adapter_id="codex-app-server",
                runtime_version="0.148.0-alpha.15",
                model_id="gpt-5.6-sol",
                skill_id="reqpilotx-structure-tree-generation",
                skill_version="1.0.0",
                output_schema_sha256="1" * 64,
                request_sha256="2" * 64,
                response_sha256="3" * 64,
                tool_receipt_sha256="4" * 64,
            ),
            runtime_thread_id="runtime-thread-metadata",
            created_at_utc=_NOW,
            expires_at_utc=_NOW + timedelta(minutes=15),
        )
        self.repository = InMemoryAgentStructureProposalRepository()

    def test_create_is_immutable_and_exact_replay_is_idempotent(self) -> None:
        created = self.repository.create(self.record, command_id="create-1")
        replay = self.repository.create(self.record, command_id="create-1")

        self.assertEqual(created, self.record)
        self.assertEqual(replay, self.record)
        self.assertEqual(
            self.repository.get_latest(
                project_id="project-1",
                document_id="document-1",
            ),
            self.record,
        )
        changed = AgentStructureProposalV1.model_validate(
            {
                **self.record.model_dump(mode="json"),
                "facts_sha256": "9" * 64,
            }
        )
        with self.assertRaisesRegex(
            AgentStructureProposalConflict,
            "replay changed payload|another payload",
        ):
            self.repository.create(changed, command_id="create-1")

    def test_apply_transition_is_cas_and_does_not_rewrite_proposal(self) -> None:
        self.repository.create(self.record, command_id="create-1")
        applied = AgentStructureProposalV1.model_validate(
            {
                **self.record.model_dump(mode="json"),
                "status": "APPLIED",
                "applied_structure_revision_id": "structure-revision-abc",
                "applied_structure_revision_number": 1,
                "applied_effective_snapshot_id": ("effective-structure-" + ("a" * 32)),
                "applied_command_id": "apply-1",
                "applied_at_utc": (_NOW + timedelta(minutes=1)).isoformat(),
            }
        )

        result = self.repository.transition(
            proposal_record_id=self.record.proposal_record_id,
            expected_status=AgentStructureProposalStatus.READY,
            proposal=applied,
            command_id="apply-1",
        )
        replay = self.repository.transition(
            proposal_record_id=self.record.proposal_record_id,
            expected_status=AgentStructureProposalStatus.READY,
            proposal=applied,
            command_id="apply-1",
        )

        self.assertEqual(result, applied)
        self.assertEqual(replay, applied)
        with self.assertRaisesRegex(
            AgentStructureProposalConflict,
            "status changed",
        ):
            self.repository.transition(
                proposal_record_id=self.record.proposal_record_id,
                expected_status=AgentStructureProposalStatus.READY,
                proposal=AgentStructureProposalV1.model_validate(
                    {
                        **self.record.model_dump(mode="json"),
                        "status": "REJECTED",
                        "status_reason_code": "user_rejected",
                    }
                ),
                command_id="reject-after-apply",
            )

    def test_contract_rejects_unbound_payload_and_incomplete_apply(self) -> None:
        with self.assertRaisesRegex(ValueError, "proposal_payload_sha256"):
            AgentStructureProposalV1.model_validate(
                {
                    **self.record.model_dump(mode="json"),
                    "proposal_payload_sha256": "0" * 64,
                }
            )
        with self.assertRaisesRegex(ValueError, "complete Apply receipt"):
            AgentStructureProposalV1.model_validate(
                {
                    **self.record.model_dump(mode="json"),
                    "status": "APPLIED",
                }
            )

    def test_postgres_migration_is_additive_and_keeps_apply_outside_model(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations/postgresql/0030_agent_structure_proposals.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS agent_structure_proposals", migration)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS agent_structure_proposal_events", migration
        )
        self.assertIn("proposal_payload_sha256", migration)
        self.assertIn("UNIQUE(proposal_id, command_id)", migration)
        self.assertIn("REFERENCES sor_documents", migration)
        self.assertNotIn("UPDATE agent_structure_proposals", migration)
        self.assertIn("never grant Apply authority", migration)

    def test_application_apply_is_idempotent_and_calls_typed_writer_once(self) -> None:
        ready = create_ready_agent_structure_proposal(
            self.repository,
            project_id=self.record.project_id,
            document_id=self.record.document_id,
            source_sha256=self.record.source_sha256,
            facts_sha256=self.record.facts_sha256,
            structure_patch_proposal=self.record.structure_patch_proposal,
            runtime_receipt=self.record.runtime_receipt,
            runtime_thread_id=self.record.runtime_thread_id,
            command_id="create-service",
            now=_NOW,
        )
        calls: list[str] = []

        def apply_writer(proposal, command_id):
            self.assertEqual(proposal, ready.structure_patch_proposal)
            calls.append(command_id)
            return AgentStructureApplyReceiptV1(
                structure_revision_id="structure-revision-service",
                structure_revision_number=1,
                effective_structure_snapshot_id="effective-structure-" + ("b" * 32),
                appended=True,
            )

        applied, receipt = apply_agent_structure_proposal(
            self.repository,
            proposal_record_id=ready.proposal_record_id,
            command_id="apply-service",
            apply_structure=apply_writer,
            now=_NOW + timedelta(minutes=1),
        )
        replay, replay_receipt = apply_agent_structure_proposal(
            self.repository,
            proposal_record_id=ready.proposal_record_id,
            command_id="apply-service",
            apply_structure=apply_writer,
            now=_NOW + timedelta(minutes=2),
        )

        self.assertEqual(calls, ["apply-service"])
        self.assertEqual(applied.status, AgentStructureProposalStatus.APPLIED)
        self.assertEqual(replay, applied)
        self.assertTrue(receipt.appended)
        self.assertFalse(replay_receipt.appended)
        self.assertEqual(
            replay_receipt.effective_structure_snapshot_id,
            receipt.effective_structure_snapshot_id,
        )

    def test_revised_proposal_stales_ready_predecessor_and_replay_repairs_it(
        self,
    ) -> None:
        first = create_ready_agent_structure_proposal(
            self.repository,
            project_id=self.record.project_id,
            document_id=self.record.document_id,
            source_sha256=self.record.source_sha256,
            facts_sha256=self.record.facts_sha256,
            structure_patch_proposal=self.record.structure_patch_proposal,
            runtime_receipt=self.record.runtime_receipt,
            runtime_thread_id=self.record.runtime_thread_id,
            command_id="create-first",
            now=_NOW,
        )
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
        revised_patch = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (
                ReparentStructureOperationV1(
                    structure_node_id=second_target.structure_node_id,
                    new_parent_structure_node_id=new_parent_id,
                    new_sibling_index=0,
                ),
            ),
        )

        revised = create_ready_agent_structure_proposal(
            self.repository,
            project_id=self.record.project_id,
            document_id=self.record.document_id,
            source_sha256=self.record.source_sha256,
            facts_sha256=self.record.facts_sha256,
            structure_patch_proposal=revised_patch,
            runtime_receipt=self.record.runtime_receipt,
            runtime_thread_id="revised-runtime-thread",
            command_id="create-revised",
            now=_NOW + timedelta(minutes=1),
        )
        replay = create_ready_agent_structure_proposal(
            self.repository,
            project_id=self.record.project_id,
            document_id=self.record.document_id,
            source_sha256=self.record.source_sha256,
            facts_sha256=self.record.facts_sha256,
            structure_patch_proposal=revised_patch,
            runtime_receipt=self.record.runtime_receipt,
            runtime_thread_id="revised-runtime-thread",
            command_id="create-revised",
            now=_NOW + timedelta(minutes=2),
        )

        self.assertEqual(replay, revised)
        self.assertEqual(revised.proposal_revision, 2)
        self.assertEqual(revised.supersedes_proposal_record_id, first.proposal_record_id)
        predecessor = self.repository.get(first.proposal_record_id)
        self.assertEqual(predecessor.status, AgentStructureProposalStatus.STALE)
        self.assertEqual(
            predecessor.status_reason_code,
            "superseded_by_revised_proposal",
        )

    def test_expired_proposal_never_calls_structure_writer(self) -> None:
        ready = create_ready_agent_structure_proposal(
            self.repository,
            project_id=self.record.project_id,
            document_id=self.record.document_id,
            source_sha256=self.record.source_sha256,
            facts_sha256=self.record.facts_sha256,
            structure_patch_proposal=self.record.structure_patch_proposal,
            runtime_receipt=self.record.runtime_receipt,
            runtime_thread_id="",
            command_id="create-expiring",
            now=_NOW,
            ttl_seconds=60,
        )
        called = False

        def forbidden_writer(_proposal, _command_id):
            nonlocal called
            called = True
            raise AssertionError("expired proposal reached Structure writer")

        with self.assertRaisesRegex(
            AgentStructureProposalConflict,
            "expired before Apply",
        ):
            apply_agent_structure_proposal(
                self.repository,
                proposal_record_id=ready.proposal_record_id,
                command_id="apply-expired",
                apply_structure=forbidden_writer,
                now=_NOW + timedelta(minutes=2),
            )
        self.assertFalse(called)
        self.assertEqual(
            self.repository.get(ready.proposal_record_id).status,
            AgentStructureProposalStatus.EXPIRED,
        )


if __name__ == "__main__":
    unittest.main()
