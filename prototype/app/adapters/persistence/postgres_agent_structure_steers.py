from __future__ import annotations

import json
from datetime import timezone
from typing import Any, Mapping

from pydantic import TypeAdapter

from app.domain.extraction.structure_revision import StructurePatchOperationV1
from app.infrastructure.postgres import PostgresDatabase
from app.ports.agent_structure_steer_repository import (
    AgentStructureSteerCommand,
    AgentStructureSteerConflict,
    AgentStructureSteerStatus,
)


class PostgresAgentStructureSteerRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def create(self, command: AgentStructureSteerCommand) -> AgentStructureSteerCommand:
        with self._database.connection() as connection:
            connection.execute(
                """
                INSERT INTO agent_structure_steer_commands (
                    id, job_id, project_id, sor_document_id, command_id,
                    expected_job_generation, source_sha256,
                    expected_revision_number, expected_effective_snapshot_id,
                    intent, focus_structure_node_ids, suggested_operations,
                    suggested_operations_authority, payload_sha256, status,
                    status_reason_code, runtime_thread_id, runtime_turn_id,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    command.steer_command_id,
                    command.job_id,
                    command.project_id,
                    command.document_id,
                    command.command_id,
                    command.expected_job_generation,
                    command.source_sha256,
                    command.expected_revision_number,
                    command.expected_effective_snapshot_id,
                    command.intent,
                    _json(list(command.focus_structure_node_ids)),
                    _json(
                        [
                            operation.model_dump(mode="json")
                            for operation in command.suggested_operations
                        ]
                    ),
                    command.suggested_operations_authority,
                    command.payload_sha256,
                    command.status.value,
                    command.status_reason_code,
                    command.runtime_thread_id,
                    command.runtime_turn_id,
                    command.created_at_utc,
                    command.updated_at_utc,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_structure_steer_commands WHERE id = %s",
                (command.steer_command_id,),
            ).fetchone()
            persisted = _record(row)
            if persisted != command:
                raise AgentStructureSteerConflict(
                    "steer identity already owns another payload"
                )
            return persisted

    def get_latest_for_job(self, job_id: str) -> AgentStructureSteerCommand | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_structure_steer_commands
                WHERE job_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return _record(row)

    def claim_next(
        self,
        *,
        job_id: str,
        expected_job_generation: int,
    ) -> AgentStructureSteerCommand | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM agent_structure_steer_commands
                    WHERE job_id = %s
                      AND expected_job_generation = %s
                      AND status = 'PENDING'
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE agent_structure_steer_commands AS command
                SET status = 'DELIVERING', updated_at = now()
                FROM candidate
                WHERE command.id = candidate.id
                RETURNING command.*
                """,
                (job_id, expected_job_generation),
            ).fetchone()
        return _record(row)

    def mark_delivered(
        self,
        *,
        steer_command_id: str,
        expected_payload_sha256: str,
        runtime_thread_id: str,
        runtime_turn_id: str,
    ) -> AgentStructureSteerCommand:
        return self._transition(
            steer_command_id=steer_command_id,
            expected_payload_sha256=expected_payload_sha256,
            target=AgentStructureSteerStatus.DELIVERED,
            runtime_thread_id=runtime_thread_id,
            runtime_turn_id=runtime_turn_id,
        )

    def mark_rejected(
        self,
        *,
        steer_command_id: str,
        expected_payload_sha256: str,
        reason_code: str,
    ) -> AgentStructureSteerCommand:
        return self._transition(
            steer_command_id=steer_command_id,
            expected_payload_sha256=expected_payload_sha256,
            target=AgentStructureSteerStatus.REJECTED,
            reason_code=reason_code,
        )

    def reject_unfinished(
        self,
        *,
        job_id: str,
        expected_job_generation: int,
        reason_code: str,
    ) -> int:
        return self._reject(
            "job_id = %s AND expected_job_generation = %s",
            (job_id, expected_job_generation),
            reason_code,
        )

    def reject_other_generations(
        self,
        *,
        job_id: str,
        current_job_generation: int,
        reason_code: str,
    ) -> int:
        return self._reject(
            "job_id = %s AND expected_job_generation <> %s",
            (job_id, current_job_generation),
            reason_code,
        )

    def _transition(
        self,
        *,
        steer_command_id: str,
        expected_payload_sha256: str,
        target: AgentStructureSteerStatus,
        reason_code: str = "",
        runtime_thread_id: str = "",
        runtime_turn_id: str = "",
    ) -> AgentStructureSteerCommand:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                UPDATE agent_structure_steer_commands
                SET status = %s,
                    status_reason_code = %s,
                    runtime_thread_id = %s,
                    runtime_turn_id = %s,
                    updated_at = now()
                WHERE id = %s
                  AND payload_sha256 = %s
                  AND status IN ('PENDING', 'DELIVERING')
                RETURNING *
                """,
                (
                    target.value,
                    reason_code,
                    runtime_thread_id,
                    runtime_turn_id,
                    steer_command_id,
                    expected_payload_sha256,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM agent_structure_steer_commands WHERE id = %s",
                    (steer_command_id,),
                ).fetchone()
            persisted = _record(row)
            if (
                persisted is None
                or persisted.payload_sha256 != expected_payload_sha256
                or persisted.status is not target
                or persisted.status_reason_code != reason_code
                or persisted.runtime_thread_id != runtime_thread_id
                or persisted.runtime_turn_id != runtime_turn_id
            ):
                raise AgentStructureSteerConflict("steer status changed")
            return persisted

    def _reject(
        self,
        predicate: str,
        parameters: tuple[Any, ...],
        reason_code: str,
    ) -> int:
        with self._database.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE agent_structure_steer_commands
                SET status = 'REJECTED',
                    status_reason_code = %s,
                    updated_at = now()
                WHERE {predicate}
                  AND status IN ('PENDING', 'DELIVERING')
                """,  # noqa: S608 -- predicate is a private constant fragment.
                (reason_code, *parameters),
            )
            return int(cursor.rowcount or 0)


def _record(row: Mapping[str, Any] | None) -> AgentStructureSteerCommand | None:
    if row is None:
        return None
    try:
        focus = tuple(str(item) for item in _list(row.get("focus_structure_node_ids")))
        operations = TypeAdapter(
            tuple[StructurePatchOperationV1, ...]
        ).validate_python(_list(row.get("suggested_operations")))
        return AgentStructureSteerCommand(
            steer_command_id=str(row["id"]),
            job_id=str(row["job_id"]),
            project_id=str(row["project_id"]),
            document_id=str(row["sor_document_id"]),
            command_id=str(row["command_id"]),
            expected_job_generation=int(row["expected_job_generation"]),
            source_sha256=str(row["source_sha256"]),
            expected_revision_number=int(row["expected_revision_number"]),
            expected_effective_snapshot_id=str(
                row["expected_effective_snapshot_id"]
            ),
            intent=str(row["intent"]),
            focus_structure_node_ids=focus,
            suggested_operations=operations,
            suggested_operations_authority=str(
                row["suggested_operations_authority"]
            ),
            payload_sha256=str(row["payload_sha256"]),
            status=AgentStructureSteerStatus(str(row["status"])),
            status_reason_code=str(row.get("status_reason_code") or ""),
            runtime_thread_id=str(row.get("runtime_thread_id") or ""),
            runtime_turn_id=str(row.get("runtime_turn_id") or ""),
            created_at_utc=row["created_at"].astimezone(timezone.utc),
            updated_at_utc=row["updated_at"].astimezone(timezone.utc),
        )
    except Exception as exc:
        raise AgentStructureSteerConflict("stored steer command is invalid") from exc


def _list(value: object) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("expected JSON array")
    return value


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = ["PostgresAgentStructureSteerRepository"]
