from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from reqpilotx_server.domain.extraction.structure_revision import StructurePatchOperationV1


class AgentStructureSteerStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class AgentStructureSteerCommand:
    steer_command_id: str
    job_id: str
    project_id: str
    document_id: str
    command_id: str
    expected_job_generation: int
    source_sha256: str
    expected_revision_number: int
    expected_effective_snapshot_id: str
    intent: str
    focus_structure_node_ids: tuple[str, ...]
    suggested_operations: tuple[StructurePatchOperationV1, ...]
    suggested_operations_authority: str
    payload_sha256: str
    status: AgentStructureSteerStatus
    status_reason_code: str
    runtime_thread_id: str
    runtime_turn_id: str
    created_at_utc: datetime
    updated_at_utc: datetime


class AgentStructureSteerConflict(RuntimeError):
    pass


@runtime_checkable
class AgentStructureSteerRepository(Protocol):
    def create(
        self,
        command: AgentStructureSteerCommand,
    ) -> AgentStructureSteerCommand: ...

    def get_latest_for_job(self, job_id: str) -> AgentStructureSteerCommand | None: ...

    def claim_next(
        self,
        *,
        job_id: str,
        expected_job_generation: int,
    ) -> AgentStructureSteerCommand | None: ...

    def mark_delivered(
        self,
        *,
        steer_command_id: str,
        expected_payload_sha256: str,
        runtime_thread_id: str,
        runtime_turn_id: str,
    ) -> AgentStructureSteerCommand: ...

    def mark_rejected(
        self,
        *,
        steer_command_id: str,
        expected_payload_sha256: str,
        reason_code: str,
    ) -> AgentStructureSteerCommand: ...

    def reject_unfinished(
        self,
        *,
        job_id: str,
        expected_job_generation: int,
        reason_code: str,
    ) -> int: ...

    def reject_other_generations(
        self,
        *,
        job_id: str,
        current_job_generation: int,
        reason_code: str,
    ) -> int: ...


__all__ = [
    "AgentStructureSteerCommand",
    "AgentStructureSteerConflict",
    "AgentStructureSteerRepository",
    "AgentStructureSteerStatus",
]
