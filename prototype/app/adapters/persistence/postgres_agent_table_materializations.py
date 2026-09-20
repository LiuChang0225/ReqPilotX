from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.adapters.persistence.agent_table_materialization_support import (
    same_projection_identity,
    validate_materialization_command_id,
    validated_materialization_scope,
)
from app.application.extraction.agent_table_boundaries import (
    AgentTableProjectionMaterializationV1,
)
from app.application.extraction.agent_table_materialization_contracts import (
    AgentTableMaterializationRecordV1,
    stable_agent_table_materialization_record_id,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentLogicalTableScopeV1,
)
from app.domain.extraction.table_evidence import TableEvidenceBindingV1
from app.domain.extraction.table_records import TableRecordProjectionV1
from app.infrastructure.postgres import PostgresDatabase
from app.ports.agent_table_materialization_repository import (
    AgentTableMaterializationConflict,
)


_AGENT_ACTION = "APPLY_AGENT_BOUNDARIES"


class PostgresAgentTableMaterializationRepository:
    """Atomic Agent materialization + existing projection-revision adapter."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

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
        command_payload_sha256 = _command_payload_sha256(
            base_projection=base_projection,
            logical_table_scope=logical_table_scope,
            materialization=materialization,
            evidence_bindings=evidence_bindings,
        )
        with self._database.connection() as connection:
            _lock_projection_scope(connection, scope)
            replay = connection.execute(
                """
                SELECT m.*, c.command_payload_sha256
                FROM agent_table_materialization_commands AS c
                JOIN agent_table_materializations AS m
                  ON m.id = c.materialization_id
                WHERE c.project_id = %s
                  AND c.sor_document_id = %s
                  AND c.source_sha256 = %s
                  AND c.effective_structure_snapshot_id = %s
                  AND c.materializer_logical_table_id = %s
                  AND c.command_id = %s
                """,
                (*scope, command_id),
            ).fetchone()
            if replay is not None:
                if (
                    str(replay["command_payload_sha256"])
                    != command_payload_sha256
                ):
                    raise AgentTableMaterializationConflict(
                        "materialization command replay changed payload"
                    )
                record = _record(replay)
                if record is None:
                    raise AgentTableMaterializationConflict(
                        "materialization command points to a missing record"
                    )
                return record, False

            head = connection.execute(
                """
                SELECT *
                FROM table_projection_revisions
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND structure_snapshot_id = %s
                  AND logical_table_id = %s
                ORDER BY revision DESC
                LIMIT 1
                FOR UPDATE
                """,
                scope,
            ).fetchone()
            existing_row = connection.execute(
                """
                SELECT *
                FROM agent_table_materializations
                WHERE id = %s
                   OR (
                        project_id = %s
                    AND sor_document_id = %s
                    AND source_sha256 = %s
                    AND effective_structure_snapshot_id = %s
                    AND materializer_logical_table_id = %s
                    AND materialization_sha256 = %s
                   )
                """,
                (
                    record_id,
                    *scope,
                    materialization.materialization_sha256,
                ),
            ).fetchone()
            if existing_row is not None:
                existing = _record(existing_row)
                if (
                    existing is None
                    or existing.record_id != record_id
                    or existing.logical_table_scope != logical_table_scope
                    or existing.materialization != materialization
                    or existing.base_projection_sha256
                    != base_projection.projection_sha256
                    or existing.evidence_bindings != evidence_bindings
                ):
                    raise AgentTableMaterializationConflict(
                        "materialization identity owns another immutable payload"
                    )
                if (
                    head is None
                    or str(head["id"])
                    != existing.table_projection_revision_id
                ):
                    raise AgentTableMaterializationConflict(
                        "materialization already exists outside the current head"
                    )
                _insert_command(
                    connection,
                    scope=scope,
                    command_id=command_id,
                    record_id=record_id,
                    command_payload_sha256=command_payload_sha256,
                )
                return existing, False

            _require_current_base_projection(
                head=head,
                base_projection=base_projection,
            )
            next_projection_revision = (
                _max_projection_content_revision(
                    connection,
                    scope=scope,
                    base_revision=base_projection.projection_revision,
                )
                + 1
            )
            if (
                materialization.projection.projection_revision
                != next_projection_revision
            ):
                raise AgentTableMaterializationConflict(
                    "materialized projection does not use the next lineage revision"
                )

            revision_id = str(uuid.uuid4())
            head_revision = int((head or {}).get("revision") or 0) + 1
            created_at = datetime.now(timezone.utc)
            projection_payload = materialization.projection.model_dump(mode="json")
            evidence_payload = [
                binding.model_dump(mode="json") for binding in evidence_bindings
            ]
            connection.execute(
                """
                INSERT INTO table_projection_revisions (
                    id, project_id, sor_document_id, source_sha256,
                    structure_snapshot_id, logical_table_id, revision, action,
                    projection_id, projection_sha256, projection_json,
                    evidence_bindings_json, supersedes_id, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    revision_id,
                    project_id,
                    document_id,
                    materialization.source_sha256,
                    materialization.materializer_structure_snapshot_id,
                    materialization.materializer_logical_table_id,
                    head_revision,
                    _AGENT_ACTION,
                    materialization.projection.projection_id,
                    materialization.projection.projection_sha256,
                    _json(projection_payload),
                    _json(evidence_payload),
                    str(head["id"]) if head is not None else "",
                    created_at.isoformat(),
                ),
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
                created_at_utc=created_at,
            )
            connection.execute(
                """
                INSERT INTO agent_table_materializations (
                    id, project_id, sor_document_id, source_sha256,
                    effective_structure_snapshot_id,
                    materializer_logical_table_id, boundary_artifact_id,
                    boundary_artifact_sha256, agent_logical_table_id,
                    logical_table_scope_sha256, d1_table_map_artifact_id,
                    d1_table_map_artifact_sha256, logical_table_scope_payload,
                    materialization_sha256, base_projection_id,
                    base_projection_revision, base_projection_sha256,
                    output_projection_id, output_projection_revision,
                    output_projection_sha256, table_projection_revision_id,
                    table_projection_head_revision, materialization_payload,
                    evidence_bindings, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s::jsonb, %s
                )
                """,
                (
                    record.record_id,
                    record.project_id,
                    record.document_id,
                    materialization.source_sha256,
                    materialization.materializer_structure_snapshot_id,
                    materialization.materializer_logical_table_id,
                    materialization.boundary_artifact_id,
                    materialization.boundary_artifact_sha256,
                    materialization.agent_logical_table_id,
                    logical_table_scope.scope_sha256,
                    logical_table_scope.d1_table_map_artifact.artifact_id,
                    logical_table_scope.d1_table_map_artifact.artifact_sha256,
                    _json(logical_table_scope.model_dump(mode="json")),
                    materialization.materialization_sha256,
                    record.base_projection_id,
                    record.base_projection_revision,
                    record.base_projection_sha256,
                    materialization.projection.projection_id,
                    materialization.projection.projection_revision,
                    materialization.projection.projection_sha256,
                    record.table_projection_revision_id,
                    record.table_projection_head_revision,
                    _json(materialization.model_dump(mode="json")),
                    _json(evidence_payload),
                    record.created_at_utc,
                ),
            )
            _insert_command(
                connection,
                scope=scope,
                command_id=command_id,
                record_id=record_id,
                command_payload_sha256=command_payload_sha256,
            )
            return record, True

    def get(
        self,
        *,
        project_id: str,
        document_id: str,
        record_id: str,
    ) -> AgentTableMaterializationRecordV1 | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM agent_table_materializations
                WHERE id = %s
                  AND project_id = %s
                  AND sor_document_id = %s
                """,
                (record_id, project_id, document_id),
            ).fetchone()
        return _record(row)


def _lock_projection_scope(connection: Any, scope: tuple[str, ...]) -> None:
    lock_key = "\x1f".join(("table-projection-revision/v1", *scope))
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (lock_key,),
    )


def _require_current_base_projection(
    *,
    head: Mapping[str, Any] | None,
    base_projection: TableRecordProjectionV1,
) -> None:
    if head is None or str(head.get("action") or "") == "RESTORE_AUTO":
        return
    projection_payload = _mapping(head.get("projection_json"))
    try:
        current = TableRecordProjectionV1.model_validate(projection_payload)
    except ValueError as exc:
        raise AgentTableMaterializationConflict(
            "current table projection head is invalid"
        ) from exc
    if not same_projection_identity(current, base_projection):
        raise AgentTableMaterializationConflict(
            "base table projection changed before materialization commit"
        )


def _max_projection_content_revision(
    connection: Any,
    *,
    scope: tuple[str, ...],
    base_revision: int,
) -> int:
    rows = connection.execute(
        """
        SELECT projection_json
        FROM table_projection_revisions
        WHERE project_id = %s
          AND sor_document_id = %s
          AND source_sha256 = %s
          AND structure_snapshot_id = %s
          AND logical_table_id = %s
          AND projection_json <> ''
        """,
        scope,
    ).fetchall()
    highest = base_revision
    for row in rows:
        payload = _mapping(row.get("projection_json"))
        revision = payload.get("projection_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise AgentTableMaterializationConflict(
                "stored table projection revision is invalid"
            )
        highest = max(highest, revision)
    return highest


def _insert_command(
    connection: Any,
    *,
    scope: tuple[str, ...],
    command_id: str,
    record_id: str,
    command_payload_sha256: str,
) -> None:
    connection.execute(
        """
        INSERT INTO agent_table_materialization_commands (
            project_id, sor_document_id, source_sha256,
            effective_structure_snapshot_id, materializer_logical_table_id,
            command_id, materialization_id, command_payload_sha256
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (*scope, command_id, record_id, command_payload_sha256),
    )


def _record(row: Mapping[str, Any] | None) -> AgentTableMaterializationRecordV1 | None:
    if row is None:
        return None
    materialization = AgentTableProjectionMaterializationV1.model_validate(
        _mapping(row.get("materialization_payload"))
    )
    logical_table_scope = AgentLogicalTableScopeV1.model_validate(
        _mapping(row.get("logical_table_scope_payload"))
    )
    evidence_payload = _sequence(row.get("evidence_bindings"))
    return AgentTableMaterializationRecordV1(
        record_id=str(row["id"]),
        project_id=str(row["project_id"]),
        document_id=str(row["sor_document_id"]),
        base_projection_id=str(row["base_projection_id"]),
        base_projection_revision=int(row["base_projection_revision"]),
        base_projection_sha256=str(row["base_projection_sha256"]),
        logical_table_scope=logical_table_scope,
        materialization=materialization,
        evidence_bindings=tuple(
            TableEvidenceBindingV1.model_validate(item)
            for item in evidence_payload
        ),
        table_projection_revision_id=str(row["table_projection_revision_id"]),
        table_projection_head_revision=int(row["table_projection_head_revision"]),
        created_at_utc=row["created_at"],
    )


def _command_payload_sha256(
    *,
    base_projection: TableRecordProjectionV1,
    logical_table_scope: AgentLogicalTableScopeV1,
    materialization: AgentTableProjectionMaterializationV1,
    evidence_bindings: tuple[TableEvidenceBindingV1, ...],
) -> str:
    payload = {
        "base_projection_id": base_projection.projection_id,
        "base_projection_revision": base_projection.projection_revision,
        "base_projection_sha256": base_projection.projection_sha256,
        "logical_table_scope_sha256": logical_table_scope.scope_sha256,
        "d1_table_map_artifact_id": (
            logical_table_scope.d1_table_map_artifact.artifact_id
        ),
        "d1_table_map_artifact_sha256": (
            logical_table_scope.d1_table_map_artifact.artifact_sha256
        ),
        "materialization_sha256": materialization.materialization_sha256,
        "evidence_bindings": [
            binding.model_dump(mode="json") for binding in evidence_bindings
        ],
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, Mapping):
            return dict(loaded)
    raise ValueError("persisted Agent table materialization JSON must be an object")


def _sequence(value: Any) -> list[dict[str, Any]]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, Sequence) or isinstance(loaded, (str, bytes)):
        raise ValueError("persisted Agent table evidence JSON must be an array")
    if any(not isinstance(item, Mapping) for item in loaded):
        raise ValueError("persisted Agent table evidence entries must be objects")
    return [dict(item) for item in loaded]


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = ["PostgresAgentTableMaterializationRepository"]
