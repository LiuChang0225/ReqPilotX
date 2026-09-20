from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock

from app.ports.agent_structure_steer_repository import (
    AgentStructureSteerCommand,
    AgentStructureSteerConflict,
    AgentStructureSteerStatus,
)


class InMemoryAgentStructureSteerRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, AgentStructureSteerCommand] = {}
        self._command_ids: dict[tuple[str, str], str] = {}

    def create(self, command: AgentStructureSteerCommand) -> AgentStructureSteerCommand:
        with self._lock:
            key = (command.job_id, command.command_id)
            existing_id = self._command_ids.get(key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.payload_sha256 != command.payload_sha256:
                    raise AgentStructureSteerConflict(
                        "steer command replay changed payload"
                    )
                return existing
            existing = self._records.get(command.steer_command_id)
            if existing is not None and existing != command:
                raise AgentStructureSteerConflict(
                    "steer identity already owns another payload"
                )
            self._records[command.steer_command_id] = command
            self._command_ids[key] = command.steer_command_id
            return command

    def get_latest_for_job(self, job_id: str) -> AgentStructureSteerCommand | None:
        with self._lock:
            candidates = [item for item in self._records.values() if item.job_id == job_id]
        return max(candidates, key=lambda item: item.created_at_utc, default=None)

    def claim_next(
        self,
        *,
        job_id: str,
        expected_job_generation: int,
    ) -> AgentStructureSteerCommand | None:
        with self._lock:
            candidates = sorted(
                (
                    item
                    for item in self._records.values()
                    if item.job_id == job_id
                    and item.expected_job_generation == expected_job_generation
                    and item.status is AgentStructureSteerStatus.PENDING
                ),
                key=lambda item: item.created_at_utc,
            )
            if not candidates:
                return None
            current = candidates[0]
            claimed = replace(
                current,
                status=AgentStructureSteerStatus.DELIVERING,
                updated_at_utc=datetime.now(timezone.utc),
            )
            self._records[current.steer_command_id] = claimed
            return claimed

    def mark_delivered(
        self,
        *,
        steer_command_id: str,
        expected_payload_sha256: str,
        runtime_thread_id: str,
        runtime_turn_id: str,
    ) -> AgentStructureSteerCommand:
        return self._transition(
            steer_command_id,
            expected_payload_sha256=expected_payload_sha256,
            status=AgentStructureSteerStatus.DELIVERED,
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
            steer_command_id,
            expected_payload_sha256=expected_payload_sha256,
            status=AgentStructureSteerStatus.REJECTED,
            reason_code=reason_code,
        )

    def reject_unfinished(
        self,
        *,
        job_id: str,
        expected_job_generation: int,
        reason_code: str,
    ) -> int:
        return self._reject_matching(
            lambda item: item.job_id == job_id
            and item.expected_job_generation == expected_job_generation,
            reason_code,
        )

    def reject_other_generations(
        self,
        *,
        job_id: str,
        current_job_generation: int,
        reason_code: str,
    ) -> int:
        return self._reject_matching(
            lambda item: item.job_id == job_id
            and item.expected_job_generation != current_job_generation,
            reason_code,
        )

    def _transition(
        self,
        steer_command_id: str,
        *,
        expected_payload_sha256: str,
        status: AgentStructureSteerStatus,
        reason_code: str = "",
        runtime_thread_id: str = "",
        runtime_turn_id: str = "",
    ) -> AgentStructureSteerCommand:
        with self._lock:
            current = self._records.get(steer_command_id)
            if current is None or current.payload_sha256 != expected_payload_sha256:
                raise AgentStructureSteerConflict("steer command changed")
            if current.status is status:
                if (
                    current.status_reason_code == reason_code
                    and current.runtime_thread_id == runtime_thread_id
                    and current.runtime_turn_id == runtime_turn_id
                ):
                    return current
                raise AgentStructureSteerConflict("steer replay changed delivery")
            if current.status not in {
                AgentStructureSteerStatus.PENDING,
                AgentStructureSteerStatus.DELIVERING,
            }:
                raise AgentStructureSteerConflict("steer status changed")
            updated = replace(
                current,
                status=status,
                status_reason_code=reason_code,
                runtime_thread_id=runtime_thread_id,
                runtime_turn_id=runtime_turn_id,
                updated_at_utc=datetime.now(timezone.utc),
            )
            self._records[steer_command_id] = updated
            return updated

    def _reject_matching(self, predicate, reason_code: str) -> int:
        count = 0
        with self._lock:
            for command_id, current in tuple(self._records.items()):
                if not predicate(current) or current.status not in {
                    AgentStructureSteerStatus.PENDING,
                    AgentStructureSteerStatus.DELIVERING,
                }:
                    continue
                self._records[command_id] = replace(
                    current,
                    status=AgentStructureSteerStatus.REJECTED,
                    status_reason_code=reason_code,
                    updated_at_utc=datetime.now(timezone.utc),
                )
                count += 1
        return count


__all__ = ["InMemoryAgentStructureSteerRepository"]
