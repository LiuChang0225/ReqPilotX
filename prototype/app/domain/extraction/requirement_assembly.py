from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.extraction.requirement_view import RequirementProjectionRole


REQUIREMENT_ASSEMBLY_SCHEMA_VERSION = "requirement-assembly/v3"
_ASSEMBLY_ID_PATTERN = re.compile(r"^requirement-assembly-[0-9a-f]{32}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RequirementAssemblyStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    UNRESOLVED = "UNRESOLVED"


class RequirementAssemblyItem(_StrictFrozenModel):
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    source_semantic_unit_ids: tuple[str, ...]
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    source_owner_structure_node_ids: tuple[str, ...]
    unit_order: int = Field(ge=0)
    source_order: int = Field(ge=0)
    include_in_view: bool
    role: RequirementProjectionRole | None = None
    parent_semantic_unit_id: str | None = Field(
        default=None,
        pattern=r"^semantic-unit-[0-9a-f]{32}$",
    )
    parent_owner_structure_node_id: str | None = Field(
        default=None,
        pattern=r"^structure-node-[0-9a-f]{32}$",
    )
    status: RequirementAssemblyStatus
    reason_codes: tuple[str, ...] = ()

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("assembly reason codes must use machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("assembly reason codes must be unique and sorted")
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
            raise ValueError("assembly source units require stable identities")
        if len(value) != len(set(value)):
            raise ValueError("assembly source units must be unique")
        return value

    @field_validator("source_owner_structure_node_ids")
    @classmethod
    def validate_source_owner_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not re.fullmatch(r"structure-node-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("assembly source owners require stable identities")
        return value

    @model_validator(mode="after")
    def validate_item(self) -> RequirementAssemblyItem:
        if self.source_semantic_unit_ids[0] != self.semantic_unit_id:
            raise ValueError("assembly primary unit must lead source unit order")
        if self.source_owner_structure_node_ids[0] != self.owner_structure_node_id:
            raise ValueError("assembly primary owner must lead source owner order")
        if len(self.source_semantic_unit_ids) != len(
            self.source_owner_structure_node_ids
        ):
            raise ValueError("assembly source units must retain owner grounding")
        if self.status is RequirementAssemblyStatus.UNRESOLVED:
            if (
                self.include_in_view
                or self.role is not None
                or self.parent_semantic_unit_id is not None
                or self.parent_owner_structure_node_id is not None
            ):
                raise ValueError("unresolved assembly items cannot enter the view")
            if not self.reason_codes:
                raise ValueError("unresolved assembly items require a reason code")
            return self
        if not self.include_in_view:
            if (
                self.role is not None
                or self.parent_semantic_unit_id is not None
                or self.parent_owner_structure_node_id is not None
            ):
                raise ValueError(
                    "excluded assembly items cannot define a role or parent"
                )
            return self
        if self.role is None:
            raise ValueError("included assembly items require a projection role")
        if self.role is RequirementProjectionRole.PRIMARY:
            if (
                self.parent_semantic_unit_id is not None
                or self.parent_owner_structure_node_id is not None
            ):
                raise ValueError("PRIMARY assembly items cannot have a parent")
        elif (
            self.parent_semantic_unit_id is None
            or self.parent_owner_structure_node_id is None
        ):
            raise ValueError("non-PRIMARY assembly items require a parent")
        if self.parent_semantic_unit_id == self.semantic_unit_id:
            raise ValueError("an assembly item cannot be its own parent")
        return self

    @property
    def target_structure_node_id(self) -> str:
        return self.owner_structure_node_id

    @property
    def parent_structure_node_id(self) -> str | None:
        return self.parent_owner_structure_node_id


class RequirementAssemblySnapshot(_StrictFrozenModel):
    schema_version: Literal["requirement-assembly/v3"] = (
        REQUIREMENT_ASSEMBLY_SCHEMA_VERSION
    )
    requirement_assembly_id: str = Field(min_length=1, max_length=160)
    source_ir_id: str = Field(min_length=1, max_length=160)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    semantic_snapshot_id: str = Field(min_length=1, max_length=160)
    semantic_unit_plan_id: str = Field(pattern=r"^semantic-unit-plan-[0-9a-f]{32}$")
    assembler_id: str = Field(min_length=1, max_length=160)
    assembler_version: str = Field(min_length=1, max_length=160)
    provider_run_id: str = Field(min_length=1, max_length=200)
    scope_semantic_unit_ids: tuple[str, ...]
    scope_owner_structure_node_ids: tuple[str, ...]
    items: tuple[RequirementAssemblyItem, ...]

    @field_validator(
        "source_ir_id",
        "structure_snapshot_id",
        "semantic_snapshot_id",
        "assembler_id",
        "assembler_version",
        "provider_run_id",
    )
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assembly identity values must not be blank")
        return value

    @field_validator("scope_semantic_unit_ids")
    @classmethod
    def validate_scope_semantic_unit_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not re.fullmatch(r"semantic-unit-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("assembly scope requires stable semantic-unit IDs")
        if len(value) != len(set(value)):
            raise ValueError("assembly semantic-unit scope must be unique")
        return value

    @field_validator("scope_owner_structure_node_ids")
    @classmethod
    def validate_scope_owner_structure_node_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not re.fullmatch(r"structure-node-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("assembly scope requires stable Structure owners")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> RequirementAssemblySnapshot:
        primary_unit_ids = tuple(item.semantic_unit_id for item in self.items)
        if len(primary_unit_ids) != len(set(primary_unit_ids)):
            raise ValueError("assembly scope must contain unique targets")
        flattened_unit_ids = tuple(
            unit_id for item in self.items for unit_id in item.source_semantic_unit_ids
        )
        if len(flattened_unit_ids) != len(set(flattened_unit_ids)) or set(
            flattened_unit_ids
        ) != set(self.scope_semantic_unit_ids):
            raise ValueError(
                "assembly items must exactly partition the semantic-unit scope"
            )
        flattened_owner_ids = tuple(
            owner_id
            for item in self.items
            for owner_id in item.source_owner_structure_node_ids
        )
        if len(self.scope_semantic_unit_ids) != len(
            self.scope_owner_structure_node_ids
        ):
            raise ValueError(
                "assembly scope requires one Structure owner per semantic unit"
            )
        closed_owner_ids: set[str] = set()
        active_owner_id: str | None = None
        for owner_id in self.scope_owner_structure_node_ids:
            if active_owner_id is not None and owner_id != active_owner_id:
                closed_owner_ids.add(active_owner_id)
            if owner_id in closed_owner_ids:
                raise ValueError(
                    "assembly units for one Structure owner must remain contiguous"
                )
            active_owner_id = owner_id
        if len(flattened_owner_ids) != len(self.scope_owner_structure_node_ids):
            raise ValueError(
                "assembly items must retain one owner grounding per source unit"
            )
        unit_order_by_id = {
            unit_id: unit_order
            for unit_order, unit_id in enumerate(self.scope_semantic_unit_ids)
        }
        owner_by_unit_id = dict(
            zip(
                self.scope_semantic_unit_ids,
                self.scope_owner_structure_node_ids,
                strict=True,
            )
        )
        for item in self.items:
            lineage_orders = tuple(
                unit_order_by_id[unit_id] for unit_id in item.source_semantic_unit_ids
            )
            if lineage_orders != tuple(sorted(lineage_orders)):
                raise ValueError(
                    "assembly source-unit lineage must preserve source order"
                )
            if any(
                owner_by_unit_id[unit_id] != owner_id
                for unit_id, owner_id in zip(
                    item.source_semantic_unit_ids,
                    item.source_owner_structure_node_ids,
                    strict=True,
                )
            ):
                raise ValueError(
                    "assembly source units must retain exact Structure ownership"
                )
        unit_orders = tuple(item.unit_order for item in self.items)
        if (
            unit_orders != tuple(sorted(unit_orders))
            or len(unit_orders) != len(set(unit_orders))
            or any(
                item.unit_order != unit_order_by_id[item.semantic_unit_id]
                for item in self.items
            )
        ):
            raise ValueError("assembly primary unit_order must be ascending")
        orders = tuple(item.source_order for item in self.items)
        if orders != tuple(sorted(orders)):
            raise ValueError("assembly items require ascending source_order values")
        expected = stable_requirement_assembly_id(
            source_ir_id=self.source_ir_id,
            structure_snapshot_id=self.structure_snapshot_id,
            semantic_snapshot_id=self.semantic_snapshot_id,
            semantic_unit_plan_id=self.semantic_unit_plan_id,
            assembler_id=self.assembler_id,
            assembler_version=self.assembler_version,
            provider_run_id=self.provider_run_id,
            items=self.items,
        )
        if self.requirement_assembly_id != expected:
            raise ValueError(
                "requirement_assembly_id does not match its immutable content"
            )
        return self

    @property
    def scope_structure_node_ids(self) -> tuple[str, ...]:
        """Transient alias for the historical primary assembly targets."""

        return tuple(item.owner_structure_node_id for item in self.items)


def stable_requirement_assembly_id(
    *,
    source_ir_id: str,
    structure_snapshot_id: str,
    semantic_snapshot_id: str,
    semantic_unit_plan_id: str,
    assembler_id: str,
    assembler_version: str,
    provider_run_id: str,
    items: tuple[RequirementAssemblyItem, ...],
) -> str:
    values = (
        source_ir_id,
        structure_snapshot_id,
        semantic_snapshot_id,
        semantic_unit_plan_id,
        assembler_id,
        assembler_version,
        provider_run_id,
    )
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ValueError("assembly identity values must not be blank")
    payload = json.dumps(
        (*values, [item.model_dump(mode="json") for item in items]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"requirement-assembly-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


__all__ = [
    "REQUIREMENT_ASSEMBLY_SCHEMA_VERSION",
    "RequirementAssemblyItem",
    "RequirementAssemblySnapshot",
    "RequirementAssemblyStatus",
    "stable_requirement_assembly_id",
]
