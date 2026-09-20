from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.extraction.native_atomization_reference import (
    NativeAtomizationContextRangeV1,
    NativeAtomizationReferenceStatus,
)
from app.domain.extraction.semantic_provider import SemanticProviderRunRecord
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationRange,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class DocumentDecompositionSourceSegmentV1(_StrictFrozenModel):
    """One immutable parent-local slice of a canonical document page segment."""

    segment_index: int = Field(ge=0)
    page_number: int = Field(ge=1)
    source_ref: str = Field(min_length=1, max_length=240)
    source_start_offset: int = Field(ge=0)
    source_end_offset: int = Field(gt=0)
    source_text: str = Field(min_length=1, max_length=500_000)
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_source_slice(self) -> DocumentDecompositionSourceSegmentV1:
        if self.source_end_offset <= self.source_start_offset:
            raise ValueError("document decomposition source range is empty")
        if self.source_end_offset - self.source_start_offset != len(self.source_text):
            raise ValueError("document decomposition source range/text mismatch")
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != (
            self.source_text_sha256
        ):
            raise ValueError("document decomposition source text hash mismatch")
        return self


class DocumentDecompositionItemRequestV1(_StrictFrozenModel):
    parent_provision_id: str = Field(pattern=r"^native-provision-[0-9a-f]{32}$")
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    explicit_label: str | None = Field(default=None, max_length=120)
    ancestor_labels: tuple[str, ...] = ()
    source_order: int = Field(ge=0)
    source_segments: tuple[DocumentDecompositionSourceSegmentV1, ...] = Field(
        min_length=1,
        max_length=512,
    )

    @field_validator("ancestor_labels")
    @classmethod
    def validate_ancestor_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("document decomposition ancestors must be unique")
        return value

    @model_validator(mode="after")
    def validate_segments(self) -> DocumentDecompositionItemRequestV1:
        if tuple(item.segment_index for item in self.source_segments) != tuple(
            range(len(self.source_segments))
        ):
            raise ValueError("document decomposition segments must be contiguous")
        expected_lineage = document_decomposition_lineage_sha256(
            parent_provision_id=self.parent_provision_id,
            explicit_label=self.explicit_label,
            ancestor_labels=self.ancestor_labels,
            source_order=self.source_order,
            source_segments=self.source_segments,
        )
        if self.lineage_sha256 != expected_lineage:
            raise ValueError("document decomposition lineage is stale")
        return self


class DocumentDecompositionRequestV1(_StrictFrozenModel):
    schema_version: Literal["agent-document-decomposition-request/v1"] = (
        "agent-document-decomposition-request/v1"
    )
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[DocumentDecompositionItemRequestV1, ...] = Field(
        min_length=1,
        max_length=24,
    )

    @model_validator(mode="after")
    def validate_items(self) -> DocumentDecompositionRequestV1:
        parent_ids = tuple(item.parent_provision_id for item in self.items)
        lineages = tuple(item.lineage_sha256 for item in self.items)
        orders = tuple(item.source_order for item in self.items)
        if (
            len(parent_ids) != len(set(parent_ids))
            or len(lineages) != len(set(lineages))
            or orders != tuple(sorted(set(orders)))
        ):
            raise ValueError(
                "document decomposition parents must be unique and ordered"
            )
        return self


class DocumentDecompositionItemReferenceV1(_StrictFrozenModel):
    parent_provision_id: str = Field(pattern=r"^native-provision-[0-9a-f]{32}$")
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: NativeAtomizationReferenceStatus
    obligations: tuple[RequirementAtomizationObligation, ...] = Field(
        min_length=1,
        max_length=128,
    )
    context_ranges: tuple[NativeAtomizationContextRangeV1, ...] = Field(
        default=(),
        max_length=256,
    )
    diagnostic_codes: tuple[str, ...] = ()

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,119}", item) is None for item in value
        ):
            raise ValueError("document decomposition diagnostics are invalid")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> DocumentDecompositionItemReferenceV1:
        has_unresolved = any(
            item.disposition.value == "UNRESOLVED" for item in self.context_ranges
        )
        expected = (
            NativeAtomizationReferenceStatus.PARTIAL
            if has_unresolved
            else NativeAtomizationReferenceStatus.OBSERVED
        )
        if self.status is not expected:
            raise ValueError("document decomposition status is inconsistent")
        return self


class DocumentDecompositionReferenceV1(_StrictFrozenModel):
    schema_version: Literal["agent-document-decomposition-reference/v1"] = (
        "agent-document-decomposition-reference/v1"
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[DocumentDecompositionItemReferenceV1, ...] = Field(
        min_length=1,
        max_length=24,
    )

    @model_validator(mode="after")
    def validate_items(self) -> DocumentDecompositionReferenceV1:
        parent_ids = tuple(item.parent_provision_id for item in self.items)
        if len(parent_ids) != len(set(parent_ids)):
            raise ValueError("document decomposition references must be unique")
        return self


class DocumentDecompositionItemReceiptV1(_StrictFrozenModel):
    schema_version: Literal["agent-document-decomposition-receipt/v1"] = (
        "agent-document-decomposition-receipt/v1"
    )
    parent_provision_id: str = Field(pattern=r"^native-provision-[0-9a-f]{32}$")
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: NativeAtomizationReferenceStatus
    obligation_count: int = Field(ge=1)
    context_range_count: int = Field(ge=0)
    grounding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DocumentDecompositionProviderResult:
    reference: DocumentDecompositionReferenceV1
    validation_receipts: tuple[DocumentDecompositionItemReceiptV1, ...]
    run: SemanticProviderRunRecord


@runtime_checkable
class DocumentDecompositionProviderPort(Protocol):
    def decompose_document_parents(
        self,
        request: DocumentDecompositionRequestV1,
    ) -> DocumentDecompositionProviderResult: ...


def validate_document_decomposition_reference(
    request: DocumentDecompositionRequestV1,
    reference: DocumentDecompositionReferenceV1,
) -> tuple[DocumentDecompositionItemReceiptV1, ...]:
    if reference.source_sha256 != request.source_sha256:
        raise ValueError("document decomposition source identity is stale")
    expected_ids = tuple(item.parent_provision_id for item in request.items)
    observed_ids = tuple(item.parent_provision_id for item in reference.items)
    if observed_ids != expected_ids:
        raise ValueError(
            "document decomposition must exactly cover parents in request order"
        )
    return tuple(
        _validate_item(request_item, reference_item)
        for request_item, reference_item in zip(
            request.items,
            reference.items,
            strict=True,
        )
    )


def _validate_item(
    request: DocumentDecompositionItemRequestV1,
    reference: DocumentDecompositionItemReferenceV1,
) -> DocumentDecompositionItemReceiptV1:
    if reference.lineage_sha256 != request.lineage_sha256:
        raise ValueError("document decomposition parent lineage is stale")
    coverage = [bytearray(len(item.source_text)) for item in request.source_segments]
    decisions: list[dict[str, object]] = []
    for obligation_index, obligation in enumerate(reference.obligations):
        for source_range in obligation.source_ranges:
            _mark_range(request, coverage, source_range)
        decisions.append(
            {
                "kind": "obligation",
                "index": obligation_index,
                "ranges": [
                    item.model_dump(mode="json") for item in obligation.source_ranges
                ],
                "statement_sha256": hashlib.sha256(
                    obligation.atomic_statement.encode("utf-8")
                ).hexdigest(),
                "split_basis": obligation.split_basis,
                "reason_codes": obligation.reason_codes,
            }
        )
    for context_index, context in enumerate(reference.context_ranges):
        _mark_range(request, coverage, context.source_range)
        decisions.append(
            {
                "kind": "context",
                "index": context_index,
                "range": context.source_range.model_dump(mode="json"),
                "disposition": context.disposition.value,
                "reason_codes": context.reason_codes,
            }
        )
    grounding: list[dict[str, object]] = []
    for segment, observed in zip(request.source_segments, coverage, strict=True):
        if any(
            not character.isspace() and not observed[offset]
            for offset, character in enumerate(segment.source_text)
        ):
            raise ValueError("document decomposition does not exact-cover parent")
        grounding.append(
            {
                "segment_index": segment.segment_index,
                "page_number": segment.page_number,
                "source_ref": segment.source_ref,
                "source_start_offset": segment.source_start_offset,
                "source_end_offset": segment.source_end_offset,
                "source_text_sha256": segment.source_text_sha256,
            }
        )
    return DocumentDecompositionItemReceiptV1(
        parent_provision_id=request.parent_provision_id,
        lineage_sha256=request.lineage_sha256,
        status=reference.status,
        obligation_count=len(reference.obligations),
        context_range_count=len(reference.context_ranges),
        grounding_sha256=_canonical_hash(grounding),
        decision_sha256=_canonical_hash(decisions),
    )


def _mark_range(
    request: DocumentDecompositionItemRequestV1,
    coverage: list[bytearray],
    source_range: RequirementAtomizationRange,
) -> None:
    if source_range.segment_index >= len(request.source_segments):
        raise ValueError("document decomposition references an unknown segment")
    segment = request.source_segments[source_range.segment_index]
    if source_range.end_offset > len(segment.source_text):
        raise ValueError("document decomposition range exceeds its segment")
    observed = coverage[source_range.segment_index]
    for offset in range(source_range.start_offset, source_range.end_offset):
        if observed[offset]:
            raise ValueError("document decomposition ranges overlap")
        observed[offset] = 1


def document_decomposition_lineage_sha256(
    *,
    parent_provision_id: str,
    explicit_label: str | None,
    ancestor_labels: tuple[str, ...],
    source_order: int,
    source_segments: tuple[DocumentDecompositionSourceSegmentV1, ...],
) -> str:
    return _canonical_hash(
        {
            "parent_provision_id": parent_provision_id,
            "explicit_label": explicit_label,
            "ancestor_labels": ancestor_labels,
            "source_order": source_order,
            "source_segments": [
                item.model_dump(mode="json") for item in source_segments
            ],
        }
    )


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DocumentDecompositionItemReceiptV1",
    "DocumentDecompositionItemReferenceV1",
    "DocumentDecompositionItemRequestV1",
    "DocumentDecompositionProviderPort",
    "DocumentDecompositionProviderResult",
    "DocumentDecompositionReferenceV1",
    "DocumentDecompositionRequestV1",
    "DocumentDecompositionSourceSegmentV1",
    "document_decomposition_lineage_sha256",
    "validate_document_decomposition_reference",
]
