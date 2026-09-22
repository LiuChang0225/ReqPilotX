from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
)
from reqpilotx.domain.extraction.semantics import RequirementSubtype, SemanticClass
from reqpilotx.domain.extraction.structure import StructureSourceRef


LLM_REQUIREMENT_REVIEW_SCHEMA_VERSION = "llm-requirement-review/v4"
LLM_REQUIREMENT_PREDICTION_SCHEMA_VERSION = "llm-requirement-prediction/v5"
LLM_REQUIREMENT_REVIEW_SESSION_SCHEMA_VERSION = "llm-requirement-review-session/v4"
LLM_REQUIREMENT_REVIEW_REFERENCE_CANDIDATE_SCHEMA_VERSION = (
    "llm-requirement-review-reference-candidate/v1"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STRUCTURE_NODE_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")
_MACHINE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LlmRequirementReviewStatus(str, Enum):
    DRAFT = "DRAFT"
    AI_REVIEWED_SILVER = "AI_REVIEWED_SILVER"
    HUMAN_REVIEWED = "HUMAN_REVIEWED"


class LlmRequirementReviewReferenceCandidateStatus(str, Enum):
    NOT_CAPTURED = "NOT_CAPTURED"
    SELF_ATTESTED_REVIEW = "SELF_ATTESTED_REVIEW"


class LlmRequirementReviewOperationType(str, Enum):
    CONFIRMED = "CONFIRMED"
    RECLASSIFIED = "RECLASSIFIED"
    SUBTYPE_CHANGED = "SUBTYPE_CHANGED"
    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"
    ROLE_CHANGED = "ROLE_CHANGED"
    DECISION_MODE_CHANGED = "DECISION_MODE_CHANGED"
    REPARENTED = "REPARENTED"
    PROMOTED = "PROMOTED"
    SPLIT = "SPLIT"
    MERGED = "MERGED"


class LlmRequirementPredictionNode(_StrictFrozenModel):
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    source_semantic_unit_ids: tuple[str, ...]
    owner_structure_node_id: str
    source_owner_structure_node_ids: tuple[str, ...]
    review_owner_semantic_unit_id: str | None = Field(
        default=None,
        pattern=r"^semantic-unit-[0-9a-f]{32}$",
    )
    review_owner_structure_node_id: str | None = None
    unit_order: int = Field(ge=0)
    source_order: int = Field(ge=0)
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    include_in_view: bool
    role: RequirementProjectionRole | None = None
    parent_semantic_unit_id: str | None = Field(
        default=None,
        pattern=r"^semantic-unit-[0-9a-f]{32}$",
    )
    parent_owner_structure_node_id: str | None = None
    decision_mode: RequirementDecisionMode
    source_refs: tuple[StructureSourceRef, ...]
    source_binding_sha256: str
    review_text: str = Field(max_length=500_000)
    review_text_sha256: str
    reason_codes: tuple[str, ...] = ()

    @field_validator(
        "owner_structure_node_id",
        "review_owner_structure_node_id",
        "parent_owner_structure_node_id",
    )
    @classmethod
    def validate_structure_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("review prediction nodes require stable structure IDs")
        return value

    @field_validator("source_binding_sha256", "review_text_sha256")
    @classmethod
    def validate_source_binding_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(
                "review prediction hashes must be lowercase SHA-256 values"
            )
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _MACHINE_KEY_PATTERN.fullmatch(item) for item in value):
            raise ValueError("review reason codes must be machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("review reason codes must be unique and sorted")
        return value

    @field_validator("source_semantic_unit_ids")
    @classmethod
    def validate_source_semantic_unit_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not re.fullmatch(r"semantic-unit-[0-9a-f]{32}", item) for item in value):
            raise ValueError("prediction source units require stable identities")
        return value

    @field_validator("source_owner_structure_node_ids")
    @classmethod
    def validate_source_owner_structure_node_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("prediction source owners require stable identities")
        return value

    @model_validator(mode="after")
    def validate_prediction_shape(self) -> LlmRequirementPredictionNode:
        if (
            not self.source_semantic_unit_ids
            or self.source_semantic_unit_ids[0] != self.semantic_unit_id
            or len(self.source_semantic_unit_ids)
            != len(set(self.source_semantic_unit_ids))
        ):
            raise ValueError(
                "prediction source units must uniquely begin with the primary unit"
            )
        if (
            not self.source_owner_structure_node_ids
            or self.source_owner_structure_node_ids[0] != self.owner_structure_node_id
            or len(self.source_owner_structure_node_ids)
            != len(self.source_semantic_unit_ids)
        ):
            raise ValueError(
                "prediction source units must retain ordered Structure ownership"
            )
        if (self.review_owner_semantic_unit_id is None) != (
            self.review_owner_structure_node_id is None
        ):
            raise ValueError("prediction review ownership requires unit and Structure")
        if (self.parent_semantic_unit_id is None) != (
            self.parent_owner_structure_node_id is None
        ):
            raise ValueError("prediction parentage requires unit and Structure")
        _validate_semantic_shape(self.semantic_class, self.requirement_subtype)
        _validate_projection_shape(
            semantic_class=self.semantic_class,
            include_in_view=self.include_in_view,
            role=self.role,
            parent_id=self.parent_semantic_unit_id,
            decision_mode=self.decision_mode,
        )
        if not self.source_refs:
            raise ValueError("every prediction node must retain source grounding")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("prediction source refs must be unique")
        expected_text_sha256 = hashlib.sha256(
            self.review_text.encode("utf-8")
        ).hexdigest()
        if self.review_text_sha256 != expected_text_sha256:
            raise ValueError("review_text_sha256 does not match review_text")
        return self

    @property
    def target_structure_node_id(self) -> str:
        return self.owner_structure_node_id

    @property
    def review_owner_target_structure_node_id(self) -> str | None:
        return self.review_owner_structure_node_id

    @property
    def parent_target_structure_node_id(self) -> str | None:
        return self.parent_owner_structure_node_id


class LlmRequirementModelPrediction(_StrictFrozenModel):
    schema_version: Literal["llm-requirement-prediction/v5"] = (
        LLM_REQUIREMENT_PREDICTION_SCHEMA_VERSION
    )
    prediction_id: str = Field(pattern=r"^llm-prediction-[0-9a-f]{32}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    scope_locator_sha256: str
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=160)
    classification_prompt_version: str = Field(min_length=1, max_length=160)
    assembly_prompt_version: str = Field(min_length=1, max_length=160)
    semantic_unit_plan_ids: tuple[str, ...]
    semantic_unit_ids: tuple[str, ...]
    provider_run_ids: tuple[str, ...]
    nodes: tuple[LlmRequirementPredictionNode, ...]

    @field_validator("source_sha256", "scope_locator_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("prediction hashes must be lowercase SHA-256 values")
        return value

    @field_validator("semantic_unit_plan_ids")
    @classmethod
    def validate_semantic_unit_plan_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(
            not re.fullmatch(r"semantic-unit-plan-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("prediction unit plans require stable identities")
        return value

    @field_validator("semantic_unit_ids")
    @classmethod
    def validate_semantic_unit_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not re.fullmatch(r"semantic-unit-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("prediction units require stable identities")
        if len(value) != len(set(value)):
            raise ValueError("prediction semantic-unit scope must be unique")
        return value

    @model_validator(mode="after")
    def validate_prediction(self) -> LlmRequirementModelPrediction:
        if not self.nodes:
            raise ValueError("a model prediction must contain at least one target")
        targets = tuple(item.semantic_unit_id for item in self.nodes)
        orders = tuple((item.source_order, item.unit_order) for item in self.nodes)
        unit_orders = tuple(item.unit_order for item in self.nodes)
        if len(targets) != len(set(targets)):
            raise ValueError("model prediction targets must be unique")
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise ValueError(
                "model prediction targets require unique source/unit order"
            )
        if unit_orders != tuple(range(len(self.nodes))):
            raise ValueError("model prediction unit_order must be contiguous")
        target_set = set(targets)
        nodes_by_target = {item.semantic_unit_id: item for item in self.nodes}
        closed_review_owners: set[str] = set()
        previous_review_owner: str | None = None
        for node in self.nodes:
            if (
                node.parent_semantic_unit_id is not None
                and node.parent_semantic_unit_id not in target_set
            ):
                raise ValueError("prediction parent must be another predicted target")
            owner_target_id = (
                node.review_owner_semantic_unit_id or node.semantic_unit_id
            )
            owner = nodes_by_target.get(owner_target_id)
            if owner is None:
                raise ValueError("review source owner must be another predicted target")
            if owner_target_id != previous_review_owner:
                if owner_target_id in closed_review_owners:
                    raise ValueError(
                        "prediction review owners cannot interleave source order"
                    )
                if previous_review_owner is not None:
                    closed_review_owners.add(previous_review_owner)
                previous_review_owner = owner_target_id
            if node.parent_semantic_unit_id is not None:
                parent = nodes_by_target[node.parent_semantic_unit_id]
                if (
                    node.parent_owner_structure_node_id
                    != parent.owner_structure_node_id
                ):
                    raise ValueError("prediction parent grounding is inconsistent")
                if (parent.source_order, parent.unit_order) >= (
                    node.source_order,
                    node.unit_order,
                ):
                    raise ValueError(
                        "prediction parent must precede its child in unit order"
                    )
            if owner_target_id == node.semantic_unit_id:
                continue
            owner_owner_id = (
                owner.review_owner_semantic_unit_id or owner.semantic_unit_id
            )
            if owner_owner_id != owner.semantic_unit_id:
                raise ValueError("review source ownership cannot form a chain")
            if node.include_in_view or not owner.include_in_view:
                raise ValueError(
                    "only an excluded prediction source may belong to an included owner"
                )
            if (
                node.semantic_class is not owner.semantic_class
                or node.requirement_subtype is not owner.requirement_subtype
                or (node.source_order, node.unit_order)
                <= (owner.source_order, owner.unit_order)
            ):
                raise ValueError(
                    "owned continuation sources must follow compatible prediction owners"
                )
            if node.review_owner_structure_node_id != owner.owner_structure_node_id:
                raise ValueError("prediction review owner grounding is inconsistent")
        flattened_unit_ids = tuple(
            unit_id for node in self.nodes for unit_id in node.source_semantic_unit_ids
        )
        if flattened_unit_ids != self.semantic_unit_ids:
            raise ValueError("prediction nodes must partition the semantic-unit scope")
        if not self.semantic_unit_plan_ids or len(self.semantic_unit_plan_ids) != len(
            set(self.semantic_unit_plan_ids)
        ):
            raise ValueError("prediction semantic unit plans must be unique")
        if len(self.provider_run_ids) != len(set(self.provider_run_ids)):
            raise ValueError("provider run IDs must be unique")
        expected = stable_llm_requirement_prediction_id(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            structure_snapshot_id=self.structure_snapshot_id,
            scope_locator_sha256=self.scope_locator_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            model_id=self.model_id,
            classification_prompt_version=self.classification_prompt_version,
            assembly_prompt_version=self.assembly_prompt_version,
            semantic_unit_plan_ids=self.semantic_unit_plan_ids,
            semantic_unit_ids=self.semantic_unit_ids,
            provider_run_ids=self.provider_run_ids,
            nodes=self.nodes,
        )
        if self.prediction_id != expected:
            raise ValueError("prediction_id does not match immutable model output")
        return self


class LlmReviewSourceSpan(_StrictFrozenModel):
    """One exact Unicode-code-point range within an immutable model target."""

    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)

    @field_validator("owner_structure_node_id")
    @classmethod
    def validate_target_id(cls, value: str) -> str:
        if not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("review spans require stable structure IDs")
        return value

    @model_validator(mode="after")
    def validate_offsets(self) -> LlmReviewSourceSpan:
        if self.end_offset < self.start_offset:
            raise ValueError("review span end_offset must not precede start_offset")
        return self

    @property
    def target_structure_node_id(self) -> str:
        return self.owner_structure_node_id


class LlmReviewedRequirementNode(_StrictFrozenModel):
    review_node_id: str
    source_semantic_unit_ids: tuple[str, ...]
    source_owner_structure_node_ids: tuple[str, ...]
    source_spans: tuple[LlmReviewSourceSpan, ...]
    source_order: int = Field(ge=0)
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    include_in_view: bool
    role: RequirementProjectionRole | None = None
    parent_review_node_id: str | None = None
    decision_mode: RequirementDecisionMode
    reason_codes: tuple[str, ...] = ()

    @field_validator("review_node_id", "parent_review_node_id")
    @classmethod
    def reject_blank_review_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("review node IDs must not be blank")
        return value

    @field_validator("source_semantic_unit_ids")
    @classmethod
    def validate_source_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("review nodes require at least one source target")
        if any(not re.fullmatch(r"semantic-unit-[0-9a-f]{32}", item) for item in value):
            raise ValueError("review source targets must use semantic unit IDs")
        if len(value) != len(set(value)):
            raise ValueError("review source targets must be unique")
        return value

    @field_validator("source_owner_structure_node_ids")
    @classmethod
    def validate_source_owner_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value
        ):
            raise ValueError("review source owners require stable Structure IDs")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _MACHINE_KEY_PATTERN.fullmatch(item) for item in value):
            raise ValueError("review reason codes must be machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("review reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_reviewed_shape(self) -> LlmReviewedRequirementNode:
        if not self.source_spans:
            raise ValueError("review nodes require at least one exact source span")
        span_targets = tuple(
            dict.fromkeys(span.semantic_unit_id for span in self.source_spans)
        )
        if span_targets != self.source_semantic_unit_ids:
            raise ValueError("review source targets must match source span order")
        owner_by_unit: dict[str, str] = {}
        for span in self.source_spans:
            previous_owner = owner_by_unit.setdefault(
                span.semantic_unit_id,
                span.owner_structure_node_id,
            )
            if previous_owner != span.owner_structure_node_id:
                raise ValueError(
                    "one review source unit cannot change Structure ownership"
                )
        span_owners = tuple(owner_by_unit[unit_id] for unit_id in span_targets)
        if span_owners != self.source_owner_structure_node_ids:
            raise ValueError("review source owners must match source span order")
        if len(self.source_semantic_unit_ids) != len(
            self.source_owner_structure_node_ids
        ):
            raise ValueError("review source units must retain owner grounding")
        _validate_semantic_shape(self.semantic_class, self.requirement_subtype)
        _validate_projection_shape(
            semantic_class=self.semantic_class,
            include_in_view=self.include_in_view,
            role=self.role,
            parent_id=self.parent_review_node_id,
            decision_mode=self.decision_mode,
        )
        if self.parent_review_node_id == self.review_node_id:
            raise ValueError("a reviewed node cannot be its own parent")
        return self

    @property
    def source_target_structure_node_ids(self) -> tuple[str, ...]:
        return self.source_owner_structure_node_ids


class LlmRequirementReviewedResult(_StrictFrozenModel):
    schema_version: Literal["llm-requirement-review/v4"] = (
        LLM_REQUIREMENT_REVIEW_SCHEMA_VERSION
    )
    prediction_id: str = Field(pattern=r"^llm-prediction-[0-9a-f]{32}$")
    revision: int = Field(ge=0)
    status: LlmRequirementReviewStatus
    reviewer_label: str = Field(default="", max_length=120)
    nodes: tuple[LlmReviewedRequirementNode, ...]
    confirmed_review_node_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> LlmRequirementReviewedResult:
        if not self.nodes:
            raise ValueError("a reviewed result must contain at least one node")
        ids = tuple(item.review_node_id for item in self.nodes)
        orders = tuple(item.source_order for item in self.nodes)
        if len(ids) != len(set(ids)):
            raise ValueError("review node IDs must be unique")
        if orders != tuple(sorted(orders)):
            raise ValueError("review nodes require ascending source order")
        if len(self.confirmed_review_node_ids) != len(
            set(self.confirmed_review_node_ids)
        ):
            raise ValueError("confirmed review node IDs must be unique")
        unknown_confirmed_ids = set(self.confirmed_review_node_ids).difference(ids)
        if unknown_confirmed_ids:
            raise ValueError(
                "confirmed review node IDs must belong to the reviewed result"
            )
        confirmed_order = tuple(
            node_id for node_id in ids if node_id in set(self.confirmed_review_node_ids)
        )
        if self.confirmed_review_node_ids != confirmed_order:
            raise ValueError(
                "confirmed review node IDs must retain reviewed node order"
            )
        return self


class LlmRequirementReviewOperation(_StrictFrozenModel):
    operation: LlmRequirementReviewOperationType
    review_node_id: str
    source_semantic_unit_ids: tuple[str, ...]
    source_owner_structure_node_ids: tuple[str, ...]
    field: str | None = None
    before: str | bool | int | None = None
    after: str | bool | int | None = None

    @field_validator("review_node_id")
    @classmethod
    def reject_blank_review_node_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review operations require a review node ID")
        return value

    @field_validator("source_semantic_unit_ids")
    @classmethod
    def validate_source_semantic_unit_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not re.fullmatch(r"semantic-unit-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("review operations require stable semantic-unit IDs")
        if len(value) != len(set(value)):
            raise ValueError("review operation source units must be unique")
        return value

    @field_validator("source_owner_structure_node_ids")
    @classmethod
    def validate_source_owner_structure_node_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value
        ):
            raise ValueError("review operations require stable Structure owners")
        return value

    @model_validator(mode="after")
    def validate_source_lineage(self) -> LlmRequirementReviewOperation:
        if len(self.source_semantic_unit_ids) != len(
            self.source_owner_structure_node_ids
        ):
            raise ValueError(
                "review operation source units must retain owner grounding"
            )
        return self


class LlmRequirementReviewMetrics(_StrictFrozenModel):
    target_count: int = Field(ge=1)
    reviewed_node_count: int = Field(ge=1)
    confirmed_target_count: int = Field(ge=0)
    edited_target_count: int = Field(ge=0)
    confirmed_target_rate: float = Field(ge=0.0, le=1.0)
    requirement_subtype_compared_target_count: int = Field(ge=0)
    projection_compared_target_count: int = Field(ge=0)
    semantic_class_agreement: float = Field(ge=0.0, le=1.0)
    requirement_subtype_agreement: float = Field(ge=0.0, le=1.0)
    inclusion_agreement: float = Field(ge=0.0, le=1.0)
    role_agreement: float = Field(ge=0.0, le=1.0)
    parent_edge_agreement: float = Field(ge=0.0, le=1.0)
    decision_mode_agreement: float = Field(ge=0.0, le=1.0)
    merged_review_node_count: int = Field(ge=0)
    split_target_count: int = Field(ge=0)
    promoted_target_count: int = Field(ge=0)


class LlmRequirementReviewDelta(_StrictFrozenModel):
    prediction_id: str
    revision: int = Field(ge=0)
    operations: tuple[LlmRequirementReviewOperation, ...]
    metrics: LlmRequirementReviewMetrics


class LlmRequirementReviewSession(_StrictFrozenModel):
    schema_version: Literal["llm-requirement-review-session/v4"] = (
        LLM_REQUIREMENT_REVIEW_SESSION_SCHEMA_VERSION
    )
    session_id: str = Field(pattern=r"^llm-review-[0-9a-f]{32}$")
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    prediction: LlmRequirementModelPrediction
    reviewed_result: LlmRequirementReviewedResult
    delta: LlmRequirementReviewDelta
    created_at_utc: datetime
    updated_at_utc: datetime

    @model_validator(mode="after")
    def validate_session(self) -> LlmRequirementReviewSession:
        if not self.project_id.strip() or not self.document_id.strip():
            raise ValueError("review session bindings must not be blank")
        if self.created_at_utc.tzinfo is None or self.updated_at_utc.tzinfo is None:
            raise ValueError("review session timestamps must be timezone-aware")
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("review session update time cannot precede creation")
        prediction_id = self.prediction.prediction_id
        if self.reviewed_result.prediction_id != prediction_id:
            raise ValueError("reviewed result is not bound to the session prediction")
        if self.delta.prediction_id != prediction_id:
            raise ValueError("review delta is not bound to the session prediction")
        if self.delta.revision != self.reviewed_result.revision:
            raise ValueError("review delta revision must match the reviewed result")
        return self


class LlmRequirementReviewReferenceCandidate(_StrictFrozenModel):
    """Text-free receipt for one review revision, never an independent Gold claim."""

    schema_version: Literal["llm-requirement-review-reference-candidate/v1"] = (
        LLM_REQUIREMENT_REVIEW_REFERENCE_CANDIDATE_SCHEMA_VERSION
    )
    reference_candidate_id: str = Field(pattern=r"^llm-review-reference-[0-9a-f]{32}$")
    status: LlmRequirementReviewReferenceCandidateStatus
    session_id: str = Field(pattern=r"^llm-review-[0-9a-f]{32}$")
    prediction_id: str = Field(pattern=r"^llm-prediction-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=0)
    prediction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    delta_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_unit_count: int = Field(ge=1)
    review_node_count: int = Field(ge=1)
    review_operation_count: int = Field(ge=0)
    same_owner_multi_unit_group_count: int = Field(ge=0)
    edited_target_count: int = Field(ge=0)
    split_target_count: int = Field(ge=0)
    merged_review_node_count: int = Field(ge=0)
    reviewer_label_present: bool
    exact_partition_validated: Literal[True] = True
    independent_review_verified: Literal[False] = False
    human_gold: Literal[False] = False
    serving_activation_eligible: Literal[False] = False
    recorded_at_utc: datetime | None = None
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_reference_candidate(
        self,
    ) -> LlmRequirementReviewReferenceCandidate:
        if not self.reason_codes:
            raise ValueError("review reference candidates require reason codes")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("review reference candidate reason codes must be unique")
        if any(not _MACHINE_KEY_PATTERN.fullmatch(item) for item in self.reason_codes):
            raise ValueError(
                "review reference candidate reason codes must be machine keys"
            )
        if (
            self.status
            is LlmRequirementReviewReferenceCandidateStatus.SELF_ATTESTED_REVIEW
        ):
            if not self.reviewer_label_present:
                raise ValueError("a self-attested review requires a reviewer label")
            if self.recorded_at_utc is None or self.recorded_at_utc.tzinfo is None:
                raise ValueError(
                    "a self-attested review requires a timezone-aware record time"
                )
        elif self.recorded_at_utc is not None:
            raise ValueError("an uncaptured reference cannot define a record time")
        return self


def stable_llm_requirement_prediction_id(
    *,
    source_ir_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    scope_locator_sha256: str,
    provider_id: str,
    provider_version: str,
    model_id: str,
    classification_prompt_version: str,
    assembly_prompt_version: str,
    semantic_unit_plan_ids: tuple[str, ...],
    semantic_unit_ids: tuple[str, ...],
    provider_run_ids: tuple[str, ...],
    nodes: tuple[LlmRequirementPredictionNode, ...],
) -> str:
    payload = {
        "source_ir_id": source_ir_id,
        "source_sha256": source_sha256,
        "structure_snapshot_id": structure_snapshot_id,
        "scope_locator_sha256": scope_locator_sha256,
        "provider_id": provider_id,
        "provider_version": provider_version,
        "model_id": model_id,
        "classification_prompt_version": classification_prompt_version,
        "assembly_prompt_version": assembly_prompt_version,
        "semantic_unit_plan_ids": semantic_unit_plan_ids,
        "semantic_unit_ids": semantic_unit_ids,
        "provider_run_ids": provider_run_ids,
        "nodes": [item.model_dump(mode="json") for item in nodes],
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"llm-prediction-{digest[:32]}"


def source_binding_sha256(
    *,
    source_ir_id: str,
    semantic_unit_id: str,
    source_semantic_unit_ids: tuple[str, ...],
    owner_structure_node_id: str,
    source_owner_structure_node_ids: tuple[str, ...],
    source_refs: tuple[StructureSourceRef, ...],
) -> str:
    payload = {
        "source_ir_id": source_ir_id,
        "semantic_unit_id": semantic_unit_id,
        "source_semantic_unit_ids": source_semantic_unit_ids,
        "owner_structure_node_id": owner_structure_node_id,
        "source_owner_structure_node_ids": source_owner_structure_node_ids,
        "source_refs": [item.model_dump(mode="json") for item in source_refs],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_semantic_shape(
    semantic_class: SemanticClass,
    requirement_subtype: RequirementSubtype | None,
) -> None:
    if semantic_class is SemanticClass.REQUIREMENT:
        if requirement_subtype is None:
            raise ValueError("reviewed requirements require a subtype")
    elif requirement_subtype is not None:
        raise ValueError("only reviewed requirements may carry a subtype")


def _validate_projection_shape(
    *,
    semantic_class: SemanticClass,
    include_in_view: bool,
    role: RequirementProjectionRole | None,
    parent_id: str | None,
    decision_mode: RequirementDecisionMode,
) -> None:
    if not include_in_view:
        if role is not None or parent_id is not None:
            raise ValueError("excluded review nodes cannot define role or parent")
        if decision_mode is not RequirementDecisionMode.NONE:
            raise ValueError("excluded review nodes require decision_mode NONE")
        return
    if role is None:
        raise ValueError("included review nodes require a projection role")
    if semantic_class is SemanticClass.REQUIREMENT:
        if role not in {
            RequirementProjectionRole.PRIMARY,
            RequirementProjectionRole.CHILD,
            RequirementProjectionRole.CONTENT,
            RequirementProjectionRole.PARAMETER,
        }:
            raise ValueError("requirement semantics require a requirement role")
    elif role not in {
        RequirementProjectionRole.CONTENT,
        RequirementProjectionRole.REFERENCE,
    }:
        raise ValueError("non-requirement semantics require a context role")
    if role is RequirementProjectionRole.PRIMARY:
        if parent_id is not None:
            raise ValueError("PRIMARY review nodes cannot define a parent")
    elif parent_id is None:
        raise ValueError("non-PRIMARY review nodes require a parent")
    if role in {RequirementProjectionRole.CONTENT, RequirementProjectionRole.REFERENCE}:
        if decision_mode is not RequirementDecisionMode.NONE:
            raise ValueError("CONTENT and REFERENCE review nodes require NONE")
    elif decision_mode is RequirementDecisionMode.NONE:
        raise ValueError("decision requirement roles require DIRECT or ROLLUP")


__all__ = [
    "LLM_REQUIREMENT_PREDICTION_SCHEMA_VERSION",
    "LLM_REQUIREMENT_REVIEW_REFERENCE_CANDIDATE_SCHEMA_VERSION",
    "LLM_REQUIREMENT_REVIEW_SCHEMA_VERSION",
    "LLM_REQUIREMENT_REVIEW_SESSION_SCHEMA_VERSION",
    "LlmRequirementModelPrediction",
    "LlmRequirementPredictionNode",
    "LlmRequirementReviewDelta",
    "LlmRequirementReviewMetrics",
    "LlmRequirementReviewOperation",
    "LlmRequirementReviewOperationType",
    "LlmRequirementReviewReferenceCandidate",
    "LlmRequirementReviewReferenceCandidateStatus",
    "LlmRequirementReviewStatus",
    "LlmRequirementReviewSession",
    "LlmRequirementReviewedResult",
    "LlmReviewSourceSpan",
    "LlmReviewedRequirementNode",
    "source_binding_sha256",
    "stable_llm_requirement_prediction_id",
]
