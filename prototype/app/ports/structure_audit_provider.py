from __future__ import annotations

import re
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
)
from app.domain.extraction.structure import StructureSourceRef
from app.domain.extraction.structure_revision import StructurePatchOperationV1
from app.ports.semantic_provider import SemanticClassificationRequest


_STRUCTURE_NODE_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureAuditCategory(str, Enum):
    POSSIBLE_MISSED_BOUNDARY = "possible_missed_boundary"
    POSSIBLE_MISPLACED_PARENT = "possible_misplaced_parent"
    POSSIBLE_SOURCE_ORDER_ERROR = "possible_source_order_error"
    POSSIBLE_DUPLICATE_CONTENT = "possible_duplicate_content"
    POSSIBLE_TABLE_CONTINUATION_ERROR = "possible_table_continuation_error"


class StructureAuditProposalEvidence(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureAuditPhysicalOrderInput(_StrictFrozenModel):
    """Native SourceIR order for one structure node's own source anchors."""

    structure_node_id: str = Field(min_length=1, max_length=160)
    physical_source_orders: tuple[int, ...] = ()

    @field_validator("structure_node_id")
    @classmethod
    def validate_structure_node_id(cls, value: str) -> str:
        if not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("audit node IDs must use stable structure identities")
        return value

    @field_validator("physical_source_orders")
    @classmethod
    def validate_physical_source_orders(
        cls,
        value: tuple[int, ...],
    ) -> tuple[int, ...]:
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in value
        ):
            raise ValueError("physical source orders must be non-negative integers")
        if value != tuple(sorted(set(value))):
            raise ValueError("physical source orders must be unique and sorted")
        return value


class StructureAuditContinuationInput(_StrictFrozenModel):
    """One currently grounded CONTINUES relation visible to the reviewer."""

    source_structure_node_id: str = Field(min_length=1, max_length=160)
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)

    @field_validator(
        "source_structure_node_id",
        "target_structure_node_id",
    )
    @classmethod
    def validate_structure_node_id(cls, value: str) -> str:
        if not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "audit continuation IDs must use stable structure identities"
            )
        return value

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(
        cls,
        value: tuple[StructureSourceRef, ...],
    ) -> tuple[StructureSourceRef, ...]:
        if len(value) != len(set(value)):
            raise ValueError("audit continuation source refs must be unique")
        return value

    @model_validator(mode="after")
    def validate_relation_shape(self) -> StructureAuditContinuationInput:
        if self.source_structure_node_id == self.target_structure_node_id:
            raise ValueError("audit continuation cannot reference itself")
        return self


class StructureAuditRequest(_StrictFrozenModel):
    """Source-bound evidence plus the physical outline needed to challenge a tree."""

    evidence_request: SemanticClassificationRequest
    physical_order: tuple[StructureAuditPhysicalOrderInput, ...]
    outline_spine_structure_node_ids: tuple[str, ...] = ()
    current_continuations: tuple[StructureAuditContinuationInput, ...] = ()

    @field_validator("outline_spine_structure_node_ids")
    @classmethod
    def validate_outline_spine_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("outline spine IDs must use stable structure identities")
        if len(value) != len(set(value)):
            raise ValueError("outline spine IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_request_coverage(self) -> StructureAuditRequest:
        request_nodes = (
            *self.evidence_request.nodes,
            *self.evidence_request.context_nodes,
        )
        request_ids = tuple(item.target_structure_node_id for item in request_nodes)
        request_source_refs = {
            evidence.source_ref
            for node in request_nodes
            for evidence in node.evidence
        }
        position_ids = tuple(item.structure_node_id for item in self.physical_order)
        if len(position_ids) != len(set(position_ids)) or set(position_ids) != set(
            request_ids
        ):
            raise ValueError(
                "audit physical order must exactly cover target and context nodes"
            )
        if not set(self.outline_spine_structure_node_ids).issubset(request_ids):
            raise ValueError(
                "audit outline spine must reference target or context nodes"
            )
        continuation_identities = tuple(
            (
                item.source_structure_node_id,
                item.target_structure_node_id,
            )
            for item in self.current_continuations
        )
        if len(continuation_identities) != len(set(continuation_identities)):
            raise ValueError("audit continuation relations must be unique")
        if any(
            source_id not in request_ids or target_id not in request_ids
            for source_id, target_id in continuation_identities
        ):
            raise ValueError(
                "audit continuation endpoints must be target or context nodes"
            )
        if any(
            not set(item.source_refs).issubset(request_source_refs)
            for item in self.current_continuations
        ):
            raise ValueError(
                "audit continuation grounding must be present in request evidence"
            )
        return self


class StructureAuditDecision(_StrictFrozenModel):
    """One provider suspicion plus an optional, still-untrusted patch intent."""

    category: StructureAuditCategory
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    related_structure_node_ids: tuple[str, ...] = ()
    evidence_source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    proposed_operations: tuple[StructurePatchOperationV1, ...] = Field(
        default=(),
        max_length=1,
    )
    proposal_evidence: StructureAuditProposalEvidence | None = None

    @field_validator("target_structure_node_id")
    @classmethod
    def validate_target_structure_node_id(cls, value: str) -> str:
        if not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("audit target ID must use a stable structure identity")
        return value

    @field_validator("related_structure_node_ids")
    @classmethod
    def validate_related_structure_node_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("audit related IDs must use stable structure identities")
        if len(value) != len(set(value)):
            raise ValueError("audit related IDs must be unique")
        return value

    @field_validator("evidence_source_refs")
    @classmethod
    def validate_evidence_source_refs(
        cls,
        value: tuple[StructureSourceRef, ...],
    ) -> tuple[StructureSourceRef, ...]:
        if len(value) != len(set(value)):
            raise ValueError("audit evidence source refs must be unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("audit reason codes must use machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("audit reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_decision_shape(self) -> StructureAuditDecision:
        if self.target_structure_node_id in self.related_structure_node_ids:
            raise ValueError("audit target cannot also be a related node")
        if bool(self.proposed_operations) != (self.proposal_evidence is not None):
            raise ValueError(
                "audit proposal evidence is required exactly when an operation is proposed"
            )
        return self


class StructureAuditProviderResult(_StrictFrozenModel):
    findings: tuple[StructureAuditDecision, ...] = Field(max_length=3)
    run: SemanticProviderRunRecord
    rejected_finding_count: int = Field(default=0, ge=0, le=3)

    @model_validator(mode="after")
    def validate_result_shape(self) -> StructureAuditProviderResult:
        if (
            self.run.status
            not in {
                SemanticProviderRunStatus.SUCCESS,
                SemanticProviderRunStatus.PARTIAL,
            }
            and (self.findings or self.rejected_finding_count)
        ):
            raise ValueError(
                "failed audit runs cannot claim or reject findings"
            )
        if (
            self.rejected_finding_count
            and self.run.status is not SemanticProviderRunStatus.PARTIAL
        ):
            raise ValueError(
                "rejected audit findings require a partial provider run"
            )
        if len(self.findings) + self.rejected_finding_count > 3:
            raise ValueError("audit provider result exceeds the finding limit")
        return self


@runtime_checkable
class StructureAuditProviderPort(Protocol):
    provider_id: str
    provider_version: str
    model_id: str

    def audit_structure(
        self,
        request: StructureAuditRequest,
    ) -> StructureAuditProviderResult: ...


__all__ = [
    "StructureAuditCategory",
    "StructureAuditContinuationInput",
    "StructureAuditDecision",
    "StructureAuditPhysicalOrderInput",
    "StructureAuditProposalEvidence",
    "StructureAuditProviderPort",
    "StructureAuditProviderResult",
    "StructureAuditRequest",
]
