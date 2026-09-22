from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

from reqpilotx_server.application.extraction.agent_table_boundaries import (
    AgentTableProjectionMaterializationV1,
)
from reqpilotx_server.application.extraction.agent_table_materialization_contracts import (
    AgentTableMaterializationRecordV1,
    stable_agent_table_materialization_record_id,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    AgentLogicalTableScopeV1,
)
from reqpilotx_server.adapters.persistence.agent_table_materialization_support import (
    same_projection_identity,
    validate_materialization_command_id,
    validated_materialization_scope,
)
from reqpilotx_server.domain.extraction.table_evidence import TableEvidenceBindingV1
from reqpilotx_server.domain.extraction.table_records import TableRecordProjectionV1
from reqpilotx_server.ports.agent_table_materialization_repository import (
    AgentTableMaterializationConflict,
)


@dataclass(frozen=True, slots=True)
class _ProjectionHead:
    revision_id: str
    head_revision: int
    projection: TableRecordProjectionV1


class InMemoryAgentTableMaterializationRepository:
    """Strict test adapter for immutable materialization/projection CAS."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, AgentTableMaterializationRecordV1] = {}
        self._commands: dict[tuple[str, ...], str] = {}
        self._materializations: dict[tuple[str, ...], str] = {}
        self._heads: dict[tuple[str, ...], _ProjectionHead] = {}
        self._projection_revisions: dict[tuple[str, ...], set[int]] = {}

    def commit(
        self,
        *,
        project_id: str,
        document_id: str,
        command_id: str,
        base_projection: TableRecordProjectionV1,
        logical_table_scope: AgentLogicalTableScopeV1,
        materialization: AgentTableProjectionMaterializationV1,
        evidence_bindings: tuple[TableEvidenceBindingV1, ...],
    ) -> tuple[AgentTableMaterializationRecordV1, bool]:
        validate_materialization_command_id(command_id)
        scope = validated_materialization_scope(
            project_id=project_id,
            document_id=document_id,
            base_projection=base_projection,
            logical_table_scope=logical_table_scope,
            materialization=materialization,
        )
        record_id = stable_agent_table_materialization_record_id(
            project_id=project_id,
            document_id=document_id,
            materialization_sha256=materialization.materialization_sha256,
            logical_table_scope_sha256=logical_table_scope.scope_sha256,
        )
        command_key = (*scope, command_id)
        materialization_key = (
            *scope,
            materialization.materialization_sha256,
        )
        with self._lock:
            prior_record_id = self._commands.get(command_key)
            if prior_record_id is not None:
                if prior_record_id != record_id:
                    raise AgentTableMaterializationConflict(
                        "materialization command replay changed payload"
                    )
                return self._records[prior_record_id], False

            prior_scope_record_id = self._materializations.get(
                materialization_key
            )
            if (
                prior_scope_record_id is not None
                and prior_scope_record_id != record_id
            ):
                raise AgentTableMaterializationConflict(
                    "materialization hash was already bound to another scope"
                )

            current_head = self._heads.get(scope)
            existing = self._records.get(record_id)
            if existing is not None:
                if (
                    existing.logical_table_scope != logical_table_scope
                    or existing.materialization != materialization
                    or existing.base_projection_sha256
                    != base_projection.projection_sha256
                    or existing.evidence_bindings != evidence_bindings
                ):
                    raise AgentTableMaterializationConflict(
                        "materialization identity owns another immutable payload"
                    )
                if (
                    current_head is None
                    or current_head.revision_id
                    != existing.table_projection_revision_id
                ):
                    raise AgentTableMaterializationConflict(
                        "materialization already exists outside the current head"
                    )
                self._commands[command_key] = record_id
                return existing, False

            if current_head is not None and not same_projection_identity(
                current_head.projection,
                base_projection,
            ):
                raise AgentTableMaterializationConflict(
                    "base table projection changed before materialization commit"
                )
            historical_revisions = self._projection_revisions.setdefault(
                scope,
                set(),
            )
            next_projection_revision = max(
                {base_projection.projection_revision, *historical_revisions}
            ) + 1
            if (
                materialization.projection.projection_revision
                != next_projection_revision
            ):
                raise AgentTableMaterializationConflict(
                    "materialized projection does not use the next lineage revision"
                )
            revision_id = str(uuid.uuid4())
            head_revision = (
                1 if current_head is None else current_head.head_revision + 1
            )
            record = AgentTableMaterializationRecordV1(
                record_id=record_id,
                project_id=project_id,
                document_id=document_id,
                base_projection_id=base_projection.projection_id,
                base_projection_revision=base_projection.projection_revision,
                base_projection_sha256=base_projection.projection_sha256,
                logical_table_scope=logical_table_scope,
                materialization=materialization,
                evidence_bindings=evidence_bindings,
                table_projection_revision_id=revision_id,
                table_projection_head_revision=head_revision,
                created_at_utc=datetime.now(timezone.utc),
            )
            self._records[record_id] = record
            self._commands[command_key] = record_id
            self._materializations[materialization_key] = record_id
            self._heads[scope] = _ProjectionHead(
                revision_id=revision_id,
                head_revision=head_revision,
                projection=materialization.projection,
            )
            historical_revisions.add(materialization.projection.projection_revision)
            return record, True

    def get(
        self,
        *,
        project_id: str,
        document_id: str,
        record_id: str,
    ) -> AgentTableMaterializationRecordV1 | None:
        with self._lock:
            record = self._records.get(record_id)
        if (
            record is None
            or record.project_id != project_id
            or record.document_id != document_id
        ):
            return None
        return record

__all__ = ["InMemoryAgentTableMaterializationRepository"]
