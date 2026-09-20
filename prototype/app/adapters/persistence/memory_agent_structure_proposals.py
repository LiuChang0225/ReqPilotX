from __future__ import annotations

from threading import RLock

from app.application.agent_workflow.contracts import (
    AgentStructureProposalStatus,
    AgentStructureProposalV1,
)
from app.ports.agent_structure_proposal_repository import (
    AgentStructureProposalConflict,
)


class InMemoryAgentStructureProposalRepository:
    """Strict test adapter with the same immutable/CAS semantics as PostgreSQL."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, AgentStructureProposalV1] = {}
        self._commands: dict[tuple[str, str], str] = {}

    def create(
        self,
        proposal: AgentStructureProposalV1,
        *,
        command_id: str,
    ) -> AgentStructureProposalV1:
        command_id = _command_id(command_id)
        proposal_digest = proposal.model_dump_json()
        with self._lock:
            command_key = (proposal.proposal_record_id, command_id)
            prior_digest = self._commands.get(command_key)
            if prior_digest is not None:
                if prior_digest != proposal_digest:
                    raise AgentStructureProposalConflict(
                        "proposal command replay changed payload"
                    )
                return self._records[proposal.proposal_record_id]
            existing = self._records.get(proposal.proposal_record_id)
            if existing is not None and existing != proposal:
                raise AgentStructureProposalConflict(
                    "proposal identity already owns another payload"
                )
            self._records[proposal.proposal_record_id] = proposal
            self._commands[command_key] = proposal_digest
            return proposal

    def get(self, proposal_record_id: str) -> AgentStructureProposalV1 | None:
        with self._lock:
            return self._records.get(proposal_record_id)

    def get_latest(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> AgentStructureProposalV1 | None:
        with self._lock:
            candidates = [
                proposal
                for proposal in self._records.values()
                if proposal.project_id == project_id
                and proposal.document_id == document_id
            ]
        return max(
            candidates,
            key=lambda item: (item.proposal_revision, item.created_at_utc),
            default=None,
        )

    def transition(
        self,
        *,
        proposal_record_id: str,
        expected_status: AgentStructureProposalStatus,
        proposal: AgentStructureProposalV1,
        command_id: str,
    ) -> AgentStructureProposalV1:
        command_id = _command_id(command_id)
        if proposal.proposal_record_id != proposal_record_id:
            raise AgentStructureProposalConflict("transition identity drifted")
        with self._lock:
            command_key = (proposal_record_id, command_id)
            prior_digest = self._commands.get(command_key)
            current = self._records.get(proposal_record_id)
            if current is None:
                raise AgentStructureProposalConflict("proposal does not exist")
            target_digest = proposal.model_dump_json()
            if prior_digest is not None:
                if prior_digest != target_digest:
                    raise AgentStructureProposalConflict(
                        "transition command replay changed payload"
                    )
                return current
            if current.status is not expected_status:
                raise AgentStructureProposalConflict("proposal status changed")
            if (
                current.structure_patch_proposal != proposal.structure_patch_proposal
                or current.proposal_payload_sha256 != proposal.proposal_payload_sha256
                or current.project_id != proposal.project_id
                or current.document_id != proposal.document_id
                or current.source_sha256 != proposal.source_sha256
            ):
                raise AgentStructureProposalConflict(
                    "status transition attempted to rewrite immutable content"
                )
            self._records[proposal_record_id] = proposal
            self._commands[command_key] = target_digest
            return proposal


def _command_id(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 200:
        raise ValueError("command_id must be between 1 and 200 characters")
    return value


__all__ = ["InMemoryAgentStructureProposalRepository"]
