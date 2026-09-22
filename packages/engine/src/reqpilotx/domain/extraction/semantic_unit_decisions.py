from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx.domain.extraction.semantic_units import SemanticUnit
from reqpilotx.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticClass,
    SemanticGroundingSpan,
)


SEMANTIC_UNIT_DECISION_REQUEST_SCHEMA_VERSION = "semantic-unit-decision-request/v1"
SEMANTIC_UNIT_DECISION_SCHEMA_VERSION = "semantic-unit-decision/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_SEMANTIC_SPAN_ID_PATTERN = re.compile(r"^semantic-span-[0-9a-f]{32}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SemanticUnitDecisionEvidence(_StrictFrozenModel):
    """Exact text view for one source-bound unit span."""

    grounding_span: SemanticGroundingSpan
    text: str = Field(min_length=1, repr=False)
    text_sha256: str

    @field_validator("text_sha256")
    @classmethod
    def validate_text_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("unit decision evidence requires a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_text_binding(self) -> SemanticUnitDecisionEvidence:
        if len(self.text) != (
            self.grounding_span.end_offset - self.grounding_span.start_offset
        ):
            raise ValueError(
                "unit decision evidence length must match its Unicode span"
            )
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("unit decision evidence hash does not match its text")
        return self


class SemanticUnitDecisionRequest(_StrictFrozenModel):
    """One provider-neutral request targeting exactly one SemanticUnit."""

    schema_version: Literal["semantic-unit-decision-request/v1"] = (
        SEMANTIC_UNIT_DECISION_REQUEST_SCHEMA_VERSION
    )
    decision_request_id: str = Field(
        pattern=r"^semantic-unit-decision-request-[0-9a-f]{32}$"
    )
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    semantic_unit_plan_id: str = Field(pattern=r"^semantic-unit-plan-[0-9a-f]{32}$")
    unitizer_version: str = Field(min_length=1, max_length=160)
    semantic_unit: SemanticUnit
    evidence: tuple[SemanticUnitDecisionEvidence, ...]

    @field_validator("source_sha256")
    @classmethod
    def validate_source_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(
                "unit decision requests require a lowercase source SHA-256"
            )
        return value

    @field_validator(
        "source_ir_id",
        "structure_snapshot_id",
        "unitizer_version",
    )
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("unit decision request identity cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> SemanticUnitDecisionRequest:
        if not self.evidence:
            raise ValueError("unit decision requests require source-bound evidence")
        evidence_spans = tuple(item.grounding_span for item in self.evidence)
        if evidence_spans != self.semantic_unit.source_spans:
            raise ValueError(
                "unit decision evidence must exactly cover its semantic unit"
            )
        expected_request_id = stable_semantic_unit_decision_request_id(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            structure_snapshot_id=self.structure_snapshot_id,
            semantic_unit_plan_id=self.semantic_unit_plan_id,
            unitizer_version=self.unitizer_version,
            semantic_unit=self.semantic_unit,
            evidence=self.evidence,
        )
        if self.decision_request_id != expected_request_id:
            raise ValueError(
                "decision_request_id must be derived from its unit and evidence"
            )
        return self

    @property
    def semantic_unit_id(self) -> str:
        return self.semantic_unit.semantic_unit_id

    @property
    def owner_structure_node_id(self) -> str:
        return self.semantic_unit.owner_structure_node_id


class SemanticUnitDecision(_StrictFrozenModel):
    """One accepted classification decision bound to one unit request."""

    schema_version: Literal["semantic-unit-decision/v1"] = (
        SEMANTIC_UNIT_DECISION_SCHEMA_VERSION
    )
    decision_request_id: str = Field(
        pattern=r"^semantic-unit-decision-request-[0-9a-f]{32}$"
    )
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    is_continuation_fragment: bool = False
    evidence_grounding_span_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @field_validator("evidence_grounding_span_ids")
    @classmethod
    def validate_evidence_span_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SEMANTIC_SPAN_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError(
                "unit decision evidence IDs must use stable span identities"
            )
        if len(value) != len(set(value)):
            raise ValueError("unit decision evidence IDs must be unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("unit decision reason codes must be machine-readable")
        if value != tuple(sorted(set(value))):
            raise ValueError("unit decision reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> SemanticUnitDecision:
        if self.semantic_class is SemanticClass.REQUIREMENT:
            if self.requirement_subtype is None:
                raise ValueError("requirement unit decisions require a subtype")
        elif self.requirement_subtype is not None:
            raise ValueError("only requirement unit decisions may carry a subtype")
        if (
            self.is_continuation_fragment
            and self.semantic_class is not SemanticClass.REQUIREMENT
        ):
            raise ValueError(
                "only requirement unit decisions may be continuation fragments"
            )
        if self.semantic_class is SemanticClass.UNRESOLVED:
            if not self.reason_codes:
                raise ValueError("unresolved unit decisions require a reason code")
            if self.is_continuation_fragment:
                raise ValueError(
                    "unresolved unit decisions cannot claim a continuation"
                )
        elif not self.evidence_grounding_span_ids:
            raise ValueError("resolved unit decisions require exact span evidence")
        return self


def stable_semantic_unit_decision_request_id(
    *,
    source_ir_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    semantic_unit_plan_id: str,
    unitizer_version: str,
    semantic_unit: SemanticUnit,
    evidence: tuple[SemanticUnitDecisionEvidence, ...],
) -> str:
    for value, field_name in (
        (source_ir_id, "source_ir_id"),
        (structure_snapshot_id, "structure_snapshot_id"),
        (semantic_unit_plan_id, "semantic_unit_plan_id"),
        (unitizer_version, "unitizer_version"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be blank")
    if not _SHA256_PATTERN.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256")
    if not isinstance(semantic_unit, SemanticUnit):
        raise TypeError("semantic_unit must be a SemanticUnit")
    if not isinstance(evidence, tuple) or not evidence:
        raise ValueError("unit decision requests require evidence")
    if any(not isinstance(item, SemanticUnitDecisionEvidence) for item in evidence):
        raise TypeError("evidence must contain SemanticUnitDecisionEvidence values")

    evidence_binding = [
        {
            "grounding_span": item.grounding_span.model_dump(mode="json"),
            "text_sha256": item.text_sha256,
        }
        for item in evidence
    ]
    payload = json.dumps(
        (
            source_ir_id,
            source_sha256,
            structure_snapshot_id,
            semantic_unit_plan_id,
            unitizer_version,
            semantic_unit.model_dump(mode="json"),
            evidence_binding,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"semantic-unit-decision-request-{digest}"


__all__ = [
    "SEMANTIC_UNIT_DECISION_REQUEST_SCHEMA_VERSION",
    "SEMANTIC_UNIT_DECISION_SCHEMA_VERSION",
    "SemanticUnitDecision",
    "SemanticUnitDecisionEvidence",
    "SemanticUnitDecisionRequest",
    "stable_semantic_unit_decision_request_id",
]
