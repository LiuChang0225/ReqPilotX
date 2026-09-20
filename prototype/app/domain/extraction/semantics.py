from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.extraction.structure import StructureSourceRef, StructureSourceRefKind


SEMANTIC_CLASSIFICATION_SCHEMA_VERSION = "semantic-classification/v3"
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    """Immutable, provider-neutral value object for semantic derivations."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SemanticClass(str, Enum):
    HEADING = "HEADING"
    REQUIREMENT = "REQUIREMENT"
    BACKGROUND = "BACKGROUND"
    TECHNICAL_INFORMATION = "TECHNICAL_INFORMATION"
    SCHEDULE = "SCHEDULE"
    REFERENCE = "REFERENCE"
    PROJECT_RISK = "PROJECT_RISK"
    VERSION_METADATA = "VERSION_METADATA"
    ATTACHMENT = "ATTACHMENT"
    NOISE = "NOISE"
    UNRESOLVED = "UNRESOLVED"


class SemanticInformationKind(str, Enum):
    """Source-bound information kept below a requirement's decision boundary."""

    SCHEDULE_INFORMATION = "schedule_information"
    SAMPLE_AND_PHASE_INFORMATION = "sample_and_phase_information"


class RequirementSubtype(str, Enum):
    PRODUCT_REQUIREMENT = "PRODUCT_REQUIREMENT"
    PERFORMANCE_PARAMETER_REQUIREMENT = "PERFORMANCE_PARAMETER_REQUIREMENT"
    TEST_METHOD_REQUIREMENT = "TEST_METHOD_REQUIREMENT"
    TEST_EQUIPMENT_REQUIREMENT = "TEST_EQUIPMENT_REQUIREMENT"
    TEST_OBJECTIVE_REQUIREMENT = "TEST_OBJECTIVE_REQUIREMENT"
    ACCEPTANCE_CRITERION = "ACCEPTANCE_CRITERION"
    REGULATION_REQUIREMENT = "REGULATION_REQUIREMENT"
    INTERFACE_REQUIREMENT = "INTERFACE_REQUIREMENT"


class SemanticCandidateSource(str, Enum):
    RULE = "RULE"
    MODEL = "MODEL"
    HUMAN = "HUMAN"


class SemanticAnnotationStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"


class SemanticGroundingSpan(_StrictFrozenModel):
    """A half-open character span over one SourceIR-backed text view.

    The text itself deliberately remains outside the semantic snapshot.  The
    application boundary resolves it from SourceIR (or a versioned OCR/vision
    callback) and verifies that these spans cover the complete semantic input
    in source order.
    """

    grounding_span_id: str = Field(min_length=1, max_length=160)
    source_ref: StructureSourceRef
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_span(self) -> SemanticGroundingSpan:
        if self.end_offset < self.start_offset:
            raise ValueError("grounding spans use a half-open ordered interval")
        expected_id = stable_semantic_grounding_span_id(
            source_ref=self.source_ref,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
        )
        if self.grounding_span_id != expected_id:
            raise ValueError(
                "grounding_span_id must be derived from its source interval"
            )
        return self


class GroundedSemanticInformationFacet(_StrictFrozenModel):
    """One non-projecting information value grounded to an exact table cell.

    The facet deliberately carries a digest rather than copied customer text.
    Its owner remains the row-level semantic unit, so recording schedule or
    sample/phase information cannot create another RequirementView item.
    """

    information_facet_id: str = Field(
        pattern=r"^semantic-information-facet-[0-9a-f]{32}$"
    )
    semantic_snapshot_id: str = Field(min_length=1, max_length=160)
    owner_semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    information_kind: SemanticInformationKind
    semantic_class: SemanticClass
    source_ref: StructureSourceRef
    evidence_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_codes: tuple[str, ...]

    @field_validator("reason_codes")
    @classmethod
    def validate_facet_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not _REASON_CODE_PATTERN.fullmatch(code) for code in value):
            raise ValueError("information facet reason_codes require machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError(
                "information facet reason_codes must be unique and stably sorted"
            )
        return value

    @model_validator(mode="after")
    def validate_facet_shape(self) -> GroundedSemanticInformationFacet:
        expected_class = {
            SemanticInformationKind.SCHEDULE_INFORMATION: SemanticClass.SCHEDULE,
            SemanticInformationKind.SAMPLE_AND_PHASE_INFORMATION: (
                SemanticClass.TECHNICAL_INFORMATION
            ),
        }[self.information_kind]
        expected_reason = f"{self.information_kind.value}_signal"
        if self.semantic_class is not expected_class:
            raise ValueError(
                "information kind must use its provider-neutral SemanticClass"
            )
        if self.source_ref.kind is not StructureSourceRefKind.TABLE_CELL:
            raise ValueError("information facets require exact table-cell grounding")
        if self.reason_codes != (expected_reason,):
            raise ValueError(
                "information facets require the reason code fixed by their kind"
            )
        expected_id = stable_semantic_information_facet_id(
            semantic_snapshot_id=self.semantic_snapshot_id,
            owner_semantic_unit_id=self.owner_semantic_unit_id,
            owner_structure_node_id=self.owner_structure_node_id,
            information_kind=self.information_kind,
            semantic_class=self.semantic_class,
            source_ref=self.source_ref,
            evidence_text_sha256=self.evidence_text_sha256,
        )
        if self.information_facet_id != expected_id:
            raise ValueError(
                "information_facet_id must be derived from its exact evidence binding"
            )
        return self


class SemanticAnnotation(_StrictFrozenModel):
    semantic_annotation_id: str = Field(min_length=1, max_length=160)
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    candidate_source: SemanticCandidateSource
    provider_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    grounding_spans: tuple[SemanticGroundingSpan, ...]
    selected_evidence_source_refs: tuple[StructureSourceRef, ...] = ()
    is_continuation_fragment: bool = False
    status: SemanticAnnotationStatus
    reason_codes: tuple[str, ...] = ()

    @field_validator("semantic_annotation_id")
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic identity values must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(code) for code in value):
            raise ValueError("reason_codes must use machine-readable keys")
        if len(value) != len(set(value)):
            raise ValueError("reason_codes must not contain duplicates")
        if value != tuple(sorted(value)):
            raise ValueError("reason_codes must be stored in stable sorted order")
        return value

    @field_validator("selected_evidence_source_refs")
    @classmethod
    def validate_selected_evidence_source_refs(
        cls,
        value: tuple[StructureSourceRef, ...],
    ) -> tuple[StructureSourceRef, ...]:
        if len(value) != len(set(value)):
            raise ValueError("selected evidence source refs must be unique")
        return value

    @model_validator(mode="after")
    def validate_annotation_shape(self) -> SemanticAnnotation:
        if self.semantic_class is SemanticClass.REQUIREMENT:
            pass
        elif self.requirement_subtype is not None:
            raise ValueError("requirement_subtype is valid only for REQUIREMENT")
        if (
            self.is_continuation_fragment
            and self.semantic_class is not SemanticClass.REQUIREMENT
        ):
            raise ValueError(
                "only REQUIREMENT annotations may be continuation fragments"
            )

        is_unresolved_class = self.semantic_class is SemanticClass.UNRESOLVED
        is_unresolved_status = self.status is SemanticAnnotationStatus.UNRESOLVED
        if is_unresolved_class != is_unresolved_status:
            raise ValueError("UNRESOLVED class and status must be used together")
        if is_unresolved_status and not self.reason_codes:
            raise ValueError("UNRESOLVED annotations require a reason code")
        if is_unresolved_status and self.is_continuation_fragment:
            raise ValueError("UNRESOLVED annotations cannot claim a continuation")

        if self.candidate_source is SemanticCandidateSource.MODEL:
            if self.provider_run_id is None or not self.provider_run_id.strip():
                raise ValueError("MODEL candidates require provider_run_id")
            if not is_unresolved_status and not self.selected_evidence_source_refs:
                raise ValueError(
                    "resolved MODEL candidates require selected evidence refs"
                )
        elif self.provider_run_id is not None:
            raise ValueError("provider_run_id is valid only for MODEL candidates")
        elif self.selected_evidence_source_refs:
            raise ValueError(
                "selected evidence refs are valid only for MODEL candidates"
            )
        if (
            self.is_continuation_fragment
            and self.candidate_source is not SemanticCandidateSource.MODEL
        ):
            raise ValueError("continuation fragment decisions require MODEL provenance")

        span_ids = [span.grounding_span_id for span in self.grounding_spans]
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("grounding spans must not contain duplicates")
        previous_end_by_source: dict[StructureSourceRef, int] = {}
        closed_sources: set[StructureSourceRef] = set()
        active_source: StructureSourceRef | None = None
        for span in self.grounding_spans:
            if active_source is not None and span.source_ref != active_source:
                closed_sources.add(active_source)
            if span.source_ref in closed_sources:
                raise ValueError("grounding spans for one source must be contiguous")
            previous_end = previous_end_by_source.get(span.source_ref)
            if previous_end is not None and span.start_offset < previous_end:
                raise ValueError("grounding spans for one source must not overlap")
            previous_end_by_source[span.source_ref] = span.end_offset
            active_source = span.source_ref
        return self

    @property
    def target_structure_node_id(self) -> str:
        """Transient Structure-owner alias for provider-neutral callers.

        The alias is intentionally absent from serialization: semantic
        decision identity is ``semantic_unit_id`` from schema v2 onward.
        """

        return self.owner_structure_node_id


class SemanticClassificationSnapshot(_StrictFrozenModel):
    """A complete ordered semantic decision over one declared tree scope."""

    schema_version: Literal["semantic-classification/v3"] = (
        SEMANTIC_CLASSIFICATION_SCHEMA_VERSION
    )
    semantic_snapshot_id: str = Field(min_length=1, max_length=160)
    source_ir_id: str = Field(min_length=1, max_length=160)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    classifier_id: str = Field(min_length=1, max_length=160)
    classifier_version: str = Field(min_length=1, max_length=160)
    semantic_unit_plan_id: str = Field(pattern=r"^semantic-unit-plan-[0-9a-f]{32}$")
    unitizer_version: str = Field(min_length=1, max_length=160)
    scope_semantic_unit_ids: tuple[str, ...] = ()
    scope_owner_structure_node_ids: tuple[str, ...] = ()
    annotations: tuple[SemanticAnnotation, ...] = ()

    @field_validator(
        "semantic_snapshot_id",
        "source_ir_id",
        "structure_snapshot_id",
        "classifier_id",
        "classifier_version",
        "unitizer_version",
    )
    @classmethod
    def reject_blank_snapshot_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("semantic snapshot identity values must not be blank")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> SemanticClassificationSnapshot:
        if len(self.scope_semantic_unit_ids) != len(set(self.scope_semantic_unit_ids)):
            raise ValueError("semantic scope must contain unique semantic units")
        if len(self.scope_semantic_unit_ids) != len(
            self.scope_owner_structure_node_ids
        ):
            raise ValueError(
                "semantic units must retain one Structure-owner grounding each"
            )
        closed_owner_ids: set[str] = set()
        active_owner_id: str | None = None
        for owner_id in self.scope_owner_structure_node_ids:
            if active_owner_id is not None and owner_id != active_owner_id:
                closed_owner_ids.add(active_owner_id)
            if owner_id in closed_owner_ids:
                raise ValueError(
                    "semantic units for one Structure owner must remain contiguous"
                )
            active_owner_id = owner_id
        expected_snapshot_id = stable_semantic_snapshot_id(
            source_ir_id=self.source_ir_id,
            structure_snapshot_id=self.structure_snapshot_id,
            classifier_id=self.classifier_id,
            classifier_version=self.classifier_version,
            semantic_unit_plan_id=self.semantic_unit_plan_id,
            unitizer_version=self.unitizer_version,
            scope_semantic_unit_ids=self.scope_semantic_unit_ids,
        )
        if self.semantic_snapshot_id != expected_snapshot_id:
            raise ValueError(
                "semantic_snapshot_id must be derived from its stable scope"
            )

        observed_unit_ids = tuple(
            annotation.semantic_unit_id for annotation in self.annotations
        )
        if observed_unit_ids != self.scope_semantic_unit_ids:
            raise ValueError(
                "annotations must cover every scoped semantic unit exactly once and in order"
            )
        observed_owner_ids = tuple(
            annotation.owner_structure_node_id for annotation in self.annotations
        )
        if observed_owner_ids != self.scope_owner_structure_node_ids:
            raise ValueError(
                "annotations must preserve semantic-unit Structure ownership"
            )
        annotation_ids: set[str] = set()
        for annotation in self.annotations:
            if annotation.semantic_annotation_id in annotation_ids:
                raise ValueError("semantic_annotation_id must be unique")
            annotation_ids.add(annotation.semantic_annotation_id)
            expected_annotation_id = stable_semantic_annotation_id(
                semantic_snapshot_id=self.semantic_snapshot_id,
                semantic_unit_id=annotation.semantic_unit_id,
            )
            if annotation.semantic_annotation_id != expected_annotation_id:
                raise ValueError(
                    "semantic_annotation_id must be derived from snapshot and target"
                )
        return self

    @property
    def scope_structure_node_ids(self) -> tuple[str, ...]:
        """Transient Structure-owner alias for legacy in-process algorithms."""

        return self.scope_owner_structure_node_ids


def stable_semantic_snapshot_id(
    *,
    source_ir_id: str,
    structure_snapshot_id: str,
    classifier_id: str,
    classifier_version: str,
    semantic_unit_plan_id: str,
    unitizer_version: str,
    scope_semantic_unit_ids: tuple[str, ...],
) -> str:
    _require_non_blank(source_ir_id, "source_ir_id")
    _require_non_blank(structure_snapshot_id, "structure_snapshot_id")
    _require_non_blank(classifier_id, "classifier_id")
    _require_non_blank(classifier_version, "classifier_version")
    _require_non_blank(semantic_unit_plan_id, "semantic_unit_plan_id")
    _require_non_blank(unitizer_version, "unitizer_version")
    if len(scope_semantic_unit_ids) != len(set(scope_semantic_unit_ids)):
        raise ValueError("semantic scope must contain unique semantic units")
    return _stable_id(
        "semantic",
        source_ir_id,
        structure_snapshot_id,
        classifier_id,
        classifier_version,
        semantic_unit_plan_id,
        unitizer_version,
        list(scope_semantic_unit_ids),
    )


def stable_semantic_annotation_id(
    *,
    semantic_snapshot_id: str,
    semantic_unit_id: str,
) -> str:
    _require_non_blank(semantic_snapshot_id, "semantic_snapshot_id")
    _require_non_blank(semantic_unit_id, "semantic_unit_id")
    return _stable_id(
        "semantic-annotation",
        semantic_snapshot_id,
        semantic_unit_id,
    )


def stable_semantic_grounding_span_id(
    *,
    source_ref: StructureSourceRef,
    start_offset: int,
    end_offset: int,
) -> str:
    if not isinstance(source_ref, StructureSourceRef):
        raise TypeError("source_ref must be a StructureSourceRef")
    if start_offset < 0 or end_offset < start_offset:
        raise ValueError("grounding spans use a non-negative half-open interval")
    return _stable_id(
        "semantic-span",
        source_ref.model_dump(mode="json"),
        start_offset,
        end_offset,
    )


def stable_semantic_information_facet_id(
    *,
    semantic_snapshot_id: str,
    owner_semantic_unit_id: str,
    owner_structure_node_id: str,
    information_kind: SemanticInformationKind,
    semantic_class: SemanticClass,
    source_ref: StructureSourceRef,
    evidence_text_sha256: str,
) -> str:
    _require_non_blank(semantic_snapshot_id, "semantic_snapshot_id")
    _require_non_blank(owner_semantic_unit_id, "owner_semantic_unit_id")
    _require_non_blank(owner_structure_node_id, "owner_structure_node_id")
    if not isinstance(information_kind, SemanticInformationKind):
        raise TypeError("information_kind must be a SemanticInformationKind")
    if not isinstance(semantic_class, SemanticClass):
        raise TypeError("semantic_class must be a SemanticClass")
    if not isinstance(source_ref, StructureSourceRef):
        raise TypeError("source_ref must be a StructureSourceRef")
    if re.fullmatch(r"[0-9a-f]{64}", evidence_text_sha256) is None:
        raise ValueError("evidence_text_sha256 must be a lowercase SHA-256 digest")
    return _stable_id(
        "semantic-information-facet",
        semantic_snapshot_id,
        owner_semantic_unit_id,
        owner_structure_node_id,
        information_kind.value,
        semantic_class.value,
        source_ref.model_dump(mode="json"),
        evidence_text_sha256,
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
    "SEMANTIC_CLASSIFICATION_SCHEMA_VERSION",
    "GroundedSemanticInformationFacet",
    "RequirementSubtype",
    "SemanticAnnotation",
    "SemanticAnnotationStatus",
    "SemanticCandidateSource",
    "SemanticClass",
    "SemanticClassificationSnapshot",
    "SemanticGroundingSpan",
    "SemanticInformationKind",
    "stable_semantic_annotation_id",
    "stable_semantic_grounding_span_id",
    "stable_semantic_information_facet_id",
    "stable_semantic_snapshot_id",
]
