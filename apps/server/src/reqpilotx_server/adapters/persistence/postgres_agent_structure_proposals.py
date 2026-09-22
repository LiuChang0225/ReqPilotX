from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from reqpilotx_server.application.agent_workflow.contracts import (
    AgentRuntimeReceiptV1,
    AgentStructureProposalStatus,
    AgentStructureProposalV1,
)
from reqpilotx_server.domain.extraction.structure_revision import StructurePatchProposalV1
from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.ports.agent_structure_proposal_repository import (
    AgentStructureProposalConflict,
)


_COMMAND_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class PostgresAgentStructureProposalRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def create(
        self,
        proposal: AgentStructureProposalV1,
        *,
        command_id: str,
    ) -> AgentStructureProposalV1:
        command_id = _command_id(command_id)
        if proposal.status is not AgentStructureProposalStatus.READY:
            raise ValueError("new Agent Structure proposals must start READY")
        event_sha256 = _record_sha256(proposal)
        with self._database.connection() as connection:
            connection.execute(
                """
                INSERT INTO agent_structure_proposals (
                    id,
                    project_id,
                    sor_document_id,
                    source_sha256,
                    facts_sha256,
                    proposal_revision,
                    supersedes_proposal_id,
                    structure_patch_proposal_id,
                    proposal_payload_sha256,
                    proposal_payload,
                    runtime_receipt,
                    runtime_thread_id,
                    status,
                    status_reason_code,
                    created_at,
                    expires_at,
                    applied_structure_revision_id,
                    applied_structure_revision_number,
                    applied_effective_snapshot_id,
                    applied_command_id,
                    applied_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    proposal.proposal_record_id,
                    proposal.project_id,
                    proposal.document_id,
                    proposal.source_sha256,
                    proposal.facts_sha256,
                    proposal.proposal_revision,
                    proposal.supersedes_proposal_record_id,
                    proposal.structure_patch_proposal.proposal_id,
                    proposal.proposal_payload_sha256,
                    _json(proposal.structure_patch_proposal.model_dump(mode="json")),
                    _json(proposal.runtime_receipt.model_dump(mode="json")),
                    proposal.runtime_thread_id,
                    proposal.status.value,
                    proposal.status_reason_code,
                    proposal.created_at_utc,
                    proposal.expires_at_utc,
                    proposal.applied_structure_revision_id,
                    proposal.applied_structure_revision_number,
                    proposal.applied_effective_snapshot_id,
                    proposal.applied_command_id,
                    proposal.applied_at_utc,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_structure_proposals WHERE id = %s",
                (proposal.proposal_record_id,),
            ).fetchone()
            persisted = _record(row)
            if persisted is None or persisted != proposal:
                raise AgentStructureProposalConflict(
                    "proposal identity already owns another immutable payload"
                )
            connection.execute(
                """
                INSERT INTO agent_structure_proposal_events (
                    proposal_id,
                    command_id,
                    event_type,
                    from_status,
                    to_status,
                    event_payload_sha256
                ) VALUES (%s, %s, 'CREATED', NULL, 'READY', %s)
                ON CONFLICT (proposal_id, command_id) DO NOTHING
                """,
                (proposal.proposal_record_id, command_id, event_sha256),
            )
            event = connection.execute(
                """
                SELECT event_payload_sha256
                FROM agent_structure_proposal_events
                WHERE proposal_id = %s AND command_id = %s
                """,
                (proposal.proposal_record_id, command_id),
            ).fetchone()
            if event is None or str(event["event_payload_sha256"]) != event_sha256:
                raise AgentStructureProposalConflict(
                    "proposal command replay changed payload"
                )
            return persisted

    def get(self, proposal_record_id: str) -> AgentStructureProposalV1 | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_structure_proposals WHERE id = %s",
                (proposal_record_id,),
            ).fetchone()
        return _record(row)

    def get_latest(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> AgentStructureProposalV1 | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM agent_structure_proposals
                WHERE project_id = %s AND sor_document_id = %s
                ORDER BY proposal_revision DESC, created_at DESC
                LIMIT 1
                """,
                (project_id, document_id),
            ).fetchone()
        return _record(row)

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
        if proposal.status is AgentStructureProposalStatus.READY:
            raise ValueError("status transitions cannot target READY")
        event_sha256 = _record_sha256(proposal)
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_structure_proposals
                WHERE id = %s
                FOR UPDATE
                """,
                (proposal_record_id,),
            ).fetchone()
            current = _record(row)
            if current is None:
                raise AgentStructureProposalConflict("proposal does not exist")
            replay = connection.execute(
                """
                SELECT event_payload_sha256
                FROM agent_structure_proposal_events
                WHERE proposal_id = %s AND command_id = %s
                """,
                (proposal_record_id, command_id),
            ).fetchone()
            if replay is not None:
                if str(replay["event_payload_sha256"]) != event_sha256:
                    raise AgentStructureProposalConflict(
                        "transition command replay changed payload"
                    )
                if current != proposal:
                    raise AgentStructureProposalConflict(
                        "transition replay no longer matches current state"
                    )
                return current
            if current.status is not expected_status:
                raise AgentStructureProposalConflict("proposal status changed")
            _require_immutable_content(current, proposal)
            updated = connection.execute(
                """
                UPDATE agent_structure_proposals
                SET status = %s,
                    status_reason_code = %s,
                    applied_structure_revision_id = %s,
                    applied_structure_revision_number = %s,
                    applied_effective_snapshot_id = %s,
                    applied_command_id = %s,
                    applied_at = %s,
                    updated_at = now()
                WHERE id = %s AND status = %s
                RETURNING *
                """,
                (
                    proposal.status.value,
                    proposal.status_reason_code,
                    proposal.applied_structure_revision_id,
                    proposal.applied_structure_revision_number,
                    proposal.applied_effective_snapshot_id,
                    proposal.applied_command_id,
                    proposal.applied_at_utc,
                    proposal_record_id,
                    expected_status.value,
                ),
            ).fetchone()
            persisted = _record(updated)
            if persisted is None or persisted != proposal:
                raise AgentStructureProposalConflict("proposal status CAS failed")
            connection.execute(
                """
                INSERT INTO agent_structure_proposal_events (
                    proposal_id,
                    command_id,
                    event_type,
                    from_status,
                    to_status,
                    event_payload_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    proposal_record_id,
                    command_id,
                    proposal.status.value,
                    expected_status.value,
                    proposal.status.value,
                    event_sha256,
                ),
            )
            return persisted


def _record(row: Mapping[str, Any] | None) -> AgentStructureProposalV1 | None:
    if row is None:
        return None
    patch_payload = _mapping(row.get("proposal_payload"))
    runtime_payload = _mapping(row.get("runtime_receipt"))
    return AgentStructureProposalV1(
        proposal_record_id=str(row["id"]),
        project_id=str(row["project_id"]),
        document_id=str(row["sor_document_id"]),
        source_sha256=str(row["source_sha256"]),
        facts_sha256=str(row["facts_sha256"]),
        proposal_revision=int(row["proposal_revision"]),
        supersedes_proposal_record_id=(
            str(row["supersedes_proposal_id"])
            if row.get("supersedes_proposal_id") is not None
            else None
        ),
        structure_patch_proposal=StructurePatchProposalV1.model_validate(patch_payload),
        proposal_payload_sha256=str(row["proposal_payload_sha256"]),
        status=AgentStructureProposalStatus(str(row["status"])),
        runtime_receipt=AgentRuntimeReceiptV1.model_validate(runtime_payload),
        runtime_thread_id=str(row.get("runtime_thread_id") or ""),
        created_at_utc=row["created_at"],
        expires_at_utc=row["expires_at"],
        status_reason_code=str(row.get("status_reason_code") or ""),
        applied_structure_revision_id=str(
            row.get("applied_structure_revision_id") or ""
        ),
        applied_structure_revision_number=int(
            row.get("applied_structure_revision_number") or 0
        ),
        applied_effective_snapshot_id=str(
            row.get("applied_effective_snapshot_id") or ""
        ),
        applied_command_id=str(row.get("applied_command_id") or ""),
        applied_at_utc=row.get("applied_at"),
    )


def _require_immutable_content(
    current: AgentStructureProposalV1,
    target: AgentStructureProposalV1,
) -> None:
    mutable_fields = {
        "status",
        "status_reason_code",
        "applied_structure_revision_id",
        "applied_structure_revision_number",
        "applied_effective_snapshot_id",
        "applied_command_id",
        "applied_at_utc",
    }
    current_payload = current.model_dump(mode="json")
    target_payload = target.model_dump(mode="json")
    for field_name in mutable_fields:
        current_payload.pop(field_name)
        target_payload.pop(field_name)
    if current_payload != target_payload:
        raise AgentStructureProposalConflict(
            "status transition attempted to rewrite immutable content"
        )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, Mapping):
            return dict(loaded)
    raise ValueError("persisted proposal JSON must be an object")


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _record_sha256(value: AgentStructureProposalV1) -> str:
    return hashlib.sha256(value.model_dump_json().encode("utf-8")).hexdigest()


def _command_id(value: str) -> str:
    value = str(value or "").strip()
    if _COMMAND_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("command_id has an invalid shape")
    return value


__all__ = ["PostgresAgentStructureProposalRepository"]
