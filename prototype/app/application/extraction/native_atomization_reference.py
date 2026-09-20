from __future__ import annotations

import hashlib
import json
from enum import Enum
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.application.extraction.codex_extraction_parity import (
    strict_structured_output_schema,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationRange,
)


NATIVE_ATOMIZATION_REFERENCE_SCHEMA_VERSION = "native-atomization-reference/v1"
NATIVE_ATOMIZATION_REFERENCE_RECEIPT_SCHEMA_VERSION = (
    "native-atomization-reference-receipt/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeAtomizationReferenceStatus(str, Enum):
    OBSERVED = "OBSERVED"
    PARTIAL = "PARTIAL"


class NativeAtomizationContextDisposition(str, Enum):
    CONTEXT = "CONTEXT"
    HEADING = "HEADING"
    METHOD = "METHOD"
    PRESENTATION = "PRESENTATION"
    REFERENCE = "REFERENCE"
    UNRESOLVED = "UNRESOLVED"


class NativeAtomizationContextRangeV1(_StrictFrozenModel):
    source_range: RequirementAtomizationRange
    disposition: NativeAtomizationContextDisposition
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("context reason codes must be unique and sorted")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", item) for item in value):
            raise ValueError("context reason codes must be snake_case identifiers")
        return value


class NativeAtomizationReferenceV1(_StrictFrozenModel):
    schema_version: Literal["native-atomization-reference/v1"] = (
        NATIVE_ATOMIZATION_REFERENCE_SCHEMA_VERSION
    )
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_node_id: str = Field(min_length=1, max_length=200)
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
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostic_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("atomization diagnostics must be unique and sorted")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", item) for item in value):
            raise ValueError("atomization diagnostics must be snake_case identifiers")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> NativeAtomizationReferenceV1:
        has_unresolved = any(
            item.disposition is NativeAtomizationContextDisposition.UNRESOLVED
            for item in self.context_ranges
        )
        expected = (
            NativeAtomizationReferenceStatus.PARTIAL
            if has_unresolved
            else NativeAtomizationReferenceStatus.OBSERVED
        )
        if self.status is not expected:
            raise ValueError("atomization status does not match unresolved ranges")
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError(
                "native atomization references cannot be Gold or serving gates"
            )
        return self


class NativeAtomizationReferenceReceiptV1(_StrictFrozenModel):
    schema_version: Literal["native-atomization-reference-receipt/v1"] = (
        NATIVE_ATOMIZATION_REFERENCE_RECEIPT_SCHEMA_VERSION
    )
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_node_id: str = Field(min_length=1, max_length=200)
    status: NativeAtomizationReferenceStatus
    obligation_count: StrictInt = Field(ge=1)
    context_range_count: StrictInt = Field(ge=0)
    grounding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False


def native_atomization_reference_output_schema() -> dict[str, object]:
    schema = strict_structured_output_schema(NativeAtomizationReferenceV1)
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise RuntimeError("native atomization output schema is missing definitions")
    obligation = definitions["RequirementAtomizationObligation"]
    context = definitions["NativeAtomizationContextRangeV1"]
    obligation["properties"]["split_basis"] = {
        "type": "string",
        "enum": ["single_or_descriptive", "independent_obligation"],
    }
    reason_code_schema = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,119}$"}
    obligation["properties"]["reason_codes"]["items"] = reason_code_schema
    context["properties"]["reason_codes"]["items"] = reason_code_schema
    schema["properties"]["diagnostic_codes"]["items"] = reason_code_schema
    return schema


def validate_native_atomization_reference(
    request: RequirementAtomizationProviderRequest,
    reference: NativeAtomizationReferenceV1,
) -> NativeAtomizationReferenceReceiptV1:
    if reference.lineage_sha256 != request.lineage_sha256:
        raise ValueError("native atomization reference lineage is stale")
    if reference.review_node_id != request.review_node_id:
        raise ValueError("native atomization reference review node is stale")

    coverage = [
        bytearray(len(segment.source_text)) for segment in request.source_segments
    ]
    grounding_records: list[dict[str, object]] = []
    decision_records: list[dict[str, object]] = []

    for obligation_index, obligation in enumerate(reference.obligations):
        for source_range in obligation.source_ranges:
            _mark_range(
                request,
                coverage,
                source_range,
                owner=f"obligation:{obligation_index}",
            )
        decision_records.append(
            {
                "source_ranges": [
                    item.model_dump(mode="json") for item in obligation.source_ranges
                ],
                "atomic_statement_sha256": hashlib.sha256(
                    obligation.atomic_statement.encode("utf-8")
                ).hexdigest(),
                "split_basis": obligation.split_basis,
                "reason_codes": obligation.reason_codes,
            }
        )

    for context_index, context in enumerate(reference.context_ranges):
        _mark_range(
            request,
            coverage,
            context.source_range,
            owner=f"context:{context_index}",
        )
        decision_records.append(
            {
                "source_range": context.source_range.model_dump(mode="json"),
                "disposition": context.disposition.value,
                "reason_codes": context.reason_codes,
            }
        )

    for segment, segment_coverage in zip(
        request.source_segments,
        coverage,
        strict=True,
    ):
        missing_offsets = [
            offset
            for offset, (character, covered) in enumerate(
                zip(segment.source_text, segment_coverage, strict=True)
            )
            if not character.isspace() and not covered
        ]
        if missing_offsets:
            raise ValueError(
                "native atomization reference does not exact-cover source text"
            )
        grounding_records.append(
            {
                "segment_index": segment.segment_index,
                "semantic_unit_id": segment.semantic_unit_id,
                "owner_structure_node_id": segment.owner_structure_node_id,
                "source_start_offset": segment.source_start_offset,
                "source_end_offset": segment.source_end_offset,
                "source_text_sha256": hashlib.sha256(
                    segment.source_text.encode("utf-8")
                ).hexdigest(),
            }
        )

    return NativeAtomizationReferenceReceiptV1(
        lineage_sha256=reference.lineage_sha256,
        review_node_id=reference.review_node_id,
        status=reference.status,
        obligation_count=len(reference.obligations),
        context_range_count=len(reference.context_ranges),
        grounding_sha256=_canonical_json_sha256(grounding_records),
        decision_sha256=_canonical_json_sha256(decision_records),
    )


def _mark_range(
    request: RequirementAtomizationProviderRequest,
    coverage: list[bytearray],
    source_range: RequirementAtomizationRange,
    *,
    owner: str,
) -> None:
    if source_range.segment_index >= len(request.source_segments):
        raise ValueError("native atomization range references an unknown segment")
    segment = request.source_segments[source_range.segment_index]
    if source_range.end_offset > len(segment.source_text):
        raise ValueError("native atomization range exceeds its source segment")
    segment_coverage = coverage[source_range.segment_index]
    for offset in range(source_range.start_offset, source_range.end_offset):
        if segment_coverage[offset]:
            raise ValueError(f"native atomization ranges overlap at {owner}")
        segment_coverage[offset] = 1


def _canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "NATIVE_ATOMIZATION_REFERENCE_RECEIPT_SCHEMA_VERSION",
    "NATIVE_ATOMIZATION_REFERENCE_SCHEMA_VERSION",
    "NativeAtomizationContextDisposition",
    "NativeAtomizationContextRangeV1",
    "NativeAtomizationReferenceReceiptV1",
    "NativeAtomizationReferenceStatus",
    "NativeAtomizationReferenceV1",
    "native_atomization_reference_output_schema",
    "validate_native_atomization_reference",
]
