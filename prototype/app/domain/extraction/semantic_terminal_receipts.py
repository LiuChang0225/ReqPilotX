from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.extraction.semantic_units import SemanticUnit


SEMANTIC_UNIT_TERMINAL_RECEIPT_SCHEMA_VERSION = "semantic-unit-terminal-receipt/v1"
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SemanticUnitTerminalStatus(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


class SemanticUnitTerminalReceipt(_StrictFrozenModel):
    """One terminal, source-bound classification receipt per semantic unit."""

    schema_version: Literal["semantic-unit-terminal-receipt/v1"] = (
        SEMANTIC_UNIT_TERMINAL_RECEIPT_SCHEMA_VERSION
    )
    terminal_receipt_id: str = Field(
        pattern=r"^semantic-terminal-receipt-[0-9a-f]{32}$"
    )
    semantic_unit_plan_id: str = Field(pattern=r"^semantic-unit-plan-[0-9a-f]{32}$")
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    unit_order: int = Field(ge=0)
    source_coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planning_runtime_version: str = Field(min_length=1, max_length=160)
    status: SemanticUnitTerminalStatus
    provider_run_id: str = Field(pattern=r"^semantic-run-[0-9a-f]{32}$")
    reason_codes: tuple[str, ...]

    @field_validator("planning_runtime_version")
    @classmethod
    def reject_blank_runtime_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("planning runtime version must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("terminal receipts require machine-readable reason codes")
        if value != tuple(sorted(set(value))):
            raise ValueError("terminal receipt reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> SemanticUnitTerminalReceipt:
        expected = stable_semantic_unit_terminal_receipt_id(
            semantic_unit_plan_id=self.semantic_unit_plan_id,
            semantic_unit_id=self.semantic_unit_id,
            owner_structure_node_id=self.owner_structure_node_id,
            unit_order=self.unit_order,
            source_coverage_sha256=self.source_coverage_sha256,
            planning_runtime_version=self.planning_runtime_version,
            status=self.status,
            provider_run_id=self.provider_run_id,
            reason_codes=self.reason_codes,
        )
        if self.terminal_receipt_id != expected:
            raise ValueError("terminal receipt ID does not match immutable content")
        return self


def semantic_unit_source_coverage_sha256(unit: SemanticUnit) -> str:
    if not isinstance(unit, SemanticUnit):
        raise TypeError("unit must be a SemanticUnit")
    payload = json.dumps(
        [item.model_dump(mode="json") for item in unit.source_spans],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_semantic_unit_terminal_receipt_id(
    *,
    semantic_unit_plan_id: str,
    semantic_unit_id: str,
    owner_structure_node_id: str,
    unit_order: int,
    source_coverage_sha256: str,
    planning_runtime_version: str,
    status: SemanticUnitTerminalStatus,
    provider_run_id: str,
    reason_codes: tuple[str, ...],
) -> str:
    if not isinstance(status, SemanticUnitTerminalStatus):
        raise TypeError("status must be a SemanticUnitTerminalStatus")
    payload = json.dumps(
        (
            semantic_unit_plan_id,
            semantic_unit_id,
            owner_structure_node_id,
            unit_order,
            source_coverage_sha256,
            planning_runtime_version,
            status.value,
            provider_run_id,
            reason_codes,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "semantic-terminal-receipt-"
        f"{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"
    )


__all__ = [
    "SEMANTIC_UNIT_TERMINAL_RECEIPT_SCHEMA_VERSION",
    "SemanticUnitTerminalReceipt",
    "SemanticUnitTerminalStatus",
    "semantic_unit_source_coverage_sha256",
    "stable_semantic_unit_terminal_receipt_id",
]
