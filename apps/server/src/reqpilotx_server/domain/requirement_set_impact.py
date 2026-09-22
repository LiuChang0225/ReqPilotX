from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REQUIREMENT_SET_IMPACT_RECEIPT_SCHEMA_VERSION = (
    "requirement-set-impact-receipt/v1"
)
REQUIREMENT_SET_IMPACT_NODE_SCHEMA_VERSION = "requirement-set-impact-node/v1"
REQUIREMENT_SET_IMPACT_INPUT_NODE_SCHEMA_VERSION = (
    "requirement-set-impact-input-node/v1"
)
REQUIREMENT_SET_IMPACT_ALGORITHM_VERSION = "requirement-set-impact-comparator/v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID_RE = re.compile(r"^requirement-set-impact-receipt-[0-9a-f]{32}$")


class RequirementSetImpactOutcome(str, Enum):
    RETAINED = "RETAINED"
    STALE = "STALE"
    NEW = "NEW"
    REMOVED = "REMOVED"
    NO_PRIOR_RESULT = "NO_PRIOR_RESULT"


class RequirementSetImpactReason(str, Enum):
    NODE_ADDED = "NODE_ADDED"
    NODE_REMOVED = "NODE_REMOVED"
    INTRINSIC_CHANGED = "INTRINSIC_CHANGED"
    ANALYSIS_INPUT_CHANGED = "ANALYSIS_INPUT_CHANGED"
    UNPROVEN_EQUIVALENCE = "UNPROVEN_EQUIVALENCE"
    NO_PRIOR_RESULT = "NO_PRIOR_RESULT"
    FULL_EQUIVALENCE = "FULL_EQUIVALENCE"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_json_object(value: dict[str, Any]) -> dict[str, Any]:
    try:
        _canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("impact result metadata must be canonical JSON") from exc
    return value


class RequirementSetImpactInputNodeV1(_FrozenModel):
    """One already-normalized input record consumed by the pure comparator.

    Hash construction deliberately lives outside this contract.  A missing hash
    is representable so the comparator can fail closed instead of rejecting
    readable historical data before it can emit an audit result.
    """

    schema_version: Literal["requirement-set-impact-input-node/v1"] = (
        REQUIREMENT_SET_IMPACT_INPUT_NODE_SCHEMA_VERSION
    )
    node_id: str = Field(min_length=1, max_length=260)
    requirement_id: str | None = Field(default=None, min_length=1, max_length=260)
    has_current_result: bool = False
    intrinsic_sha256: str | None = None
    analysis_input_sha256: str | None = None
    result_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requirement_id", mode="before")
    @classmethod
    def normalize_requirement_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("intrinsic_sha256", "analysis_input_sha256", mode="before")
    @classmethod
    def normalize_hash(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("intrinsic_sha256", "analysis_input_sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("impact input hashes must be lowercase SHA-256")
        return value

    @field_validator("result_metadata")
    @classmethod
    def validate_result_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_object(value)

    @model_validator(mode="after")
    def validate_result_binding(self) -> RequirementSetImpactInputNodeV1:
        if self.has_current_result and self.requirement_id is None:
            raise ValueError("a current result requires a requirement identity")
        if not self.has_current_result and self.result_metadata:
            raise ValueError("result metadata requires a current result")
        return self


class RequirementSetImpactNodeV1(_FrozenModel):
    schema_version: Literal["requirement-set-impact-node/v1"] = (
        REQUIREMENT_SET_IMPACT_NODE_SCHEMA_VERSION
    )
    node_id: str = Field(min_length=1, max_length=260)
    old_requirement_id: str | None = Field(default=None, min_length=1, max_length=260)
    new_requirement_id: str | None = Field(default=None, min_length=1, max_length=260)
    old_intrinsic_sha256: str | None = None
    new_intrinsic_sha256: str | None = None
    old_analysis_input_sha256: str | None = None
    new_analysis_input_sha256: str | None = None
    outcome: RequirementSetImpactOutcome
    reasons: tuple[RequirementSetImpactReason, ...]
    intrinsic_changed: bool
    source_result_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "old_intrinsic_sha256",
        "new_intrinsic_sha256",
        "old_analysis_input_sha256",
        "new_analysis_input_sha256",
    )
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("impact node hashes must be lowercase SHA-256")
        return value

    @field_validator("source_result_metadata")
    @classmethod
    def validate_result_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_object(value)

    @model_validator(mode="after")
    def validate_node_result(self) -> RequirementSetImpactNodeV1:
        if not self.reasons or len(self.reasons) != len(set(self.reasons)):
            raise ValueError("impact node reasons must be non-empty and unique")
        if self.outcome is RequirementSetImpactOutcome.NEW:
            if self.old_requirement_id is not None or self.new_requirement_id is None:
                raise ValueError("a new impact node requires only a new requirement")
            if (
                self.old_intrinsic_sha256 is not None
                or self.old_analysis_input_sha256 is not None
                or self.reasons != (RequirementSetImpactReason.NODE_ADDED,)
            ):
                raise ValueError("a new impact node must use the canonical reason")
        elif self.outcome is RequirementSetImpactOutcome.REMOVED:
            if self.old_requirement_id is None or self.new_requirement_id is not None:
                raise ValueError("a removed impact node requires only an old requirement")
            if (
                self.new_intrinsic_sha256 is not None
                or self.new_analysis_input_sha256 is not None
                or self.reasons != (RequirementSetImpactReason.NODE_REMOVED,)
            ):
                raise ValueError("a removed impact node must use the canonical reason")
        elif self.old_requirement_id is None or self.new_requirement_id is None:
            raise ValueError("matched impact nodes require old and new requirements")
        if self.source_result_metadata and self.outcome not in {
            RequirementSetImpactOutcome.RETAINED,
            RequirementSetImpactOutcome.STALE,
            RequirementSetImpactOutcome.REMOVED,
        }:
            raise ValueError("only a prior result may carry source result metadata")
        if self.outcome in {
            RequirementSetImpactOutcome.NEW,
            RequirementSetImpactOutcome.REMOVED,
        } and not self.intrinsic_changed:
            raise ValueError("added and removed nodes are intrinsically changed")
        if self.outcome not in {
            RequirementSetImpactOutcome.NEW,
            RequirementSetImpactOutcome.REMOVED,
        }:
            intrinsic_proven = bool(
                self.old_intrinsic_sha256 and self.new_intrinsic_sha256
            )
            analysis_proven = bool(
                self.old_analysis_input_sha256
                and self.new_analysis_input_sha256
            )
            expected_intrinsic_changed = (
                not intrinsic_proven
                or self.old_intrinsic_sha256 != self.new_intrinsic_sha256
            )
            expected_reasons: list[RequirementSetImpactReason] = []
            if (
                intrinsic_proven
                and self.old_intrinsic_sha256 != self.new_intrinsic_sha256
            ):
                expected_reasons.append(RequirementSetImpactReason.INTRINSIC_CHANGED)
            if (
                analysis_proven
                and self.old_analysis_input_sha256
                != self.new_analysis_input_sha256
            ):
                expected_reasons.append(
                    RequirementSetImpactReason.ANALYSIS_INPUT_CHANGED
                )
            if not intrinsic_proven or not analysis_proven:
                expected_reasons.append(
                    RequirementSetImpactReason.UNPROVEN_EQUIVALENCE
                )
            if self.intrinsic_changed != expected_intrinsic_changed:
                raise ValueError("impact node intrinsic change flag is inconsistent")
            if self.outcome is RequirementSetImpactOutcome.RETAINED:
                expected_reasons = [RequirementSetImpactReason.FULL_EQUIVALENCE]
            elif self.outcome is RequirementSetImpactOutcome.NO_PRIOR_RESULT:
                expected_reasons.append(RequirementSetImpactReason.NO_PRIOR_RESULT)
                if self.source_result_metadata:
                    raise ValueError("a node without a prior result has no metadata")
            elif not expected_reasons:
                raise ValueError("a stale result requires a changed or unproven input")
            if self.reasons != tuple(expected_reasons):
                raise ValueError("impact node reasons do not match its hashes")
        return self


class RequirementSetImpactReceiptV1(_FrozenModel):
    schema_version: Literal["requirement-set-impact-receipt/v1"] = (
        REQUIREMENT_SET_IMPACT_RECEIPT_SCHEMA_VERSION
    )
    algorithm_version: str = Field(min_length=1, max_length=160)
    receipt_id: str = Field(min_length=1, max_length=96)
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    old_requirement_set_id: str = Field(min_length=1, max_length=200)
    new_requirement_set_id: str = Field(min_length=1, max_length=200)
    old_tree_sha256: str
    new_tree_sha256: str
    matrix_state_schema_version: Literal[
        "requirement-matrix-state/v1"
    ]
    old_matrix_state_sha256: str
    new_matrix_state_sha256: str
    node_results: tuple[RequirementSetImpactNodeV1, ...]
    retained_result_node_ids: tuple[str, ...]
    stale_result_node_ids: tuple[str, ...]
    changed_node_ids: tuple[str, ...]
    new_node_ids: tuple[str, ...]
    removed_node_ids: tuple[str, ...]
    reason_counts: dict[str, int]
    payload_sha256: str

    @field_validator("receipt_id")
    @classmethod
    def validate_receipt_id(cls, value: str) -> str:
        if not _RECEIPT_ID_RE.fullmatch(value):
            raise ValueError("impact receipt identity is invalid")
        return value

    @field_validator(
        "old_tree_sha256",
        "new_tree_sha256",
        "old_matrix_state_sha256",
        "new_matrix_state_sha256",
        "payload_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("impact receipt hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> RequirementSetImpactReceiptV1:
        if self.old_requirement_set_id == self.new_requirement_set_id:
            raise ValueError("impact receipt requires distinct Requirement Sets")
        node_ids = tuple(item.node_id for item in self.node_results)
        if node_ids != tuple(sorted(node_ids)) or len(node_ids) != len(set(node_ids)):
            raise ValueError("impact receipt nodes must be unique and sorted")
        expected_retained = tuple(
            item.node_id
            for item in self.node_results
            if item.outcome is RequirementSetImpactOutcome.RETAINED
        )
        expected_stale = tuple(
            item.node_id
            for item in self.node_results
            if item.outcome is RequirementSetImpactOutcome.STALE
        )
        expected_changed = tuple(
            item.node_id for item in self.node_results if item.intrinsic_changed
        )
        expected_new = tuple(
            item.node_id
            for item in self.node_results
            if item.outcome is RequirementSetImpactOutcome.NEW
        )
        expected_removed = tuple(
            item.node_id
            for item in self.node_results
            if item.outcome is RequirementSetImpactOutcome.REMOVED
        )
        if (
            self.retained_result_node_ids != expected_retained
            or self.stale_result_node_ids != expected_stale
            or self.changed_node_ids != expected_changed
            or self.new_node_ids != expected_new
            or self.removed_node_ids != expected_removed
        ):
            raise ValueError("impact receipt summaries do not match node results")
        expected_reason_counts: dict[str, int] = {}
        for item in self.node_results:
            for reason in item.reasons:
                expected_reason_counts[reason.value] = (
                    expected_reason_counts.get(reason.value, 0) + 1
                )
        expected_reason_counts = dict(sorted(expected_reason_counts.items()))
        if self.reason_counts != expected_reason_counts:
            raise ValueError("impact receipt reason counts do not match node results")
        if self.receipt_id != stable_requirement_set_impact_receipt_id(self):
            raise ValueError("impact receipt identity is not canonical")
        if self.payload_sha256 != canonical_requirement_set_impact_receipt_sha256(
            self
        ):
            raise ValueError("impact receipt hash does not match its contents")
        return self


def _receipt_core_payload(receipt: RequirementSetImpactReceiptV1) -> dict[str, Any]:
    return receipt.model_dump(
        mode="json",
        exclude={"receipt_id", "payload_sha256"},
    )


def stable_requirement_set_impact_receipt_id(
    receipt: RequirementSetImpactReceiptV1,
) -> str:
    digest = hashlib.sha256(
        _canonical_json_bytes(_receipt_core_payload(receipt))
    ).hexdigest()
    return f"requirement-set-impact-receipt-{digest[:32]}"


def canonical_requirement_set_impact_receipt_sha256(
    receipt: RequirementSetImpactReceiptV1,
) -> str:
    payload = receipt.model_dump(mode="json", exclude={"payload_sha256"})
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


__all__ = [
    "REQUIREMENT_SET_IMPACT_ALGORITHM_VERSION",
    "REQUIREMENT_SET_IMPACT_INPUT_NODE_SCHEMA_VERSION",
    "REQUIREMENT_SET_IMPACT_NODE_SCHEMA_VERSION",
    "REQUIREMENT_SET_IMPACT_RECEIPT_SCHEMA_VERSION",
    "RequirementSetImpactInputNodeV1",
    "RequirementSetImpactNodeV1",
    "RequirementSetImpactOutcome",
    "RequirementSetImpactReason",
    "RequirementSetImpactReceiptV1",
    "canonical_requirement_set_impact_receipt_sha256",
    "stable_requirement_set_impact_receipt_id",
]
