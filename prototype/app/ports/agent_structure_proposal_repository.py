from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.application.agent_workflow.contracts import (
    AgentStructureProposalStatus,
    AgentStructureProposalV1,
)


class AgentStructureProposalConflict(RuntimeError):
    """Raised when an immutable proposal or optimistic status changed."""


@runtime_checkable
class AgentStructureProposalRepository(Protocol):
    def create(
        self,
        proposal: AgentStructureProposalV1,
        *,
        command_id: str,
    ) -> AgentStructureProposalV1: ...

    def get(self, proposal_record_id: str) -> AgentStructureProposalV1 | None: ...

    def get_latest(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> AgentStructureProposalV1 | None: ...

    def transition(
        self,
        *,
        proposal_record_id: str,
        expected_status: AgentStructureProposalStatus,
        proposal: AgentStructureProposalV1,
        command_id: str,
    ) -> AgentStructureProposalV1: ...


__all__ = [
    "AgentStructureProposalConflict",
    "AgentStructureProposalRepository",
]
