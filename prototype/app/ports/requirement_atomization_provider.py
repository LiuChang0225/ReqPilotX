from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.extraction.semantic_provider import SemanticProviderRunRecord


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RequirementAtomizationSourceSegment(_StrictFrozenModel):
    segment_index: int = Field(ge=0)
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    source_start_offset: int = Field(ge=0)
    source_end_offset: int = Field(ge=0)
    source_text: str = Field(min_length=1, max_length=500_000)

    @model_validator(mode="after")
    def validate_segment(self) -> RequirementAtomizationSourceSegment:
        if self.source_end_offset <= self.source_start_offset:
            raise ValueError("atomization source segments must be non-empty")
        if self.source_end_offset - self.source_start_offset != len(self.source_text):
            raise ValueError(
                "atomization source segment text must match its source range"
            )
        return self


class RequirementAtomizationRange(_StrictFrozenModel):
    segment_index: int = Field(ge=0)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> RequirementAtomizationRange:
        if self.end_offset <= self.start_offset:
            raise ValueError("atomization obligation ranges must be non-empty")
        return self


class RequirementAtomizationObligation(_StrictFrozenModel):
    source_ranges: tuple[RequirementAtomizationRange, ...] = Field(
        min_length=1,
        max_length=128,
    )
    atomic_statement: str = Field(min_length=1, max_length=500_000)
    split_basis: str = Field(min_length=1, max_length=120)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("atomic_statement")
    @classmethod
    def validate_atomic_statement(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("atomization obligations require an atomic statement")
        return value

    @field_validator("split_basis")
    @classmethod
    def validate_split_basis(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {
            "single_or_descriptive",
            "independent_obligation",
        }:
            raise ValueError("atomization split_basis is unsupported")
        return normalized

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("atomization reason codes must be unique and sorted")
        return value


class RequirementAtomizationContextDisposition(str, Enum):
    CONTEXT = "CONTEXT"
    HEADING = "HEADING"
    METHOD = "METHOD"
    PRESENTATION = "PRESENTATION"
    REFERENCE = "REFERENCE"
    UNRESOLVED = "UNRESOLVED"


class RequirementAtomizationContextRange(_StrictFrozenModel):
    source_range: RequirementAtomizationRange
    disposition: RequirementAtomizationContextDisposition
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError(
                "atomization context reason codes must be unique and sorted"
            )
        return value


class RequirementAtomizationTableContext(_StrictFrozenModel):
    """Compact, resolvable context for exactly one table projection record.

    The provider receives identities and resolver keys only.  Authoritative
    cells, merges, images, and other SourceIR facts remain behind the evidence
    resolver and are never copied into the atomization contract.
    """

    table_evidence_binding_id: str = Field(
        pattern=r"^table-evidence-binding-[0-9a-f]{32}$"
    )
    logical_table_id: str = Field(min_length=1, max_length=260)
    projection_id: str = Field(min_length=1, max_length=260)
    projection_revision: int = Field(ge=0)
    projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_id: str | None = Field(default=None, min_length=1, max_length=260)
    header_resolver_refs: tuple[str, ...] = ()
    shared_value_resolver_refs: tuple[str, ...] = ()
    note_resolver_refs: tuple[str, ...] = ()
    image_resolver_refs: tuple[str, ...] = ()

    @field_validator(
        "logical_table_id",
        "projection_id",
        "record_id",
    )
    @classmethod
    def reject_blank_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("table atomization identities cannot be blank")
        return value

    @field_validator(
        "header_resolver_refs",
        "shared_value_resolver_refs",
        "note_resolver_refs",
        "image_resolver_refs",
    )
    @classmethod
    def validate_resolver_refs(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("table atomization resolver refs cannot be blank")
        if len(values) != len(set(values)):
            raise ValueError("table atomization resolver refs must be unique")
        return values


class RequirementAtomizationProviderRequest(_StrictFrozenModel):
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_node_id: str = Field(min_length=1, max_length=200)
    requirement_subtype: str = Field(min_length=1, max_length=120)
    parent_context_statement: str = Field(default="", max_length=500_000)
    source_segments: tuple[RequirementAtomizationSourceSegment, ...] = Field(
        min_length=1,
        max_length=512,
    )
    table_context: RequirementAtomizationTableContext | None = None

    @model_validator(mode="after")
    def validate_segments(self) -> RequirementAtomizationProviderRequest:
        if tuple(item.segment_index for item in self.source_segments) != tuple(
            range(len(self.source_segments))
        ):
            raise ValueError("atomization source segments must use contiguous indexes")
        return self


class RequirementAtomizationProviderResult(_StrictFrozenModel):
    obligations: tuple[RequirementAtomizationObligation, ...] = Field(
        min_length=1,
        max_length=128,
    )
    context_ranges: tuple[RequirementAtomizationContextRange, ...] = Field(
        default=(),
        max_length=256,
    )
    run: SemanticProviderRunRecord

    @model_validator(mode="after")
    def validate_independent_statements(
        self,
    ) -> RequirementAtomizationProviderResult:
        if len(self.obligations) <= 1:
            return self
        normalized = tuple(
            " ".join(item.atomic_statement.split()).casefold()
            for item in self.obligations
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                "multi-obligation atomization requires independent atomic statements"
            )
        return self


class RequirementAtomizationProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@runtime_checkable
class RequirementAtomizationProviderPort(Protocol):
    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult: ...


__all__ = [
    "RequirementAtomizationObligation",
    "RequirementAtomizationContextDisposition",
    "RequirementAtomizationContextRange",
    "RequirementAtomizationProviderError",
    "RequirementAtomizationProviderPort",
    "RequirementAtomizationProviderRequest",
    "RequirementAtomizationProviderResult",
    "RequirementAtomizationRange",
    "RequirementAtomizationSourceSegment",
    "RequirementAtomizationTableContext",
]
