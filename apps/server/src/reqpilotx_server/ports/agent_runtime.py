from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from reqpilotx_server.application.agent_workflow.contracts import AgentRuntimeReceiptV1
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure import StructureTreeSnapshot
from reqpilotx_server.domain.extraction.structure_revision import (
    EffectiveStructureSnapshotV1,
    StructurePatchOperationV1,
    StructurePatchProposalV1,
    StructureRevisionV1,
)


class AgentRuntimeUnavailableError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class AgentRuntimeInvocationError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class AgentStructureProposalTaskV1:
    """In-memory task assembled from current canonical facts at worker claim time."""

    run_id: str
    project_id: str
    document_id: str
    facts_sha256: str
    source_ir: SourceIRSnapshot
    base_structure: StructureTreeSnapshot
    structure_revisions: tuple[StructureRevisionV1, ...]
    effective_structure: EffectiveStructureSnapshotV1
    model_id: str
    reasoning_effort: str
    skill_id: str
    skill_version: str
    deadline_seconds: int
    intent: str = "GENERAL"
    focus_structure_node_ids: tuple[str, ...] = ()
    suggested_operations: tuple[StructurePatchOperationV1, ...] = ()
    suggested_operations_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class AgentStructureProposalResultV1:
    proposal: StructurePatchProposalV1 | None
    runtime_receipt: AgentRuntimeReceiptV1
    runtime_thread_id: str = ""
    no_proposal_reason_code: str = ""

    def __post_init__(self) -> None:
        if (self.proposal is None) != bool(self.no_proposal_reason_code):
            raise ValueError(
                "Agent result requires either a proposal or a no-proposal reason"
            )


@dataclass(frozen=True, slots=True)
class AgentStructureSteerV1:
    steer_command_id: str
    payload_sha256: str
    intent: str
    focus_structure_node_ids: tuple[str, ...] = ()
    suggested_operations: tuple[StructurePatchOperationV1, ...] = ()
    suggested_operations_authority: str = "NONE"


@runtime_checkable
class AgentRuntimeSteeringPort(Protocol):
    def next_command(self) -> AgentStructureSteerV1 | None: ...

    def mark_delivered(
        self,
        command: AgentStructureSteerV1,
        *,
        runtime_thread_id: str,
        runtime_turn_id: str,
    ) -> None: ...

    def mark_failed(
        self,
        command: AgentStructureSteerV1,
        *,
        reason_code: str,
    ) -> None: ...


@runtime_checkable
class AgentRuntimePort(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def skill_id(self) -> str: ...

    @property
    def skill_version(self) -> str: ...

    def propose_structure(
        self,
        task: AgentStructureProposalTaskV1,
        *,
        progress: Callable[[str], None],
        cancellation_requested: Callable[[], bool],
        steering: AgentRuntimeSteeringPort | None = None,
    ) -> AgentStructureProposalResultV1: ...


__all__ = [
    "AgentRuntimePort",
    "AgentRuntimeSteeringPort",
    "AgentRuntimeInvocationError",
    "AgentRuntimeUnavailableError",
    "AgentStructureProposalResultV1",
    "AgentStructureSteerV1",
    "AgentStructureProposalTaskV1",
]
