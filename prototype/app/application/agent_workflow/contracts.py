from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.domain.extraction.structure_revision import (
    StructurePatchOperationV1,
    StructurePatchProposalV1,
)


AGENT_STRUCTURE_PROPOSAL_SCHEMA_VERSION = "agent-structure-proposal/v1"
AGENT_RUNTIME_RECEIPT_SCHEMA_VERSION = "agent-runtime-receipt/v1"
AGENT_STRUCTURE_APPLY_RECEIPT_SCHEMA_VERSION = "agent-structure-apply-receipt/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AgentStructureProposalStatus(StrEnum):
    READY = "READY"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


class AgentRuntimeReceiptV1(_StrictFrozenModel):
    """Content-free provenance for one runtime-generated proposal."""

    schema_version: Literal["agent-runtime-receipt/v1"] = (
        AGENT_RUNTIME_RECEIPT_SCHEMA_VERSION
    )
    runtime_adapter_id: str = Field(min_length=1, max_length=120)
    runtime_version: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=120)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    skill_id: str = Field(min_length=1, max_length=120)
    skill_version: str = Field(min_length=1, max_length=120)
    output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: StrictInt = Field(default=0, ge=0)
    cached_input_tokens: StrictInt = Field(default=0, ge=0)
    output_tokens: StrictInt = Field(default=0, ge=0)


class AgentStructureApplyReceiptV1(_StrictFrozenModel):
    """Receipt returned by the existing typed Structure writer."""

    schema_version: Literal["agent-structure-apply-receipt/v1"] = (
        AGENT_STRUCTURE_APPLY_RECEIPT_SCHEMA_VERSION
    )
    structure_revision_id: str = Field(min_length=1, max_length=160)
    structure_revision_number: StrictInt = Field(ge=1)
    effective_structure_snapshot_id: str = Field(
        pattern=r"^effective-structure-[0-9a-f]{32}$"
    )
    appended: bool


class AgentStructureProposalStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent-structure-proposal-start-request/v1"] = (
        "agent-structure-proposal-start-request/v1"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision_number: StrictInt = Field(ge=0)
    expected_effective_snapshot_id: str = Field(
        pattern=r"^effective-structure-[0-9a-f]{32}$"
    )
    intent: Literal[
        "GENERAL",
        "REVIEW_PARENTAGE",
        "REVIEW_ORDER",
        "REVIEW_BOUNDARY",
        "REVIEW_RELATIONS",
    ] = "GENERAL"
    focus_structure_node_ids: tuple[str, ...] = Field(
        default=(),
        max_length=64,
    )
    suggested_operations: tuple[StructurePatchOperationV1, ...] = Field(
        default=(),
        max_length=64,
    )
    suggested_operations_authority: Literal[
        "NONE",
        "USER_REQUESTED",
        "DETERMINISTIC_FINDING",
    ] = "NONE"

    @field_validator("focus_structure_node_ids")
    @classmethod
    def validate_focus_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.startswith("structure-node-") for value in values
        ):
            raise ValueError("focus Structure node IDs must be unique stable IDs")
        return values

    @model_validator(mode="after")
    def validate_suggestion_authority(self) -> AgentStructureProposalStartRequest:
        if bool(self.suggested_operations) != (
            self.suggested_operations_authority != "NONE"
        ):
            raise ValueError(
                "suggested operations and their authority must be supplied together"
            )
        return self


class AgentStructureProposalReviseRequest(AgentStructureProposalStartRequest):
    schema_version: Literal["agent-structure-proposal-revise-request/v1"] = (
        "agent-structure-proposal-revise-request/v1"
    )
    proposal_record_id: str = Field(
        pattern=r"^agent-structure-proposal-[0-9a-f]{32}$"
    )
    expected_proposal_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_revision_direction(self) -> AgentStructureProposalReviseRequest:
        if (
            self.intent == "GENERAL"
            and not self.focus_structure_node_ids
            and not self.suggested_operations
        ):
            raise ValueError("proposal revision requires a structured direction")
        return self


class AgentStructureProposalSteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent-structure-proposal-steer-request/v1"] = (
        "agent-structure-proposal-steer-request/v1"
    )
    job_id: str = Field(min_length=1, max_length=160)
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_job_generation: StrictInt = Field(ge=1)
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision_number: StrictInt = Field(ge=0)
    expected_effective_snapshot_id: str = Field(
        pattern=r"^effective-structure-[0-9a-f]{32}$"
    )
    intent: Literal[
        "REVIEW_PARENTAGE",
        "REVIEW_ORDER",
        "REVIEW_BOUNDARY",
        "REVIEW_RELATIONS",
    ]
    focus_structure_node_ids: tuple[str, ...] = Field(default=(), max_length=64)
    suggested_operations: tuple[StructurePatchOperationV1, ...] = Field(
        default=(), max_length=64
    )
    suggested_operations_authority: Literal[
        "NONE",
        "USER_REQUESTED",
        "DETERMINISTIC_FINDING",
    ] = "NONE"

    @field_validator("focus_structure_node_ids")
    @classmethod
    def validate_focus_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.startswith("structure-node-") for value in values
        ):
            raise ValueError("focus Structure node IDs must be unique stable IDs")
        return values

    @model_validator(mode="after")
    def validate_direction(self) -> AgentStructureProposalSteerRequest:
        if bool(self.suggested_operations) != (
            self.suggested_operations_authority != "NONE"
        ):
            raise ValueError(
                "suggested operations and their authority must be supplied together"
            )
        if not self.focus_structure_node_ids and not self.suggested_operations:
            raise ValueError("active-turn steering requires a bounded focus or operation")
        return self


class AgentStructureProposalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_record_id: str = Field(pattern=r"^agent-structure-proposal-[0-9a-f]{32}$")
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_proposal_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentStructureProposalRejectRequest(AgentStructureProposalActionRequest):
    schema_version: Literal["agent-structure-proposal-reject-request/v1"] = (
        "agent-structure-proposal-reject-request/v1"
    )
    reason_code: Literal["user_rejected"] = "user_rejected"


class AgentStructureProposalApplyRequest(AgentStructureProposalActionRequest):
    schema_version: Literal["agent-structure-proposal-apply-request/v1"] = (
        "agent-structure-proposal-apply-request/v1"
    )


class AgentStructureProposalV1(_StrictFrozenModel):
    """One immutable, durable proposal with no direct Apply authority."""

    schema_version: Literal["agent-structure-proposal/v1"] = (
        AGENT_STRUCTURE_PROPOSAL_SCHEMA_VERSION
    )
    proposal_record_id: str = Field(pattern=r"^agent-structure-proposal-[0-9a-f]{32}$")
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_revision: StrictInt = Field(ge=1)
    supersedes_proposal_record_id: str | None = Field(
        default=None,
        pattern=r"^agent-structure-proposal-[0-9a-f]{32}$",
    )
    structure_patch_proposal: StructurePatchProposalV1
    proposal_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AgentStructureProposalStatus = AgentStructureProposalStatus.READY
    runtime_receipt: AgentRuntimeReceiptV1
    runtime_thread_id: str = Field(default="", max_length=200)
    created_at_utc: datetime
    expires_at_utc: datetime
    status_reason_code: str = Field(default="", max_length=120)
    applied_structure_revision_id: str = Field(default="", max_length=160)
    applied_structure_revision_number: StrictInt = Field(default=0, ge=0)
    applied_effective_snapshot_id: str = Field(default="", max_length=160)
    applied_command_id: str = Field(default="", max_length=200)
    applied_at_utc: datetime | None = None

    @model_validator(mode="after")
    def validate_identity_and_state(self) -> AgentStructureProposalV1:
        created = _utc(self.created_at_utc)
        expires = _utc(self.expires_at_utc)
        if expires <= created:
            raise ValueError("proposal expiry must follow creation")
        if (expires - created).total_seconds() > 24 * 60 * 60:
            raise ValueError("proposal expiry cannot exceed 24 hours")
        if self.structure_patch_proposal.source_sha256 != self.source_sha256:
            raise ValueError("Structure patch proposal source identity drifted")
        expected_payload_sha256 = structure_patch_proposal_sha256(
            self.structure_patch_proposal
        )
        if self.proposal_payload_sha256 != expected_payload_sha256:
            raise ValueError("proposal_payload_sha256 must bind the patch proposal")
        expected_id = stable_agent_structure_proposal_record_id(
            project_id=self.project_id,
            document_id=self.document_id,
            source_sha256=self.source_sha256,
            proposal_revision=self.proposal_revision,
            structure_patch_proposal_id=self.structure_patch_proposal.proposal_id,
        )
        if self.proposal_record_id != expected_id:
            raise ValueError("proposal_record_id must bind the complete identity")
        if (
            self.proposal_revision == 1
            and self.supersedes_proposal_record_id is not None
        ):
            raise ValueError("the first proposal revision cannot supersede another")
        if self.proposal_revision > 1 and self.supersedes_proposal_record_id is None:
            raise ValueError("a revised proposal must identify its predecessor")
        if self.supersedes_proposal_record_id == self.proposal_record_id:
            raise ValueError("a proposal cannot supersede itself")
        applied = self.status is AgentStructureProposalStatus.APPLIED
        if applied != bool(
            self.applied_structure_revision_id
            and self.applied_structure_revision_number > 0
            and self.applied_effective_snapshot_id
            and self.applied_command_id
            and self.applied_at_utc is not None
        ):
            raise ValueError("APPLIED status requires one complete Apply receipt")
        if not applied and any(
            (
                self.applied_structure_revision_id,
                self.applied_structure_revision_number,
                self.applied_effective_snapshot_id,
                self.applied_command_id,
            )
        ):
            raise ValueError("non-applied proposals cannot claim Apply identities")
        if (
            self.status
            in {
                AgentStructureProposalStatus.REJECTED,
                AgentStructureProposalStatus.STALE,
                AgentStructureProposalStatus.EXPIRED,
            }
            and not self.status_reason_code
        ):
            raise ValueError("terminal non-applied status requires a reason code")
        if (
            self.status is AgentStructureProposalStatus.READY
            and self.status_reason_code
        ):
            raise ValueError("READY proposal cannot carry a terminal reason")
        return self


def structure_patch_proposal_sha256(proposal: StructurePatchProposalV1) -> str:
    return _canonical_sha256(proposal.model_dump(mode="json"))


def stable_agent_structure_proposal_record_id(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    proposal_revision: int,
    structure_patch_proposal_id: str,
) -> str:
    payload = {
        "schema_version": AGENT_STRUCTURE_PROPOSAL_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "proposal_revision": proposal_revision,
        "structure_patch_proposal_id": structure_patch_proposal_id,
    }
    return f"agent-structure-proposal-{_canonical_sha256(payload)[:32]}"


def _canonical_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "AGENT_RUNTIME_RECEIPT_SCHEMA_VERSION",
    "AGENT_STRUCTURE_APPLY_RECEIPT_SCHEMA_VERSION",
    "AGENT_STRUCTURE_PROPOSAL_SCHEMA_VERSION",
    "AgentRuntimeReceiptV1",
    "AgentStructureApplyReceiptV1",
    "AgentStructureProposalStatus",
    "AgentStructureProposalActionRequest",
    "AgentStructureProposalApplyRequest",
    "AgentStructureProposalRejectRequest",
    "AgentStructureProposalReviseRequest",
    "AgentStructureProposalSteerRequest",
    "AgentStructureProposalStartRequest",
    "AgentStructureProposalV1",
    "stable_agent_structure_proposal_record_id",
    "structure_patch_proposal_sha256",
]
