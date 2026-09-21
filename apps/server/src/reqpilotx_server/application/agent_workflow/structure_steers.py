from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from reqpilotx_server.application.agent_workflow.contracts import (
    AgentStructureProposalSteerRequest,
)
from reqpilotx_server.ports.agent_structure_steer_repository import (
    AgentStructureSteerCommand,
    AgentStructureSteerRepository,
    AgentStructureSteerStatus,
)


def create_agent_structure_steer(
    repository: AgentStructureSteerRepository,
    *,
    project_id: str,
    document_id: str,
    payload: AgentStructureProposalSteerRequest,
    now: datetime | None = None,
) -> AgentStructureSteerCommand:
    steer_payload = {
        "schema_version": "agent-structure-steer-command/v1",
        "job_id": payload.job_id,
        "project_id": project_id,
        "document_id": document_id,
        "command_id": payload.command_id,
        "expected_job_generation": payload.expected_job_generation,
        "source_sha256": payload.expected_source_sha256,
        "expected_revision_number": payload.expected_revision_number,
        "expected_effective_snapshot_id": payload.expected_effective_snapshot_id,
        "intent": payload.intent,
        "focus_structure_node_ids": list(payload.focus_structure_node_ids),
        "suggested_operations": [
            operation.model_dump(mode="json")
            for operation in payload.suggested_operations
        ],
        "suggested_operations_authority": (
            payload.suggested_operations_authority
        ),
    }
    payload_sha256 = _canonical_sha256(steer_payload)
    steer_command_id = (
        "agent-structure-steer-"
        + _canonical_sha256(
            {
                "job_id": payload.job_id,
                "command_id": payload.command_id,
                "payload_sha256": payload_sha256,
            }
        )[:32]
    )
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return repository.create(
        AgentStructureSteerCommand(
            steer_command_id=steer_command_id,
            job_id=payload.job_id,
            project_id=project_id,
            document_id=document_id,
            command_id=payload.command_id,
            expected_job_generation=payload.expected_job_generation,
            source_sha256=payload.expected_source_sha256,
            expected_revision_number=payload.expected_revision_number,
            expected_effective_snapshot_id=payload.expected_effective_snapshot_id,
            intent=payload.intent,
            focus_structure_node_ids=payload.focus_structure_node_ids,
            suggested_operations=payload.suggested_operations,
            suggested_operations_authority=(
                payload.suggested_operations_authority
            ),
            payload_sha256=payload_sha256,
            status=AgentStructureSteerStatus.PENDING,
            status_reason_code="",
            runtime_thread_id="",
            runtime_turn_id="",
            created_at_utc=observed_at,
            updated_at_utc=observed_at,
        )
    )


def serialize_agent_structure_steer(
    command: AgentStructureSteerCommand | None,
) -> dict[str, Any] | None:
    if command is None:
        return None
    return {
        "schema_version": "agent-structure-steer-public/v1",
        "steer_command_id": command.steer_command_id,
        "job_id": command.job_id,
        "command_id": command.command_id,
        "expected_job_generation": command.expected_job_generation,
        "status": command.status.value,
        "status_reason_code": command.status_reason_code,
        "intent": command.intent,
        "focus_structure_node_ids": list(command.focus_structure_node_ids),
        "operation_count": len(command.suggested_operations),
        "payload_sha256": command.payload_sha256,
        "created_at_utc": command.created_at_utc.isoformat(),
        "updated_at_utc": command.updated_at_utc.isoformat(),
    }


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "create_agent_structure_steer",
    "serialize_agent_structure_steer",
]
