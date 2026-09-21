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

from reqpilotx_server.application.extraction.codex_extraction_parity import (
    strict_structured_output_schema,
)
from reqpilotx_server.ports.requirement_atomization_provider import RequirementAtomizationRange


NATIVE_PARENT_BOUNDARY_REFERENCE_SCHEMA_VERSION = "native-parent-boundary-reference/v2"
NATIVE_PARENT_BOUNDARY_RECEIPT_SCHEMA_VERSION = (
    "native-parent-boundary-reference-receipt/v2"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeParentBoundaryStatus(str, Enum):
    OBSERVED = "OBSERVED"
    PARTIAL = "PARTIAL"


class NativeProvisionDisposition(str, Enum):
    REQUIREMENT = "REQUIREMENT"
    INFORMATION = "INFORMATION"
    UNRESOLVED = "UNRESOLVED"


class NativeParentContextDisposition(str, Enum):
    HEADING = "HEADING"
    METADATA = "METADATA"
    PRESENTATION = "PRESENTATION"
    REFERENCE = "REFERENCE"
    UNRESOLVED = "UNRESOLVED"


class NativeParentBoundarySourceSegmentV1(_StrictFrozenModel):
    segment_index: StrictInt = Field(ge=0)
    page_number: StrictInt = Field(ge=1)
    evidence_channel: Literal["NATIVE_TEXT", "VISUAL_TEXT", "TABLE_TEXT"]
    source_ref: str = Field(min_length=1, max_length=240)
    source_text: str = Field(min_length=1, max_length=500_000)
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_text_hash(self) -> NativeParentBoundarySourceSegmentV1:
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != (
            self.source_text_sha256
        ):
            raise ValueError("parent-boundary source text hash mismatch")
        return self


class NativeParentBoundaryRequestV1(_StrictFrozenModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(min_length=1, max_length=200)
    source_segments: tuple[NativeParentBoundarySourceSegmentV1, ...] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_segments(self) -> NativeParentBoundaryRequestV1:
        if tuple(item.segment_index for item in self.source_segments) != tuple(
            range(len(self.source_segments))
        ):
            raise ValueError("parent-boundary segments must use contiguous indexes")
        pages = tuple(item.page_number for item in self.source_segments)
        if pages != tuple(sorted(pages)):
            raise ValueError("parent-boundary segments must use ascending pages")
        return self


class NativeProvisionBlockV1(_StrictFrozenModel):
    provision_id: str = Field(pattern=r"^native-provision-[0-9a-f]{32}$")
    explicit_label: str | None = Field(default=None, max_length=120)
    ancestor_labels: tuple[str, ...] = ()
    source_order: StrictInt = Field(ge=0)
    disposition: NativeProvisionDisposition
    body_ranges: tuple[RequirementAtomizationRange, ...] = Field(
        min_length=1,
        max_length=128,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("ancestor_labels")
    @classmethod
    def validate_ancestor_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("ancestor labels must be unique and non-empty")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", item) for item in value
        ):
            raise ValueError("parent-boundary reason codes must be snake_case")
        return value

    @model_validator(mode="after")
    def validate_ancestry(self) -> NativeProvisionBlockV1:
        if (
            self.explicit_label is not None
            and "." in self.explicit_label
            and self.explicit_label in self.ancestor_labels
        ):
            raise ValueError(
                "a provision label cannot also appear in its ancestor labels"
            )
        return self


class NativeParentContextRangeV1(_StrictFrozenModel):
    source_range: RequirementAtomizationRange
    disposition: NativeParentContextDisposition
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", item) for item in value
        ):
            raise ValueError("parent context reason codes must be sorted snake_case")
        return value


class NativeParentBoundaryReferenceV1(_StrictFrozenModel):
    schema_version: Literal["native-parent-boundary-reference/v2"] = (
        NATIVE_PARENT_BOUNDARY_REFERENCE_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(min_length=1, max_length=200)
    status: NativeParentBoundaryStatus
    provisions: tuple[NativeProvisionBlockV1, ...] = Field(min_length=1, max_length=256)
    context_ranges: tuple[NativeParentContextRangeV1, ...] = Field(
        default=(),
        max_length=512,
    )
    diagnostic_codes: tuple[str, ...] = ()
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @model_validator(mode="after")
    def validate_reference(self) -> NativeParentBoundaryReferenceV1:
        orders = tuple(item.source_order for item in self.provisions)
        if orders != tuple(sorted(set(orders))):
            raise ValueError("native provisions require unique ascending source order")
        ids = tuple(item.provision_id for item in self.provisions)
        if len(ids) != len(set(ids)):
            raise ValueError("native provision IDs must be unique")
        nested_markers_by_parent: dict[str, int] = {}
        for provision in self.provisions:
            if (
                provision.ancestor_labels
                and provision.explicit_label is not None
                and re.fullmatch(r"(?:\d+|[A-Za-z])", provision.explicit_label)
            ):
                parent_label = provision.ancestor_labels[-1]
                nested_markers_by_parent[parent_label] = (
                    nested_markers_by_parent.get(parent_label, 0) + 1
                )
        if any(count > 1 for count in nested_markers_by_parent.values()):
            raise ValueError(
                "nested list items must remain inside one complete parent provision"
            )
        has_unresolved = any(
            item.disposition is NativeParentContextDisposition.UNRESOLVED
            for item in self.context_ranges
        ) or any(
            item.disposition is NativeProvisionDisposition.UNRESOLVED
            for item in self.provisions
        )
        expected = (
            NativeParentBoundaryStatus.PARTIAL
            if has_unresolved
            else NativeParentBoundaryStatus.OBSERVED
        )
        if self.status is not expected:
            raise ValueError("parent-boundary status does not match unresolved ranges")
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError(
                "parent-boundary references cannot be Gold or serving gates"
            )
        return self


class NativeParentBoundaryReceiptV1(_StrictFrozenModel):
    schema_version: Literal["native-parent-boundary-reference-receipt/v2"] = (
        NATIVE_PARENT_BOUNDARY_RECEIPT_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(min_length=1, max_length=200)
    status: NativeParentBoundaryStatus
    provision_count: StrictInt = Field(ge=1)
    context_range_count: StrictInt = Field(ge=0)
    grounding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False


def native_parent_boundary_output_schema() -> dict[str, object]:
    schema = strict_structured_output_schema(NativeParentBoundaryReferenceV1)
    definitions = schema["$defs"]
    reason_schema = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,119}$"}
    definitions["NativeProvisionBlockV1"]["properties"]["reason_codes"]["items"] = (
        reason_schema
    )
    definitions["NativeParentContextRangeV1"]["properties"]["reason_codes"]["items"] = (
        reason_schema
    )
    schema["properties"]["diagnostic_codes"]["items"] = reason_schema
    return schema


def validate_native_parent_boundary_reference(
    request: NativeParentBoundaryRequestV1,
    reference: NativeParentBoundaryReferenceV1,
) -> NativeParentBoundaryReceiptV1:
    if reference.source_sha256 != request.source_sha256:
        raise ValueError("native parent-boundary source identity is stale")
    if reference.scope_id != request.scope_id:
        raise ValueError("native parent-boundary scope identity is stale")
    coverage = [
        bytearray(len(segment.source_text)) for segment in request.source_segments
    ]
    decisions: list[dict[str, object]] = []
    for provision in reference.provisions:
        for source_range in provision.body_ranges:
            _mark_range(request, coverage, source_range)
        decisions.append(
            {
                "provision_id": provision.provision_id,
                "explicit_label": provision.explicit_label,
                "ancestor_labels": provision.ancestor_labels,
                "source_order": provision.source_order,
                "disposition": provision.disposition.value,
                "body_ranges": [
                    item.model_dump(mode="json") for item in provision.body_ranges
                ],
                "reason_codes": provision.reason_codes,
            }
        )
    for context in reference.context_ranges:
        _mark_range(request, coverage, context.source_range)
        decisions.append(
            {
                "source_range": context.source_range.model_dump(mode="json"),
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
            raise ValueError(
                "native parent-boundary reference does not exact-cover scope"
            )
        grounding.append(
            {
                "segment_index": segment.segment_index,
                "page_number": segment.page_number,
                "evidence_channel": segment.evidence_channel,
                "source_ref": segment.source_ref,
                "source_text_sha256": segment.source_text_sha256,
            }
        )
    return NativeParentBoundaryReceiptV1(
        source_sha256=request.source_sha256,
        scope_id=request.scope_id,
        status=reference.status,
        provision_count=len(reference.provisions),
        context_range_count=len(reference.context_ranges),
        grounding_sha256=_canonical_hash(grounding),
        decision_sha256=_canonical_hash(decisions),
    )


def canonicalize_native_parent_boundary_reference(
    request: NativeParentBoundaryRequestV1,
    reference: NativeParentBoundaryReferenceV1,
) -> NativeParentBoundaryReferenceV1:
    """Replace model-proposed handles with host-derived source-range identities."""

    if (
        reference.source_sha256 != request.source_sha256
        or reference.scope_id != request.scope_id
    ):
        raise ValueError("native parent-boundary identity is stale")
    provisions = tuple(
        provision.model_copy(
            update={
                "provision_id": "native-provision-"
                + _canonical_hash(
                    {
                        "source_sha256": request.source_sha256,
                        "scope_id": request.scope_id,
                        "body_ranges": [
                            item.model_dump(mode="json")
                            for item in provision.body_ranges
                        ],
                    }
                )[:32]
            }
        )
        for provision in reference.provisions
    )
    return NativeParentBoundaryReferenceV1.model_validate(
        {
            **reference.model_dump(mode="json"),
            "provisions": [item.model_dump(mode="json") for item in provisions],
        }
    )


def _mark_range(
    request: NativeParentBoundaryRequestV1,
    coverage: list[bytearray],
    source_range: RequirementAtomizationRange,
) -> None:
    if source_range.segment_index >= len(request.source_segments):
        raise ValueError("native parent range references an unknown segment")
    segment = request.source_segments[source_range.segment_index]
    if source_range.end_offset > len(segment.source_text):
        raise ValueError("native parent range exceeds its segment")
    observed = coverage[source_range.segment_index]
    for offset in range(source_range.start_offset, source_range.end_offset):
        if observed[offset]:
            raise ValueError("native parent ranges overlap")
        observed[offset] = 1


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "NativeParentBoundaryReceiptV1",
    "NativeParentBoundaryReferenceV1",
    "NativeParentBoundaryRequestV1",
    "NativeParentBoundarySourceSegmentV1",
    "NativeParentBoundaryStatus",
    "NativeParentContextDisposition",
    "NativeParentContextRangeV1",
    "NativeProvisionDisposition",
    "NativeProvisionBlockV1",
    "canonicalize_native_parent_boundary_reference",
    "native_parent_boundary_output_schema",
    "validate_native_parent_boundary_reference",
]
