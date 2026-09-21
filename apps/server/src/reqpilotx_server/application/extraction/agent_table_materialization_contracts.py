from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx_server.application.extraction.agent_table_boundaries import (
    AgentTableProjectionMaterializationV1,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    AgentLogicalTableScopeV1,
)
from reqpilotx_server.domain.extraction.table_evidence import TableEvidenceBindingV1


AGENT_TABLE_MATERIALIZATION_COMMIT_REQUEST_SCHEMA_VERSION = (
    "agent-table-materialization-commit-request/v1"
)
AGENT_TABLE_MATERIALIZATION_RECORD_SCHEMA_VERSION = (
    "agent-table-materialization-record/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AgentTableMaterializationCommitRequestV1(_StrictFrozenModel):
    """Commit one already-validated Agent table materialization.

    The request intentionally carries no row-group fallback or inference
    option.  The submitted artifact must already bind the Agent-owned parent
    boundaries to the deterministic table materializer output.
    """

    schema_version: Literal[
        "agent-table-materialization-commit-request/v1"
    ] = AGENT_TABLE_MATERIALIZATION_COMMIT_REQUEST_SCHEMA_VERSION
    command_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_effective_structure_snapshot_id: str = Field(
        pattern=r"^effective-structure-[0-9a-f]{32}$"
    )
    expected_base_projection_id: str = Field(
        pattern=r"^table-record-projection-[0-9a-f]{32}$"
    )
    expected_base_projection_revision: int = Field(ge=0)
    expected_base_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope: AgentLogicalTableScopeV1
    materialization: AgentTableProjectionMaterializationV1

    @model_validator(mode="after")
    def validate_expected_lineage(
        self,
    ) -> AgentTableMaterializationCommitRequestV1:
        materialization = self.materialization
        scope = self.logical_table_scope
        if materialization.source_sha256 != self.expected_source_sha256:
            raise ValueError("materialization source differs from expected source")
        if (
            scope.source_sha256 != self.expected_source_sha256
            or scope.logical_object_id != materialization.agent_logical_table_id
            or scope.scope_sha256
            != materialization.agent_logical_table_scope_sha256
            or scope.d1_table_map_artifact.artifact_id
            != materialization.d1_table_map_artifact_id
            or scope.d1_table_map_artifact.artifact_sha256
            != materialization.d1_table_map_artifact_sha256
            or scope.physical_table_ids
            != materialization.projection.table_fact.table_ids
        ):
            raise ValueError(
                "logical-table scope differs from materialization lineage"
            )
        if (
            materialization.materializer_structure_snapshot_id
            != self.expected_effective_structure_snapshot_id
        ):
            raise ValueError(
                "materialization structure differs from expected structure"
            )
        if (
            materialization.input_projection_id
            != self.expected_base_projection_id
            or materialization.input_projection_revision
            != self.expected_base_projection_revision
        ):
            raise ValueError(
                "materialization input differs from expected base projection"
            )
        return self


class AgentTableMaterializationRecordV1(_StrictFrozenModel):
    """Immutable persistence receipt for one projection-revision advance."""

    schema_version: Literal["agent-table-materialization-record/v1"] = (
        AGENT_TABLE_MATERIALIZATION_RECORD_SCHEMA_VERSION
    )
    record_id: str = Field(pattern=r"^agent-table-materialization-[0-9a-f]{32}$")
    project_id: str = Field(min_length=1, max_length=200)
    document_id: str = Field(min_length=1, max_length=200)
    base_projection_id: str = Field(
        pattern=r"^table-record-projection-[0-9a-f]{32}$"
    )
    base_projection_revision: int = Field(ge=0)
    base_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope: AgentLogicalTableScopeV1
    materialization: AgentTableProjectionMaterializationV1
    evidence_bindings: tuple[TableEvidenceBindingV1, ...] = Field(min_length=1)
    table_projection_revision_id: str = Field(min_length=1, max_length=200)
    table_projection_head_revision: int = Field(ge=1)
    created_at_utc: datetime

    @field_validator("project_id", "document_id", "table_projection_revision_id")
    @classmethod
    def validate_non_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Agent table materialization identities cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_record(self) -> AgentTableMaterializationRecordV1:
        materialization = self.materialization
        scope = self.logical_table_scope
        projection = materialization.projection
        if (
            self.base_projection_id != materialization.input_projection_id
            or self.base_projection_revision
            != materialization.input_projection_revision
        ):
            raise ValueError("record base projection differs from materialization")
        if (
            scope.source_ir_id != projection.table_fact.source_ir_id
            or scope.source_sha256 != materialization.source_sha256
            or scope.source_ir_canonical_sha256
            != projection.table_fact.source_ir_canonical_sha256
            or scope.logical_object_id != materialization.agent_logical_table_id
            or scope.scope_sha256
            != materialization.agent_logical_table_scope_sha256
            or scope.d1_table_map_artifact.artifact_id
            != materialization.d1_table_map_artifact_id
            or scope.d1_table_map_artifact.artifact_sha256
            != materialization.d1_table_map_artifact_sha256
            or scope.physical_table_ids != projection.table_fact.table_ids
        ):
            raise ValueError(
                "record logical-table scope differs from materialization"
            )
        expected_record_id = stable_agent_table_materialization_record_id(
            project_id=self.project_id,
            document_id=self.document_id,
            materialization_sha256=materialization.materialization_sha256,
            logical_table_scope_sha256=scope.scope_sha256,
        )
        if self.record_id != expected_record_id:
            raise ValueError("record ID does not bind materialization lineage")
        expected_record_ids: set[str | None] = {
            None,
            *(record.record_id for record in projection.records),
        }
        actual_record_ids = tuple(
            binding.record_id for binding in self.evidence_bindings
        )
        if (
            set(actual_record_ids) != expected_record_ids
            or len(actual_record_ids) != len(expected_record_ids)
        ):
            raise ValueError(
                "materialization evidence must cover the table and every record"
            )
        if any(
            binding.logical_table_id
            != materialization.materializer_logical_table_id
            or binding.source_table_ids != projection.table_fact.table_ids
            or binding.projection_id != projection.projection_id
            or binding.projection_revision != projection.projection_revision
            or binding.projection_sha256 != projection.projection_sha256
            for binding in self.evidence_bindings
        ):
            raise ValueError(
                "materialization evidence differs from the committed projection"
            )
        if self.created_at_utc.tzinfo is None or self.created_at_utc.utcoffset() is None:
            raise ValueError("materialization record timestamp must be timezone-aware")
        return self


def stable_agent_table_materialization_record_id(
    *,
    project_id: str,
    document_id: str,
    materialization_sha256: str,
    logical_table_scope_sha256: str,
) -> str:
    payload = {
        "schema_version": AGENT_TABLE_MATERIALIZATION_RECORD_SCHEMA_VERSION,
        "project_id": str(project_id),
        "document_id": str(document_id),
        "materialization_sha256": str(materialization_sha256),
        "logical_table_scope_sha256": str(logical_table_scope_sha256),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"agent-table-materialization-{digest[:32]}"


__all__ = [
    "AGENT_TABLE_MATERIALIZATION_COMMIT_REQUEST_SCHEMA_VERSION",
    "AGENT_TABLE_MATERIALIZATION_RECORD_SCHEMA_VERSION",
    "AgentTableMaterializationCommitRequestV1",
    "AgentTableMaterializationRecordV1",
    "stable_agent_table_materialization_record_id",
]
