from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx.domain.extraction.semantics import SemanticGroundingSpan
from reqpilotx.domain.extraction.structure import StructureSourceRef


SEMANTIC_UNIT_PLAN_SCHEMA_VERSION = "semantic-unit-plan/v1"
SEMANTIC_UNIT_PARTITION_CANDIDATE_SCHEMA_VERSION = (
    "semantic-unit-partition-candidate/v1"
)
_STRUCTURE_NODE_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")


class _StrictFrozenModel(BaseModel):
    """Immutable, provider-neutral value object for semantic unitization."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SemanticUnitKind(str, Enum):
    """Supported semantic decision-unit shapes.

    The first migration deliberately exposes only one full-span unit per
    Structure owner. Later unitizers may add source-bound split kinds without
    changing SourceIR or StructureTree.
    """

    OWNER_FULL_SPAN = "OWNER_FULL_SPAN"
    CONTINUOUS_EXPLICIT_LIST_ITEM = "CONTINUOUS_EXPLICIT_LIST_ITEM"


class SemanticUnitPartitionDisposition(str, Enum):
    """Outcome of an inactive, provider-neutral unitization proposal."""

    CANDIDATE = "CANDIDATE"
    NO_OP = "NO_OP"
    BLOCKED = "BLOCKED"


class SemanticUnitMarkerFamily(str, Enum):
    """Marker families proven safe enough for a partition proposal."""

    LATIN_ALPHA = "LATIN_ALPHA"


class SemanticUnitPartitionReason(str, Enum):
    """Stable, text-free reason codes for candidate unitization."""

    SAFE_CONTINUOUS_EXPLICIT_LIST = "safe_continuous_explicit_list"
    OWNER_NOT_CONTENT = "owner_not_content"
    OWNER_NOT_SINGLE_NATIVE_TEXT = "owner_not_single_native_text"
    TABLE_BOUND_OWNER = "table_bound_owner"
    CROSS_SOURCE_REF_OWNER = "cross_source_ref_owner"
    TEXT_RESOLUTION_FAILED = "text_resolution_failed"
    OWNER_TEXT_VIEW_MISMATCH = "owner_text_view_mismatch"
    NO_HARD_LINE_MARKER = "no_hard_line_marker"
    INSUFFICIENT_CONSECUTIVE_ITEMS = "insufficient_consecutive_items"
    UNSUPPORTED_MARKER_FAMILY = "unsupported_marker_family"
    MIXED_MARKER_FAMILY = "mixed_marker_family"
    EMPTY_ITEM_BODY = "empty_item_body"
    NON_CONSECUTIVE_MARKERS = "non_consecutive_markers"
    NON_LIST_LINE = "non_list_line"
    FORMULA_LIKE_ITEM = "formula_like_item"


class SemanticUnit(_StrictFrozenModel):
    """One source-bound semantic decision unit owned by a Structure node."""

    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    parent_owner_structure_node_id: str | None = Field(
        default=None,
        pattern=r"^structure-node-[0-9a-f]{32}$",
    )
    parent_semantic_unit_id: str | None = Field(
        default=None,
        pattern=r"^semantic-unit-[0-9a-f]{32}$",
    )
    source_order: int = Field(ge=0)
    unit_order: int = Field(ge=0)
    owner_unit_index: int = Field(ge=0)
    unit_kind: SemanticUnitKind
    source_spans: tuple[SemanticGroundingSpan, ...]

    @model_validator(mode="after")
    def validate_unit_shape(self) -> SemanticUnit:
        if self.parent_owner_structure_node_id == self.owner_structure_node_id:
            raise ValueError("a semantic unit owner cannot be its own parent")
        if self.parent_semantic_unit_id == self.semantic_unit_id:
            raise ValueError("a semantic unit cannot be its own parent")
        if (
            self.unit_kind is SemanticUnitKind.OWNER_FULL_SPAN
            and self.owner_unit_index != 0
        ):
            raise ValueError("full-span semantic units require owner_unit_index zero")
        if not self.source_spans:
            raise ValueError("semantic units require source-bound spans")
        if self.unit_kind is SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM and (
            len(self.source_spans) != 1
            or self.source_spans[0].end_offset <= self.source_spans[0].start_offset
        ):
            raise ValueError(
                "continuous explicit-list units require one non-empty source span"
            )

        span_ids = tuple(item.grounding_span_id for item in self.source_spans)
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("semantic unit source spans must be unique")
        previous_end_by_source: dict[StructureSourceRef, int] = {}
        closed_sources: set[StructureSourceRef] = set()
        active_source: StructureSourceRef | None = None
        for span in self.source_spans:
            if active_source is not None and span.source_ref != active_source:
                closed_sources.add(active_source)
            if span.source_ref in closed_sources:
                raise ValueError(
                    "semantic unit spans for one source must be contiguous"
                )
            previous_end = previous_end_by_source.get(span.source_ref)
            if previous_end is not None and span.start_offset < previous_end:
                raise ValueError("semantic unit source spans must not overlap")
            previous_end_by_source[span.source_ref] = span.end_offset
            active_source = span.source_ref
        return self


class SemanticUnitPartitionCandidate(_StrictFrozenModel):
    """Source-bound proposal produced before any production unit is changed.

    A ``CANDIDATE`` contains a complete, ordered partition of the current
    full-span owner unit. ``NO_OP`` and ``BLOCKED`` never carry proposed spans,
    so callers cannot accidentally treat an unsupported shape as a split.
    """

    schema_version: Literal["semantic-unit-partition-candidate/v1"] = (
        SEMANTIC_UNIT_PARTITION_CANDIDATE_SCHEMA_VERSION
    )
    candidate_unitizer_version: str = Field(min_length=1, max_length=160)
    owner_semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    parent_owner_structure_node_id: str | None = Field(
        default=None,
        pattern=r"^structure-node-[0-9a-f]{32}$",
    )
    parent_semantic_unit_id: str | None = Field(
        default=None,
        pattern=r"^semantic-unit-[0-9a-f]{32}$",
    )
    disposition: SemanticUnitPartitionDisposition
    reason_code: SemanticUnitPartitionReason
    marker_family: SemanticUnitMarkerFamily | None = None
    owner_source_spans: tuple[SemanticGroundingSpan, ...]
    proposed_unit_spans: tuple[SemanticGroundingSpan, ...] = ()

    @field_validator("candidate_unitizer_version")
    @classmethod
    def reject_blank_candidate_unitizer_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate unitizer version must not be blank")
        return value

    @model_validator(mode="after")
    def validate_candidate_partition(self) -> SemanticUnitPartitionCandidate:
        if self.parent_owner_structure_node_id == self.owner_structure_node_id:
            raise ValueError("a partition owner cannot be its own parent")
        if self.parent_semantic_unit_id == self.owner_semantic_unit_id:
            raise ValueError("a partition owner unit cannot be its own parent")
        if not self.owner_source_spans:
            raise ValueError("partition candidates require owner source spans")
        owner_span_ids = tuple(
            item.grounding_span_id for item in self.owner_source_spans
        )
        if len(owner_span_ids) != len(set(owner_span_ids)):
            raise ValueError("partition owner source spans must be unique")

        if self.disposition is not SemanticUnitPartitionDisposition.CANDIDATE:
            if self.reason_code is (
                SemanticUnitPartitionReason.SAFE_CONTINUOUS_EXPLICIT_LIST
            ):
                raise ValueError("only candidate partitions may use the safe reason")
            if self.marker_family is not None or self.proposed_unit_spans:
                raise ValueError(
                    "no-op and blocked partitions cannot expose proposed units"
                )
            return self

        if self.reason_code is not (
            SemanticUnitPartitionReason.SAFE_CONTINUOUS_EXPLICIT_LIST
        ):
            raise ValueError("candidate partitions require the safe-list reason")
        if self.marker_family is None:
            raise ValueError("candidate partitions require one marker family")
        if len(self.owner_source_spans) != 1:
            raise ValueError("candidate partitions require one source-bound owner span")
        if len(self.proposed_unit_spans) < 3:
            raise ValueError("candidate partitions require at least three units")

        owner_span = self.owner_source_spans[0]
        next_offset = owner_span.start_offset
        proposed_ids: set[str] = set()
        for span in self.proposed_unit_spans:
            if span.grounding_span_id in proposed_ids:
                raise ValueError("candidate partition spans must be unique")
            proposed_ids.add(span.grounding_span_id)
            if span.source_ref != owner_span.source_ref:
                raise ValueError(
                    "candidate partition spans must remain on the owner source"
                )
            if span.start_offset != next_offset or span.end_offset <= span.start_offset:
                raise ValueError(
                    "candidate partition spans must form a non-empty exact cover"
                )
            next_offset = span.end_offset
        if next_offset != owner_span.end_offset:
            raise ValueError(
                "candidate partition spans must cover the complete owner interval"
            )
        return self


class SemanticUnitPlan(_StrictFrozenModel):
    """Complete ordered unitization of one declared Structure scope."""

    schema_version: Literal["semantic-unit-plan/v1"] = SEMANTIC_UNIT_PLAN_SCHEMA_VERSION
    semantic_unit_plan_id: str = Field(pattern=r"^semantic-unit-plan-[0-9a-f]{32}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    unitizer_version: str = Field(min_length=1, max_length=160)
    scope_owner_structure_node_ids: tuple[str, ...]
    units: tuple[SemanticUnit, ...]

    @field_validator(
        "source_ir_id",
        "structure_snapshot_id",
        "unitizer_version",
    )
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic unit plan identity values must not be blank")
        return value

    @field_validator("scope_owner_structure_node_ids")
    @classmethod
    def validate_scope_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("semantic unit plans require a non-empty owner scope")
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("semantic unit plan owners require stable Structure IDs")
        if len(value) != len(set(value)):
            raise ValueError("semantic unit plan owner scope must be unique")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> SemanticUnitPlan:
        if not self.units:
            raise ValueError("semantic unit plans require at least one unit")
        unit_ids = tuple(item.semantic_unit_id for item in self.units)
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("semantic unit IDs must be unique")

        units_by_owner: dict[str, list[SemanticUnit]] = {}
        ordered_owner_ids: list[str] = []
        previous_owner_id: str | None = None
        closed_owner_ids: set[str] = set()
        for unit in self.units:
            owner_id = unit.owner_structure_node_id
            if previous_owner_id is not None and owner_id != previous_owner_id:
                closed_owner_ids.add(previous_owner_id)
            if owner_id in closed_owner_ids:
                raise ValueError(
                    "semantic units for one Structure owner must remain contiguous"
                )
            if owner_id not in units_by_owner:
                ordered_owner_ids.append(owner_id)
                units_by_owner[owner_id] = []
            units_by_owner[owner_id].append(unit)
            previous_owner_id = owner_id
        if tuple(ordered_owner_ids) != self.scope_owner_structure_node_ids:
            raise ValueError(
                "semantic units must cover every owner in declared scope order"
            )
        if tuple(item.unit_order for item in self.units) != tuple(
            range(len(self.units))
        ):
            raise ValueError(
                "semantic unit_order must be contiguous and source ordered"
            )

        owner_source_orders: list[int] = []
        for owner_id in self.scope_owner_structure_node_ids:
            owner_units = units_by_owner[owner_id]
            source_orders = {item.source_order for item in owner_units}
            if len(source_orders) != 1:
                raise ValueError(
                    "semantic units for one owner must share Structure source_order"
                )
            owner_source_orders.append(next(iter(source_orders)))
            if tuple(item.owner_unit_index for item in owner_units) != tuple(
                range(len(owner_units))
            ):
                raise ValueError(
                    "semantic owner_unit_index must be contiguous and owner ordered"
                )

            kinds = {item.unit_kind for item in owner_units}
            if len(owner_units) == 1:
                if kinds != {SemanticUnitKind.OWNER_FULL_SPAN}:
                    raise ValueError(
                        "single-unit owners require one full-span semantic unit"
                    )
            elif len(owner_units) < 3 or kinds != {
                SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM
            }:
                raise ValueError(
                    "multi-unit owners require at least three explicit-list items"
                )
            else:
                first_span = owner_units[0].source_spans[0]
                next_offset = first_span.start_offset
                for unit in owner_units:
                    span = unit.source_spans[0]
                    if (
                        span.source_ref != first_span.source_ref
                        or span.start_offset != next_offset
                        or span.end_offset <= span.start_offset
                    ):
                        raise ValueError(
                            "multi-unit owner spans must form one adjacent source partition"
                        )
                    next_offset = span.end_offset

            parent_owner_ids = {
                item.parent_owner_structure_node_id for item in owner_units
            }
            parent_unit_ids = {item.parent_semantic_unit_id for item in owner_units}
            if len(parent_owner_ids) != 1 or len(parent_unit_ids) != 1:
                raise ValueError(
                    "semantic units for one owner must share one parent grounding"
                )

        source_orders = tuple(owner_source_orders)
        if source_orders != tuple(sorted(source_orders)) or len(source_orders) != len(
            set(source_orders)
        ):
            raise ValueError(
                "semantic unit owners require unique ascending source order"
            )

        units_by_id = {item.semantic_unit_id: item for item in self.units}
        for unit in self.units:
            expected_unit_id = stable_semantic_unit_id(
                source_ir_id=self.source_ir_id,
                structure_snapshot_id=self.structure_snapshot_id,
                owner_structure_node_id=unit.owner_structure_node_id,
                unitizer_version=self.unitizer_version,
                ordered_source_spans=unit.source_spans,
            )
            if unit.semantic_unit_id != expected_unit_id:
                raise ValueError(
                    "semantic_unit_id must be derived from owner and source spans"
                )
            expected_parent_units = units_by_owner.get(
                unit.parent_owner_structure_node_id or ""
            )
            if expected_parent_units is not None and len(expected_parent_units) != 1:
                raise ValueError(
                    "a multi-unit owner cannot be an in-scope semantic parent"
                )
            expected_parent_unit_id = None
            if expected_parent_units is not None:
                expected_parent_unit_id = expected_parent_units[0].semantic_unit_id
            if unit.parent_semantic_unit_id != expected_parent_unit_id:
                raise ValueError(
                    "semantic unit parent must reference the in-scope owner parent"
                )
            if unit.parent_semantic_unit_id is not None:
                parent = units_by_id[unit.parent_semantic_unit_id]
                if parent.unit_order >= unit.unit_order:
                    raise ValueError(
                        "semantic unit parents must precede their children"
                    )

        expected_plan_id = stable_semantic_unit_plan_id(
            source_ir_id=self.source_ir_id,
            structure_snapshot_id=self.structure_snapshot_id,
            unitizer_version=self.unitizer_version,
            scope_owner_structure_node_ids=self.scope_owner_structure_node_ids,
            semantic_unit_ids=unit_ids,
        )
        if self.semantic_unit_plan_id != expected_plan_id:
            raise ValueError(
                "semantic_unit_plan_id must be derived from its ordered unit scope"
            )
        return self

    @property
    def semantic_unit_ids(self) -> tuple[str, ...]:
        return tuple(item.semantic_unit_id for item in self.units)


def stable_semantic_unit_id(
    *,
    source_ir_id: str,
    structure_snapshot_id: str,
    owner_structure_node_id: str,
    unitizer_version: str,
    ordered_source_spans: tuple[SemanticGroundingSpan, ...],
) -> str:
    _require_non_blank(source_ir_id, "source_ir_id")
    _require_non_blank(structure_snapshot_id, "structure_snapshot_id")
    _require_non_blank(owner_structure_node_id, "owner_structure_node_id")
    _require_non_blank(unitizer_version, "unitizer_version")
    if not _STRUCTURE_NODE_ID_PATTERN.fullmatch(owner_structure_node_id):
        raise ValueError("owner_structure_node_id must be a stable Structure ID")
    if not isinstance(ordered_source_spans, tuple) or not ordered_source_spans:
        raise ValueError("semantic units require ordered source spans")
    if any(
        not isinstance(item, SemanticGroundingSpan) for item in ordered_source_spans
    ):
        raise TypeError(
            "ordered_source_spans must contain SemanticGroundingSpan values"
        )
    return _stable_id(
        "semantic-unit",
        source_ir_id,
        structure_snapshot_id,
        owner_structure_node_id,
        unitizer_version,
        [item.model_dump(mode="json") for item in ordered_source_spans],
    )


def stable_semantic_unit_plan_id(
    *,
    source_ir_id: str,
    structure_snapshot_id: str,
    unitizer_version: str,
    scope_owner_structure_node_ids: tuple[str, ...],
    semantic_unit_ids: tuple[str, ...],
) -> str:
    _require_non_blank(source_ir_id, "source_ir_id")
    _require_non_blank(structure_snapshot_id, "structure_snapshot_id")
    _require_non_blank(unitizer_version, "unitizer_version")
    if not scope_owner_structure_node_ids:
        raise ValueError("semantic unit plans require a non-empty owner scope")
    if len(scope_owner_structure_node_ids) != len(set(scope_owner_structure_node_ids)):
        raise ValueError("semantic unit plan owner scope must be unique")
    if len(semantic_unit_ids) < len(scope_owner_structure_node_ids):
        raise ValueError("semantic unit IDs must cover every owner at least once")
    if len(semantic_unit_ids) != len(set(semantic_unit_ids)):
        raise ValueError("semantic unit IDs must be unique")
    return _stable_id(
        "semantic-unit-plan",
        source_ir_id,
        structure_snapshot_id,
        unitizer_version,
        list(scope_owner_structure_node_ids),
        list(semantic_unit_ids),
    )


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


__all__ = [
    "SEMANTIC_UNIT_PLAN_SCHEMA_VERSION",
    "SEMANTIC_UNIT_PARTITION_CANDIDATE_SCHEMA_VERSION",
    "SemanticUnit",
    "SemanticUnitKind",
    "SemanticUnitMarkerFamily",
    "SemanticUnitPartitionCandidate",
    "SemanticUnitPartitionDisposition",
    "SemanticUnitPartitionReason",
    "SemanticUnitPlan",
    "stable_semantic_unit_id",
    "stable_semantic_unit_plan_id",
]
