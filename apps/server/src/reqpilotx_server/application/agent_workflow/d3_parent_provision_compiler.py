"""Host compiler for the deliberately narrow D3 model wire contract.

The model owns only parent boundaries and professional classification.  Source
text, hashes, logical-object ownership, stable identities, lineage, capture
status, and attestation inputs remain deterministic Host responsibilities.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from reqpilotx_server.application.agent_workflow.d3_parent_provision_attestation import (
    D3_PARENT_PROVISION_DECISION_CANDIDATE_SCHEMA_VERSION,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    D3ValidatedD2ArtifactIdentityV1,
    PARENT_PROVISION_ENTRY_EVIDENCE_REF_LIMIT,
    ParentProvisionEntryV3,
    ParentProvisionSourceRangeV3,
    ParentProvisionStatus,
    ProvisionContentRole,
    RequirementKind,
    RequirementTechnicality,
    Requirementness,
    canonical_json_sha256,
    d3_source_scope_sha256,
    parent_provision_decision_candidate_sha256,
    parent_provision_decision_sha256,
    stable_parent_provision_id,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


D3_PARENT_BOUNDARY_CLASSIFICATION_CANDIDATE_SCHEMA_VERSION = (
    "d3-parent-boundary-classification-candidate/v2"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D3CandidateSourceSpanV2(_StrictFrozenModel):
    """Compact inclusive segment-boundary span selected by the model."""

    start_segment_ordinal: StrictInt = Field(ge=0)
    start_offset: StrictInt = Field(ge=0)
    end_segment_ordinal: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_non_empty_span(self) -> D3CandidateSourceSpanV2:
        if (
            self.start_segment_ordinal == self.end_segment_ordinal
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("single-segment D3 candidate span must be non-empty")
        return self


class D3ParentBoundaryClassificationEntryV2(_StrictFrozenModel):
    """The complete semantic authority granted to the D3 model."""

    source_spans: tuple[D3CandidateSourceSpanV2, ...] = Field(
        min_length=1,
        max_length=256,
    )
    explicit_label: str | None = Field(default=None, max_length=160)
    ancestor_labels: tuple[str, ...] = Field(default=(), max_length=64)
    requirementness: Requirementness
    requirement_kind: RequirementKind | None = None
    technicality: RequirementTechnicality | None = None
    content_role: ProvisionContentRole
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 160 for item in value):
            raise ValueError("D3 candidate reasons must be non-empty")
        return value

    @field_validator("explicit_label")
    @classmethod
    def reject_blank_label(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("D3 candidate explicit label must not be blank")
        return value

    @field_validator("ancestor_labels")
    @classmethod
    def validate_ancestor_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError(
                "D3 candidate ancestor labels must be unique and non-empty"
            )
        return value

    @model_validator(mode="after")
    def validate_classification_axes(
        self,
    ) -> D3ParentBoundaryClassificationEntryV2:
        if not math.isfinite(self.confidence):
            raise ValueError("D3 candidate confidence must be finite")
        if (
            self.requirementness is Requirementness.REQUIREMENT
            and self.requirement_kind is None
        ):
            raise ValueError("requirements must declare a requirement kind")
        if (
            self.requirementness is Requirementness.NON_REQUIREMENT
            and self.requirement_kind is not None
        ):
            raise ValueError("non-requirements cannot claim a requirement kind")
        if (
            self.requirementness is Requirementness.UNRESOLVED
            and self.requirement_kind not in {None, RequirementKind.UNRESOLVED}
        ):
            raise ValueError("unresolved requirementness cannot claim a resolved kind")
        if (
            self.requirementness is not Requirementness.REQUIREMENT
            and self.technicality not in {None, RequirementTechnicality.UNRESOLVED}
        ):
            raise ValueError(
                "non-requirement or unresolved entries cannot claim resolved "
                "technicality"
            )
        return self


class D3ParentBoundaryClassificationCandidateV2(_StrictFrozenModel):
    """Minimal structured output schema presented to the D3 model turn."""

    schema_version: Literal["d3-parent-boundary-classification-candidate/v2"] = (
        D3_PARENT_BOUNDARY_CLASSIFICATION_CANDIDATE_SCHEMA_VERSION
    )
    entries: tuple[D3ParentBoundaryClassificationEntryV2, ...] = Field(
        default=(),
        max_length=65536,
    )
    unresolved_logical_object_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8192,
    )

    @field_validator("unresolved_logical_object_ids")
    @classmethod
    def validate_unresolved_objects(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("D3 unresolved object IDs must be unique and non-empty")
        return value

    @property
    def candidate_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class D3HostCompiledParentProvisionDecisionV1(_StrictFrozenModel):
    """Attestation-ready decision payload compiled entirely by the Host."""

    schema_version: Literal["d3-parent-provision-decision-candidate/v1"] = (
        D3_PARENT_PROVISION_DECISION_CANDIDATE_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    predecessor_d2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_d2_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ParentProvisionStatus
    declared_logical_object_ids: tuple[str, ...] = Field(max_length=8192)
    unresolved_logical_object_ids: tuple[str, ...] = Field(max_length=8192)
    entries: tuple[ParentProvisionEntryV3, ...] = Field(max_length=65536)
    diagnostic_codes: tuple[str, ...] = Field(max_length=256)

    @model_validator(mode="after")
    def validate_compiled_shape(self) -> D3HostCompiledParentProvisionDecisionV1:
        if tuple(entry.source_order for entry in self.entries) != tuple(
            range(len(self.entries))
        ):
            raise ValueError("Host-compiled D3 entries require contiguous source order")
        if self.status is ParentProvisionStatus.COMPLETE and (
            self.unresolved_logical_object_ids or self.diagnostic_codes
        ):
            raise ValueError("COMPLETE Host-compiled D3 decision cannot be unresolved")
        if self.status is ParentProvisionStatus.PARTIAL and not (
            self.unresolved_logical_object_ids or self.diagnostic_codes
        ):
            raise ValueError("PARTIAL Host-compiled D3 decision needs diagnostics")
        return self

    @property
    def decision_sha256(self) -> str:
        return parent_provision_decision_sha256(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            kit_identity=self.kit_identity,
            predecessor_d2_artifact_id=self.predecessor_d2_artifact_id,
            predecessor_d2_artifact_sha256=self.predecessor_d2_artifact_sha256,
            status=self.status,
            declared_logical_object_ids=self.declared_logical_object_ids,
            unresolved_logical_object_ids=self.unresolved_logical_object_ids,
            entries=self.entries,
            diagnostic_codes=self.diagnostic_codes,
        )

    @property
    def decision_candidate_sha256(self) -> str:
        return parent_provision_decision_candidate_sha256(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            kit_identity=self.kit_identity,
            predecessor_d2_artifact_id=self.predecessor_d2_artifact_id,
            predecessor_d2_artifact_sha256=self.predecessor_d2_artifact_sha256,
            status=self.status,
            declared_logical_object_ids=self.declared_logical_object_ids,
            unresolved_logical_object_ids=self.unresolved_logical_object_ids,
            entries=self.entries,
            diagnostic_codes=self.diagnostic_codes,
        )


def compile_d3_parent_provision_decision(
    *,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
    candidate: D3ParentBoundaryClassificationCandidateV2,
) -> D3HostCompiledParentProvisionDecisionV1:
    """Compile a narrow model candidate into the canonical pre-attestation wire.

    All source- and lineage-bearing fields are resolved from validated Host
    inputs.  Unknown, out-of-bounds, overlapping, reordered, or incomplete
    parent ranges fail closed before policy attestation can run.
    """

    d2_identity = D3ValidatedD2ArtifactIdentityV1.model_validate(
        d2_identity.model_dump(mode="python")
    )
    source_segments = tuple(
        D3ExactSourceSegmentV1.model_validate(segment.model_dump(mode="python"))
        for segment in source_segments
    )
    kit_identity = AgentKitIdentityV1.model_validate(
        kit_identity.model_dump(mode="python")
    )
    candidate = D3ParentBoundaryClassificationCandidateV2.model_validate(
        candidate.model_dump(mode="python")
    )
    if kit_identity != d2_identity.kit_identity:
        raise ValueError("D3 compiler Kit identity drifted from validated D2")
    _validate_source_scope(d2_identity, source_segments)

    segment_order = {
        segment_id: index
        for index, segment_id in enumerate(d2_identity.source_segment_ids)
    }
    object_order = {
        object_id: index
        for index, object_id in enumerate(d2_identity.logical_object_ids)
    }
    unresolved_set = set(candidate.unresolved_logical_object_ids)
    if not unresolved_set.issubset(object_order):
        raise ValueError("D3 candidate unresolved scope contains an unknown object")
    expected_unresolved = tuple(
        object_id
        for object_id in d2_identity.logical_object_ids
        if object_id in unresolved_set
    )
    coverage = {
        segment.segment_id: bytearray(len(segment.source_text))
        for segment in source_segments
    }
    compiled_entries: list[ParentProvisionEntryV3] = []
    entry_anchor_order: list[tuple[int, int, int]] = []

    for source_order, model_entry in enumerate(candidate.entries):
        source_ranges: list[ParentProvisionSourceRangeV3] = []
        range_objects: set[str] = set()
        evidence_refs: set[str] = set()
        local_order: list[tuple[int, int, int]] = []

        for model_span in model_entry.source_spans:
            expanded = _expand_candidate_span(
                model_span,
                source_segments=source_segments,
            )
            for segment, start_offset, end_offset in expanded:
                if segment.logical_object_id in unresolved_set:
                    raise ValueError("D3 unresolved object cannot contain parent spans")
                key = (
                    segment_order[segment.segment_id],
                    start_offset,
                    end_offset,
                )
                local_order.append(key)

                observed = coverage[segment.segment_id]
                for offset in range(start_offset, end_offset):
                    if observed[offset]:
                        raise ValueError("D3 candidate parent spans overlap")
                    observed[offset] = 1

                verbatim_text = segment.source_text[start_offset:end_offset]
                if not verbatim_text.strip():
                    continue
                source_ranges.append(
                    ParentProvisionSourceRangeV3(
                        segment_id=segment.segment_id,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        verbatim_text=verbatim_text,
                        verbatim_text_sha256=_source_text_sha256(verbatim_text),
                    )
                )
                range_objects.add(segment.logical_object_id)
                evidence_refs.update(
                    {
                        segment.logical_object_id,
                        segment.segment_id,
                        segment.source_handle,
                        *segment.evidence_refs,
                    }
                )

        if local_order != sorted(local_order):
            raise ValueError("D3 candidate spans must preserve validated D2 order")
        if not source_ranges:
            raise ValueError("D3 candidate parent span cannot be whitespace-only")
        if model_entry.explicit_label is not None and not any(
            model_entry.explicit_label in source_range.verbatim_text
            for source_range in source_ranges
        ):
            raise ValueError("D3 candidate explicit label is not source-grounded")
        logical_object_ids = tuple(sorted(range_objects, key=object_order.__getitem__))
        entry_anchor_order.append(local_order[0])
        complete_evidence_refs = tuple(sorted(evidence_refs))
        entry_evidence_refs = (
            complete_evidence_refs
            if len(complete_evidence_refs) <= PARENT_PROVISION_ENTRY_EVIDENCE_REF_LIMIT
            else tuple(sorted(logical_object_ids))
        )
        exact_ranges = tuple(source_ranges)
        compiled_entries.append(
            ParentProvisionEntryV3(
                parent_provision_id=stable_parent_provision_id(
                    source_sha256=d2_identity.source_sha256,
                    d2_artifact_sha256=d2_identity.artifact_sha256,
                    logical_object_ids=logical_object_ids,
                    source_ranges=exact_ranges,
                ),
                source_order=source_order,
                logical_object_ids=logical_object_ids,
                explicit_label=model_entry.explicit_label,
                ancestor_labels=model_entry.ancestor_labels,
                source_ranges=exact_ranges,
                requirementness=model_entry.requirementness,
                requirement_kind=model_entry.requirement_kind,
                technicality=model_entry.technicality,
                content_role=model_entry.content_role,
                confidence=model_entry.confidence,
                reason_codes=_canonical_machine_codes(model_entry.reason_codes),
                # Preserve the established representation while it fits the
                # locked public schema. For large table records, exact segment
                # ownership already lives in source_ranges and is reintroduced
                # into the professional evidence set, so only redundant refs are
                # compacted to logical-object grounding.
                evidence_refs=entry_evidence_refs,
            )
        )

    if entry_anchor_order != sorted(entry_anchor_order):
        raise ValueError("D3 candidate parents must preserve global D2 source order")
    _require_complete_non_whitespace_coverage(
        source_segments,
        coverage,
        unresolved_logical_object_ids=unresolved_set,
    )

    diagnostic_codes: set[str] = set()
    if d2_identity.status is ParentProvisionStatus.PARTIAL:
        diagnostic_codes.add("PREDECESSOR_D2_PARTIAL")
    if expected_unresolved:
        diagnostic_codes.add("UNRESOLVED_LOGICAL_OBJECT_SCOPE")
    if any(
        entry.requirementness is Requirementness.UNRESOLVED
        for entry in compiled_entries
    ):
        diagnostic_codes.add("UNRESOLVED_REQUIREMENTNESS")
    status = (
        ParentProvisionStatus.PARTIAL
        if diagnostic_codes
        else ParentProvisionStatus.COMPLETE
    )
    return D3HostCompiledParentProvisionDecisionV1(
        run_id=d2_identity.run_id,
        source_sha256=d2_identity.source_sha256,
        kit_identity=kit_identity,
        predecessor_d2_artifact_id=d2_identity.artifact_id,
        predecessor_d2_artifact_sha256=d2_identity.artifact_sha256,
        status=status,
        declared_logical_object_ids=tuple(
            object_id
            for object_id in d2_identity.logical_object_ids
            if object_id not in unresolved_set
        ),
        unresolved_logical_object_ids=expected_unresolved,
        entries=tuple(compiled_entries),
        diagnostic_codes=tuple(sorted(diagnostic_codes)),
    )


def _source_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expand_candidate_span(
    span: D3CandidateSourceSpanV2,
    *,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> tuple[tuple[D3ExactSourceSegmentV1, int, int], ...]:
    start_index = span.start_segment_ordinal
    end_index = span.end_segment_ordinal
    if start_index >= len(source_segments) or end_index >= len(source_segments):
        raise ValueError("D3 candidate span references an unknown segment ordinal")
    if end_index < start_index:
        raise ValueError("D3 candidate span reverses validated D2 order")
    start_segment = source_segments[start_index]
    end_segment = source_segments[end_index]
    if span.start_offset > len(start_segment.source_text):
        raise ValueError("D3 candidate span start exceeds its exact source segment")
    if span.end_offset > len(end_segment.source_text):
        raise ValueError("D3 candidate span end exceeds its exact source segment")
    if start_index == end_index and span.end_offset <= span.start_offset:
        raise ValueError("single-segment D3 candidate span must be non-empty")

    expanded: list[tuple[D3ExactSourceSegmentV1, int, int]] = []
    for index in range(start_index, end_index + 1):
        segment = source_segments[index]
        start_offset = span.start_offset if index == start_index else 0
        end_offset = span.end_offset if index == end_index else len(segment.source_text)
        if end_offset > start_offset:
            expanded.append((segment, start_offset, end_offset))
    if not expanded:
        raise ValueError("D3 candidate span must resolve to source evidence")
    return tuple(expanded)


def _canonical_machine_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    normalized = {
        re.sub(r"[^A-Z0-9]+", "_", item.strip().upper()).strip("_")[:80].rstrip("_")
        for item in value
    }
    if "" in normalized:
        raise ValueError("D3 candidate reason cannot normalize to empty")
    return tuple(sorted(normalized))


def _validate_source_scope(
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> None:
    if tuple(segment.segment_id for segment in source_segments) != (
        d2_identity.source_segment_ids
    ):
        raise ValueError("D3 compiler segments do not match validated D2 order")
    if any(
        segment.logical_object_id not in d2_identity.logical_object_ids
        for segment in source_segments
    ):
        raise ValueError("D3 compiler segment has no validated logical owner")
    if d3_source_scope_sha256(source_segments) != d2_identity.source_scope_sha256:
        raise ValueError("D3 compiler source scope drifted from validated D2")


def _require_complete_non_whitespace_coverage(
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    coverage: dict[str, bytearray],
    *,
    unresolved_logical_object_ids: set[str],
) -> None:
    for segment in source_segments:
        observed = coverage[segment.segment_id]
        if segment.logical_object_id in unresolved_logical_object_ids:
            if any(observed):
                raise ValueError("unresolved D3 scope cannot contain parent ranges")
            continue
        if any(
            not observed[offset] and not character.isspace()
            for offset, character in enumerate(segment.source_text)
        ):
            raise ValueError(
                "D3 candidate must disposition every non-whitespace source character"
            )


__all__ = [
    "D3CandidateSourceSpanV2",
    "D3HostCompiledParentProvisionDecisionV1",
    "D3ParentBoundaryClassificationCandidateV2",
    "D3ParentBoundaryClassificationEntryV2",
    "D3_PARENT_BOUNDARY_CLASSIFICATION_CANDIDATE_SCHEMA_VERSION",
    "compile_d3_parent_provision_decision",
]
