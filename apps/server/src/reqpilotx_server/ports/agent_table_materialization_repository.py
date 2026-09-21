from __future__ import annotations

from typing import Protocol, runtime_checkable

from reqpilotx_server.application.extraction.agent_table_boundaries import (
    AgentTableProjectionMaterializationV1,
)
from reqpilotx_server.application.extraction.agent_table_materialization_contracts import (
    AgentTableMaterializationRecordV1,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    AgentLogicalTableScopeV1,
)
from reqpilotx_server.domain.extraction.table_evidence import TableEvidenceBindingV1
from reqpilotx_server.domain.extraction.table_records import TableRecordProjectionV1


class AgentTableMaterializationConflict(RuntimeError):
    """Raised when immutable lineage or projection CAS no longer matches."""


@runtime_checkable
class AgentTableMaterializationRepository(Protocol):
    """Atomically persist an Agent artifact and advance its projection head."""

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
    ) -> tuple[AgentTableMaterializationRecordV1, bool]: ...

    def get(
        self,
        *,
        project_id: str,
        document_id: str,
        record_id: str,
    ) -> AgentTableMaterializationRecordV1 | None: ...


__all__ = [
    "AgentTableMaterializationConflict",
    "AgentTableMaterializationRepository",
]
