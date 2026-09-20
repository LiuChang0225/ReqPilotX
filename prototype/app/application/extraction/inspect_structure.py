from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.extraction.pdf_table_text_arbitration import (
    folded_pdf_table_text_nodes,
)
from app.application.extraction.resolve_structure import (
    superseded_native_text_source_ids,
)
from app.application.extraction.structure_boundary_rules import (
    INSPECTOR_ALPHABETIC_PEER_POLICY,
    NUMERIC_VALUE_LINE_PATTERN,
    STRUCTURE_BOUNDARY_RULESET_VERSION,
    AlphabeticMarkerEvidence,
    AlphabeticPeerPosition,
    alphabetic_peer_position,
    embedded_parenthesized_peer_sequences,
    evaluate_alphabetic_peer_transition,
    explicit_section_label_from_text,
    implicit_cjk_root_evidence_from_text,
    iter_document_region_markers,
    parse_alphabetic_parenthesized_marker,
)
from app.application.extraction.visual_document_region_evidence import (
    DEFAULT_VISUAL_DOCUMENT_REGION_EVIDENCE_POLICY,
    VisualDocumentRegionEvidence,
    collect_visual_document_region_evidence,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    PdfTextLineGeometry,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
)
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNode,
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)


STRUCTURE_INSPECTION_SCHEMA_VERSION = "structure-inspection/v1"
STRUCTURE_INSPECTION_RUNTIME_VERSION = "1.5.0"
_LEGACY_INSPECTION_IMPLEMENTATION_VERSION = "legacy"
_PDF_VISUAL_CAPTURE_RISK_DIAGNOSTIC_CODES = frozenset(
    {
        "pdf_full_page_image_with_invisible_text_layer",
        "pdf_full_page_image_without_visible_text",
        "pdf_vector_text_with_low_native_unicode_coverage",
        "pdf_visual_text_user_forced",
    }
)
_PDF_VISUAL_CAPTURE_RECONCILABLE_COMPANION_DIAGNOSTIC_CODES = frozenset(
    {"pdf_invisible_text_span_detected"}
)
_PDF_VISUAL_CAPTURE_SUCCESS_DIAGNOSTIC_CODE = "pdf_visual_text_observations_captured"
_CANDIDATE_ID_PATTERN = re.compile(r"^structure-inspection-candidate-[0-9a-f]{32}$")
_INSPECTION_ID_PATTERN = re.compile(r"^structure-inspection-[0-9a-f]{32}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NUMERIC_BOUNDARY_PATTERN = re.compile(
    r"(?m)^[ \t\u3000]{0,8}"
    r"(?P<marker>[1-9][0-9]{0,2}"
    r"(?:\.(?:0|[1-9][0-9]{0,2})){1,7})"
    r"(?=$|[ \t\u3000、.)）:：])"
)
_SINGLE_DOT_ROOT_BOUNDARY_PATTERN = re.compile(
    r"(?m)^[ \t\u3000]{0,8}"
    r"(?P<marker>[1-9][0-9]{0,2})"
    r"\.(?![0-9])"
    r"(?P<tail>[^\r\n]*)$"
)
_METRIC_NODE_KINDS = frozenset(
    {
        StructureNodeKind.SECTION,
        StructureNodeKind.CLAUSE,
        StructureNodeKind.CONTENT,
        StructureNodeKind.LIST_GROUP,
        StructureNodeKind.GROUP,
        StructureNodeKind.CONTINUATION,
    }
)
_MIN_LONG_SCOPE_CHAR_COUNT = 800
_ROBUST_BASELINE_MULTIPLIER = 6.0
_INLINE_DOTTED_NUMBER_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?P<marker>[1-9][0-9]{0,2}"
    r"(?:\.(?:0|[1-9][0-9]{0,2})){1,7})"
    r"(?=$|[\s\u3000、.)）:：])"
)
_ENGLISH_CONTINUATION_PREFIX = re.compile(
    r"^(?:"
    r"and|or|to|of|for|in|with|by|from|than|then|that|which|where|when|"
    r"if|is|are|was|were|be|been|being"
    r")\b",
    re.IGNORECASE,
)
_SENTENCE_TERMINATOR = re.compile(r"[.!?。！？；;：:]\s*$")
_SINGLE_DOT_REFERENCE_PREFIX = re.compile(
    r"^(?:"
    r"见|详见|参见|请见|参考|"
    r"see\b|refer\b|reference\b"
    r")",
    re.IGNORECASE,
)
_SINGLE_DOT_MEASUREMENT_PREFIX = re.compile(
    r"^(?:"
    r"[+\-−–—±~～<>≤≥]?\d|"
    r"(?:mm|cm|m|km|mg|g|kg|n|kn|pa|kpa|mpa|v|mv|a|ma|hz|khz|%)"
    r"(?:\b|\s)|"
    r"(?:℃|°c|°f|度|周|天|日|小时|时|分钟|分|秒|月|年|次|个|件|台|套|辆)"
    r"(?=$|\s|[，,。.;；:：、）)])"
    r")",
    re.IGNORECASE,
)
_SINGLE_DOT_ROOT_TITLE_MAX_CHARACTERS = 160
_FULL_WIDTH_TABLE_EDGE_TOLERANCE_POINTS = 8.0
_FULL_WIDTH_TABLE_CELL_MIN_WIDTH_RATIO = 0.9
_ALLOWED_TABLE_BOUNDARY_SUPPRESSION_REASONS = frozenset(
    {
        "folded_pdf_table_text",
        "inside_table_content",
    }
)
STRUCTURE_INSPECTION_REASON_CODES = frozenset(
    {
        "alphabetic_parenthesized_sequence_detected",
        "ambiguous_lettered_region_marker_requires_confirmation",
        "ambiguous_pdf_table_geometry",
        "boundary_already_materialized_by_structure_owner",
        "coherent_single_dot_root_sequence",
        "document_region_marker_shape_detected",
        "downstream_lettered_region_designator",
        "embedded_alphabetic_parenthesized_peer_sequence",
        "embedded_numeric_parenthesized_peer_sequence",
        "embedded_peer_sequence_not_root_anchored",
        "embedded_two_item_peer_sequence",
        "english_sentence_continuation",
        "folded_pdf_table_text",
        "full_width_table_boundary_heading",
        "inline_numeric_peer_sequence",
        "inside_figure_content",
        "inside_page_furniture",
        "inside_table_content",
        "inside_table_of_contents",
        "implicit_cjk_root_requires_corroboration",
        "implicit_cjk_root_shape_detected",
        "length_outlier",
        "nearby_document_region_qualifier",
        "nearby_document_region_self_reference",
        "not_credible_outline_shape",
        "not_credible_root_heading_shape",
        "numeric_scale_or_measurement",
        "native_text_superseded_by_visual_authority",
        "ocr_authority_text_unresolved",
        "ocr_authority_table_text_unresolved",
        "outline_marker_shape_detected",
        "outline_parent_label_mismatch",
        "outline_parent_missing",
        "reference_not_outline_boundary",
        "repeated_document_region_marker_within_materialized_region",
        "source_ir_failed",
        "source_ir_not_complete",
        "source_ir_partial",
        "source_node_not_grounded_by_structure",
        "standalone_document_region_boundary",
        "unconsumed_alphabetic_parenthesized_boundary",
        "unconsumed_internal_outline_boundary",
        "unconsumed_strong_boundary",
        "visual_text_facts_missing",
        "visual_page_top_centered_region_marker",
        "visual_document_region_marker_shape_detected",
        "visual_recognition_confidence_below_policy",
        "wrapped_paragraph_continuation",
    }
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureInspectionCandidateCategory(str, Enum):
    INTERNAL_OUTLINE_BOUNDARY = "INTERNAL_OUTLINE_BOUNDARY"
    DOCUMENT_REGION_BOUNDARY = "DOCUMENT_REGION_BOUNDARY"
    OUTLINE_PARENT_MISMATCH = "OUTLINE_PARENT_MISMATCH"
    OVERSIZED_UNSPLIT_SCOPE = "OVERSIZED_UNSPLIT_SCOPE"
    SOURCE_CAPTURE_GAP = "SOURCE_CAPTURE_GAP"


class StructureInspectionConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class StructureInspectionDisposition(str, Enum):
    ACTIONABLE = "ACTIONABLE"
    SUPPRESSED = "SUPPRESSED"


class StructureInspectionCandidateV1(_StrictFrozenModel):
    """One content-safe, source-bound suspicion; never an automatic repair."""

    candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN.pattern)
    category: StructureInspectionCandidateCategory
    source_ref: StructureSourceRef | None = None
    structure_node_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    page_number: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    confidence: StructureInspectionConfidence
    disposition: StructureInspectionDisposition
    safe_marker: str | None = Field(default=None, min_length=1, max_length=80)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_REASON_CODE_PATTERN.fullmatch(item) is None for item in value):
            raise ValueError("inspection reason codes must be machine-readable")
        if value != tuple(sorted(set(value))):
            raise ValueError("inspection reason codes must be unique and sorted")
        unknown = tuple(
            item for item in value if item not in STRUCTURE_INSPECTION_REASON_CODES
        )
        if unknown:
            raise ValueError("inspection reason codes must use the stable catalog")
        return value

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float] | None,
    ) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        x0, y0, x1, y1 = value
        if any(not math.isfinite(item) for item in value) or x1 <= x0 or y1 <= y0:
            raise ValueError("inspection bbox must be finite and non-empty")
        return value

    @model_validator(mode="after")
    def validate_candidate_shape(self) -> StructureInspectionCandidateV1:
        if self.bbox is not None and self.page_number is None:
            raise ValueError("inspection bbox requires a PDF page number")
        if self.category is StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP:
            if self.safe_marker is not None:
                raise ValueError("source capture gaps must not copy source text")
            if self.source_ref is None:
                if (
                    self.structure_node_id is not None
                    or self.start_offset != 0
                    or self.end_offset != 0
                    or self.page_number is not None
                    or self.bbox is not None
                ):
                    raise ValueError(
                        "document-level capture gaps cannot invent a source interval"
                    )
            elif self.end_offset <= self.start_offset:
                raise ValueError(
                    "source-bound capture gaps require a non-empty interval"
                )
        else:
            if self.source_ref is None:
                raise ValueError("boundary candidates require a source reference")
            if self.end_offset <= self.start_offset:
                raise ValueError("boundary candidates require a non-empty interval")
            if self.safe_marker is None:
                raise ValueError("boundary candidates require a content-safe marker")
        expected_id = _stable_candidate_id(
            category=self.category,
            source_ref=self.source_ref,
            structure_node_id=self.structure_node_id,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            confidence=self.confidence,
            disposition=self.disposition,
            safe_marker=self.safe_marker,
            reason_codes=self.reason_codes,
        )
        if self.candidate_id != expected_id:
            raise ValueError("candidate_id must bind the source interval and decision")
        return self


class StructureInspectionNodeMetricV1(_StrictFrozenModel):
    """Content-free measurements used to explain why a scope was selected."""

    structure_node_id: str = Field(min_length=1, max_length=160)
    kind: StructureNodeKind
    source_refs: tuple[StructureSourceRef, ...] = ()
    own_text_char_count: int = Field(ge=0)
    own_text_line_count: int = Field(ge=0)
    source_ref_count: int = Field(ge=0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    direct_child_count: int = Field(ge=0)
    length_outlier: bool
    unconsumed_strong_boundary_count: int = Field(ge=0)
    actionable_candidate_count: int = Field(ge=0)
    suppressed_candidate_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_metric_shape(self) -> StructureInspectionNodeMetricV1:
        if self.source_ref_count != len(self.source_refs):
            raise ValueError("source_ref_count must match the retained source refs")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("metric page range must be complete or absent")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("metric page range must be ordered")
        return self


class StructureInspectionSnapshotV1(_StrictFrozenModel):
    """Deterministic detect-only output; never authorization to mutate a tree."""

    schema_version: Literal["structure-inspection/v1"] = (
        STRUCTURE_INSPECTION_SCHEMA_VERSION
    )
    inspection_id: str = Field(pattern=_INSPECTION_ID_PATTERN.pattern)
    canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    resolver_id: str = Field(min_length=1, max_length=160)
    resolver_version: str = Field(min_length=1, max_length=160)
    inspection_runtime_version: str = Field(
        default=_LEGACY_INSPECTION_IMPLEMENTATION_VERSION,
        min_length=1,
        max_length=160,
    )
    boundary_ruleset_version: str = Field(
        default=_LEGACY_INSPECTION_IMPLEMENTATION_VERSION,
        min_length=1,
        max_length=160,
    )
    visual_text_snapshot_ids: tuple[str, ...] = ()
    inspected_visual_observation_count: int = Field(default=0, ge=0)
    inspected_source_node_count: int = Field(ge=0)
    grounded_source_node_count: int = Field(ge=0)
    candidates: tuple[StructureInspectionCandidateV1, ...] = ()
    node_metrics: tuple[StructureInspectionNodeMetricV1, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> StructureInspectionSnapshotV1:
        if self.grounded_source_node_count > self.inspected_source_node_count:
            raise ValueError("grounded count cannot exceed inspected source nodes")
        if (
            self.visual_text_snapshot_ids
            != tuple(sorted(set(self.visual_text_snapshot_ids)))
        ):
            raise ValueError(
                "visual text snapshot identities must be unique and canonical"
            )
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("inspection candidates require unique identities")
        metric_ids = tuple(item.structure_node_id for item in self.node_metrics)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("inspection node metrics require unique node identities")
        expected_sha256 = _inspection_canonical_sha256(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            structure_snapshot_id=self.structure_snapshot_id,
            resolver_id=self.resolver_id,
            resolver_version=self.resolver_version,
            inspection_runtime_version=self.inspection_runtime_version,
            boundary_ruleset_version=self.boundary_ruleset_version,
            visual_text_snapshot_ids=self.visual_text_snapshot_ids,
            inspected_visual_observation_count=(
                self.inspected_visual_observation_count
            ),
            inspected_source_node_count=self.inspected_source_node_count,
            grounded_source_node_count=self.grounded_source_node_count,
            candidates=self.candidates,
            node_metrics=self.node_metrics,
        )
        if self.canonical_sha256 != expected_sha256:
            raise ValueError("canonical_sha256 must bind the complete inspection")
        expected_id = f"structure-inspection-{expected_sha256[:32]}"
        if self.inspection_id != expected_id:
            raise ValueError("inspection_id must bind the canonical inspection")
        return self


@dataclass(frozen=True)
class _TextFact:
    source_ref: StructureSourceRef
    raw_text: str
    native_locator: NativeLocator
    source_order: int
    source_node: SourceNode
    pdf_text_lines: tuple[PdfTextLineGeometry, ...] = ()


@dataclass(frozen=True)
class _MetricDraft:
    node: StructureNode
    source_refs: tuple[StructureSourceRef, ...]
    own_text_char_count: int
    own_text_line_count: int
    page_start: int | None
    page_end: int | None
    direct_child_count: int
    length_outlier: bool


@dataclass(frozen=True)
class _SingleDotRootObservation:
    fact: _TextFact
    scope_owner: StructureNode
    ordinal: int
    start_offset: int
    end_offset: int
    table_boundary_heading: bool

    @property
    def order_key(self) -> tuple[int, int, str]:
        return (
            self.fact.source_order,
            self.start_offset,
            self.fact.source_ref.source_id,
        )


@dataclass(frozen=True)
class _AlphabeticListObservation:
    fact: _TextFact
    scope_id: str
    marker: AlphabeticMarkerEvidence
    position: AlphabeticPeerPosition
    materialized: bool
    suppression_reasons: tuple[str, ...]

    @property
    def letter(self) -> str:
        return self.marker.marker

    @property
    def ordinal(self) -> int:
        return self.marker.ordinal

    @property
    def start_offset(self) -> int:
        return self.marker.start_offset

    @property
    def end_offset(self) -> int:
        return self.marker.end_offset

    @property
    def order_key(self) -> tuple[int, int, str]:
        return (
            self.fact.source_order,
            self.start_offset,
            self.fact.source_ref.source_id,
        )


def inspect_structure(
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
) -> StructureInspectionSnapshotV1:
    """Inspect one immutable base tree without repairing or copying source text.

    This V1 interface deliberately accepts ``StructureTreeSnapshot`` only.
    Revision-effective source spans need their own explicit contract before an
    optimizer may consume the result; callers must not treat this detect-only
    snapshot as a repair instruction.
    """

    if structure_tree.source_ir_id != source_ir.source_ir_id:
        raise ValueError("structure inspection inputs must bind the same SourceIR")

    source_nodes_by_id = {node.source_node_id: node for node in source_ir.nodes}
    visual_observations_by_id = {
        observation.observation_id: observation
        for snapshot in source_ir.visual_text_snapshots
        for observation in snapshot.observations
    }
    text_facts = _text_facts(source_ir)
    facts_by_ref = {fact.source_ref: fact for fact in text_facts}
    owners_by_ref = _owners_by_ref(structure_tree)
    structure_nodes_by_id = {
        node.structure_node_id: node for node in structure_tree.nodes
    }
    depths_by_id = _structure_depths(structure_tree)
    pdf_table_reasons_by_source_id = _pdf_table_suppression_reasons(source_ir)
    capture_gap_reasons = _merged_reason_codes(
        _source_capture_gap_reasons(source_ir),
        (
            tuple(
                reason
                for finding_code, reason in (
                    (
                        "pdf_ocr_authority_text_unresolved",
                        "ocr_authority_text_unresolved",
                    ),
                    (
                        "pdf_ocr_authority_table_text_unresolved",
                        "ocr_authority_table_text_unresolved",
                    ),
                )
                if any(
                    finding.blocking and finding.code == finding_code
                    for finding in structure_tree.findings
                )
            )
        ),
    )
    superseded_native_source_ids = superseded_native_text_source_ids(source_ir)
    candidate_order_by_id: dict[str, tuple[int, str]] = {}
    initial_candidates: list[StructureInspectionCandidateV1] = []
    trusted_document_region_owner_by_marker: dict[str, str] = {}

    for fact in text_facts:
        if not fact.raw_text:
            continue
        owners = owners_by_ref.get(fact.source_ref, ())
        suppression_reasons = _merged_reason_codes(
            _suppression_reasons(
                fact,
                owners=owners,
                source_nodes_by_id=source_nodes_by_id,
            ),
            pdf_table_reasons_by_source_id.get(
                fact.source_ref.source_id,
                (),
            ),
            capture_gap_reasons,
        )
        implicit_root = implicit_cjk_root_evidence_from_text(fact.raw_text)
        if (
            implicit_root is not None
            and not implicit_root.negative_reason_codes
            and not _is_materialized_boundary(
                category=(
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                ),
                marker=implicit_root.label,
                start_offset=implicit_root.start_offset,
                fact=fact,
                owners=owners,
            )
        ):
            owner = _select_owner(
                fact.source_ref,
                start_offset=implicit_root.start_offset,
                owners=owners,
                depths_by_id=depths_by_id,
            )
            disposition = _disposition(suppression_reasons)
            reasons = (
                (
                    *suppression_reasons,
                    "implicit_cjk_root_shape_detected",
                )
                if suppression_reasons
                else ("implicit_cjk_root_requires_corroboration",)
            )
            page_number, bbox = _fact_location(
                fact,
                start_offset=implicit_root.start_offset,
                end_offset=implicit_root.end_offset,
            )
            candidate = _make_candidate(
                category=(
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                ),
                source_ref=fact.source_ref,
                structure_node_id=(
                    owner.structure_node_id if owner is not None else None
                ),
                start_offset=implicit_root.start_offset,
                end_offset=implicit_root.end_offset,
                page_number=page_number,
                bbox=bbox,
                confidence=StructureInspectionConfidence.MEDIUM,
                disposition=disposition,
                safe_marker=implicit_root.label,
                reason_codes=tuple(sorted(set(reasons))),
            )
            initial_candidates.append(candidate)
            candidate_order_by_id[candidate.candidate_id] = (
                fact.source_order,
                fact.source_ref.source_id,
            )

        for match in _NUMERIC_BOUNDARY_PATTERN.finditer(fact.raw_text):
            marker = match.group("marker")
            start_offset, end_offset = match.span("marker")
            if _is_materialized_boundary(
                category=(
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                ),
                marker=marker,
                start_offset=start_offset,
                fact=fact,
                owners=owners,
            ):
                continue
            candidate_suppression_reasons = _merged_reason_codes(
                suppression_reasons,
                _numeric_boundary_suppression_reasons(
                    fact,
                    match=match,
                ),
            )
            owner = _select_owner(
                fact.source_ref,
                start_offset=start_offset,
                owners=owners,
                depths_by_id=depths_by_id,
            )
            disposition = _disposition(candidate_suppression_reasons)
            reasons = (
                (
                    *candidate_suppression_reasons,
                    "outline_marker_shape_detected",
                )
                if candidate_suppression_reasons
                else ("unconsumed_internal_outline_boundary",)
            )
            page_number, bbox = _fact_location(
                fact,
                start_offset=start_offset,
                end_offset=end_offset,
            )
            candidate = _make_candidate(
                category=(
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                ),
                source_ref=fact.source_ref,
                structure_node_id=(
                    owner.structure_node_id if owner is not None else None
                ),
                start_offset=start_offset,
                end_offset=end_offset,
                page_number=page_number,
                bbox=bbox,
                confidence=(
                    StructureInspectionConfidence.HIGH
                    if marker.count(".") >= 2
                    else StructureInspectionConfidence.MEDIUM
                ),
                disposition=disposition,
                safe_marker=marker,
                reason_codes=tuple(sorted(set(reasons))),
            )
            initial_candidates.append(candidate)
            candidate_order_by_id[candidate.candidate_id] = (
                fact.source_order,
                fact.source_ref.source_id,
            )

        for region_marker in iter_document_region_markers(fact.raw_text):
            if region_marker.reference_like:
                continue
            start_offset = region_marker.start_offset
            end_offset = region_marker.end_offset
            marker = region_marker.safe_marker
            owner = _select_owner(
                fact.source_ref,
                start_offset=start_offset,
                owners=owners,
                depths_by_id=depths_by_id,
            )
            represented_owner = _represented_structure_boundary_owner(
                fact,
                start_offset=start_offset,
                owners=owners,
                depths_by_id=depths_by_id,
            )
            region_suppression_reasons: tuple[str, ...] = ()
            if represented_owner is not None:
                trusted_document_region_owner_by_marker.setdefault(
                    marker,
                    represented_owner.structure_node_id,
                )
                region_suppression_reasons = (
                    "boundary_already_materialized_by_structure_owner",
                )
            elif (
                owner is not None
                and (
                    trusted_owner_id := (
                        trusted_document_region_owner_by_marker.get(marker)
                    )
                )
                is not None
                and _structure_node_is_descendant_or_self(
                    owner.structure_node_id,
                    ancestor_structure_node_id=trusted_owner_id,
                    structure_nodes_by_id=structure_nodes_by_id,
                )
            ):
                region_suppression_reasons = (
                    "repeated_document_region_marker_within_materialized_region",
                )
            if _is_materialized_boundary(
                category=(
                    StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY
                ),
                marker=marker,
                start_offset=start_offset,
                fact=fact,
                owners=owners,
            ):
                continue
            candidate_suppression_reasons = _merged_reason_codes(
                suppression_reasons,
                region_suppression_reasons,
            )
            disposition = _disposition(candidate_suppression_reasons)
            reasons = (
                (
                    *candidate_suppression_reasons,
                    "document_region_marker_shape_detected",
                )
                if candidate_suppression_reasons
                else ("standalone_document_region_boundary",)
            )
            page_number, bbox = _fact_location(
                fact,
                start_offset=start_offset,
                end_offset=end_offset,
            )
            candidate = _make_candidate(
                category=(
                    StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY
                ),
                source_ref=fact.source_ref,
                structure_node_id=(
                    owner.structure_node_id if owner is not None else None
                ),
                start_offset=start_offset,
                end_offset=end_offset,
                page_number=page_number,
                bbox=bbox,
                confidence=(
                    StructureInspectionConfidence.HIGH
                    if not region_marker.tail.strip(" \t\u3000:：—-、")
                    else StructureInspectionConfidence.MEDIUM
                ),
                disposition=disposition,
                safe_marker=marker,
                reason_codes=tuple(sorted(set(reasons))),
            )
            initial_candidates.append(candidate)
            candidate_order_by_id[candidate.candidate_id] = (
                fact.source_order,
                fact.source_ref.source_id,
            )

    for evidence in collect_visual_document_region_evidence(source_ir):
        owners = owners_by_ref.get(evidence.source_ref, ())
        observation = visual_observations_by_id.get(
            evidence.source_ref.source_id
        )
        if observation is None:
            raise ValueError(
                "visual region evidence must resolve to its SourceIR fact"
            )
        if _visual_boundary_is_materialized(
            source_ref=evidence.source_ref,
            raw_text=observation.raw_text,
            start_offset=evidence.start_offset,
            owners=owners,
        ):
            continue
        owner = _select_owner(
            evidence.source_ref,
            start_offset=evidence.start_offset,
            owners=owners,
            depths_by_id=depths_by_id,
        )
        reason_codes = set(evidence.reason_codes)
        reason_codes.update(capture_gap_reasons)
        visual_suppression_reasons = set(capture_gap_reasons)
        if (
            evidence.recognition_confidence is not None
            and evidence.recognition_confidence
            < (
                DEFAULT_VISUAL_DOCUMENT_REGION_EVIDENCE_POLICY
                .minimum_calibrated_confidence
            )
        ):
            reason_codes.add("visual_recognition_confidence_below_policy")
            visual_suppression_reasons.add(
                "visual_recognition_confidence_below_policy"
            )
        if evidence.explicit_marker_shape:
            reason_codes.add(
                "document_region_marker_shape_detected"
                if capture_gap_reasons
                else "standalone_document_region_boundary"
            )
        candidate = _make_candidate(
            category=(
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY
            ),
            source_ref=evidence.source_ref,
            structure_node_id=(
                owner.structure_node_id if owner is not None else None
            ),
            start_offset=evidence.start_offset,
            end_offset=evidence.end_offset,
            page_number=evidence.page_number,
            bbox=evidence.bbox,
            confidence=_visual_region_candidate_confidence(evidence),
            disposition=_disposition(
                tuple(sorted(visual_suppression_reasons))
            ),
            safe_marker=evidence.safe_marker,
            reason_codes=tuple(sorted(reason_codes)),
        )
        candidate_order = (
            evidence.order_key[0] * 1_000_000 + evidence.order_key[1],
            evidence.order_key[2],
        )
        duplicate_index = _matching_logical_region_candidate_index(
            initial_candidates,
            candidate=candidate,
        )
        if duplicate_index is None:
            initial_candidates.append(candidate)
            candidate_order_by_id[candidate.candidate_id] = candidate_order
            continue
        existing = initial_candidates[duplicate_index]
        if not _prefer_visual_region_candidate(
            source_status=source_ir.status,
            existing=existing,
            visual=candidate,
        ):
            continue
        candidate_order_by_id.pop(existing.candidate_id, None)
        initial_candidates[duplicate_index] = candidate
        candidate_order_by_id[candidate.candidate_id] = candidate_order

    outline_parent_candidates = _outline_parent_mismatch_candidates(
        structure_tree,
        facts_by_ref=facts_by_ref,
        owners_by_ref=owners_by_ref,
        structure_nodes_by_id=structure_nodes_by_id,
        source_nodes_by_id=source_nodes_by_id,
        capture_gap_reasons=capture_gap_reasons,
    )
    for candidate in outline_parent_candidates:
        initial_candidates.append(candidate)
        fact = facts_by_ref[candidate.source_ref]
        candidate_order_by_id[candidate.candidate_id] = (
            fact.source_order,
            fact.source_ref.source_id,
        )

    alphabetic_list_candidates = _coherent_alphabetic_list_candidates(
        text_facts=text_facts,
        owners_by_ref=owners_by_ref,
        structure_nodes_by_id=structure_nodes_by_id,
        depths_by_id=depths_by_id,
        source_nodes_by_id=source_nodes_by_id,
        pdf_table_reasons_by_source_id=pdf_table_reasons_by_source_id,
        capture_gap_reasons=capture_gap_reasons,
    )
    for candidate in alphabetic_list_candidates:
        initial_candidates.append(candidate)
        fact = facts_by_ref[candidate.source_ref]
        candidate_order_by_id[candidate.candidate_id] = (
            fact.source_order,
            fact.source_ref.source_id,
        )

    embedded_peer_candidates = _embedded_parenthesized_peer_candidates(
        text_facts=text_facts,
        owners_by_ref=owners_by_ref,
        depths_by_id=depths_by_id,
        source_nodes_by_id=source_nodes_by_id,
        pdf_table_reasons_by_source_id=pdf_table_reasons_by_source_id,
        capture_gap_reasons=capture_gap_reasons,
    )
    for candidate in embedded_peer_candidates:
        initial_candidates.append(candidate)
        fact = facts_by_ref[candidate.source_ref]
        candidate_order_by_id[candidate.candidate_id] = (
            fact.source_order,
            fact.source_ref.source_id,
        )

    single_dot_root_candidates = _coherent_single_dot_root_candidates(
        source_ir,
        text_facts=text_facts,
        owners_by_ref=owners_by_ref,
        structure_nodes_by_id=structure_nodes_by_id,
        depths_by_id=depths_by_id,
        source_nodes_by_id=source_nodes_by_id,
        pdf_table_reasons_by_source_id=pdf_table_reasons_by_source_id,
        capture_gap_reasons=capture_gap_reasons,
    )
    for candidate in single_dot_root_candidates:
        initial_candidates.append(candidate)
        fact = facts_by_ref[candidate.source_ref]
        candidate_order_by_id[candidate.candidate_id] = (
            fact.source_order,
            fact.source_ref.source_id,
        )

    if capture_gap_reasons:
        capture_gap = _make_candidate(
            category=StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
            source_ref=None,
            structure_node_id=None,
            start_offset=0,
            end_offset=0,
            page_number=None,
            bbox=None,
            confidence=StructureInspectionConfidence.HIGH,
            disposition=StructureInspectionDisposition.ACTIONABLE,
            safe_marker=None,
            reason_codes=capture_gap_reasons,
        )
        initial_candidates.append(capture_gap)
        candidate_order_by_id[capture_gap.candidate_id] = (-1, "")

    grounded_source_node_ids = {
        ref.source_id
        for node in structure_tree.nodes
        for ref in node.source_refs
        if ref.kind is StructureSourceRefKind.SOURCE_NODE
    }
    for source_node in source_ir.nodes:
        if (
            not source_node.raw_text
            or source_node.source_node_id in grounded_source_node_ids
        ):
            continue
        fact = facts_by_ref[
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=source_node.source_node_id,
            )
        ]
        suppression_reasons = _suppression_reasons(
            fact,
            owners=(),
            source_nodes_by_id=source_nodes_by_id,
        )
        page_number, bbox = _fact_location(
            fact,
            start_offset=0,
            end_offset=len(source_node.raw_text),
        )
        candidate = _make_candidate(
            category=StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
            source_ref=fact.source_ref,
            structure_node_id=None,
            start_offset=0,
            end_offset=len(source_node.raw_text),
            page_number=page_number,
            bbox=bbox,
            confidence=StructureInspectionConfidence.HIGH,
            disposition=_disposition(suppression_reasons),
            safe_marker=None,
            reason_codes=tuple(
                sorted(
                    {
                        "source_node_not_grounded_by_structure",
                        *suppression_reasons,
                    }
                )
            ),
        )
        initial_candidates.append(candidate)
        candidate_order_by_id[candidate.candidate_id] = (
            source_node.source_order,
            source_node.source_node_id,
        )

    if superseded_native_source_ids:
        suppressed_candidates: list[StructureInspectionCandidateV1] = []
        for candidate in initial_candidates:
            source_ref = candidate.source_ref
            if (
                source_ref is None
                or source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
                or source_ref.source_id not in superseded_native_source_ids
            ):
                suppressed_candidates.append(candidate)
                continue
            order = candidate_order_by_id.pop(candidate.candidate_id)
            suppressed = _make_candidate(
                category=candidate.category,
                source_ref=candidate.source_ref,
                structure_node_id=candidate.structure_node_id,
                start_offset=candidate.start_offset,
                end_offset=candidate.end_offset,
                page_number=candidate.page_number,
                bbox=candidate.bbox,
                confidence=candidate.confidence,
                disposition=StructureInspectionDisposition.SUPPRESSED,
                safe_marker=candidate.safe_marker,
                reason_codes=tuple(
                    sorted(
                        {
                            *candidate.reason_codes,
                            "native_text_superseded_by_visual_authority",
                        }
                    )
                ),
            )
            suppressed_candidates.append(suppressed)
            candidate_order_by_id[suppressed.candidate_id] = order
        initial_candidates = suppressed_candidates

    metric_drafts = _metric_drafts(
        structure_tree,
        facts_by_ref=facts_by_ref,
        source_nodes_by_id=source_nodes_by_id,
        owners_by_ref=owners_by_ref,
    )
    strong_candidates_by_node: dict[str, list[StructureInspectionCandidateV1]] = {}
    for candidate in initial_candidates:
        if (
            candidate.structure_node_id is not None
            and candidate.disposition is StructureInspectionDisposition.ACTIONABLE
            and candidate.category
            in {
                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            }
        ):
            strong_candidates_by_node.setdefault(
                candidate.structure_node_id,
                [],
            ).append(candidate)

    oversized_candidates: list[StructureInspectionCandidateV1] = []
    for draft in metric_drafts:
        strong_candidates = strong_candidates_by_node.get(
            draft.node.structure_node_id,
            (),
        )
        if not draft.length_outlier or not strong_candidates:
            continue
        anchor = min(
            strong_candidates,
            key=lambda candidate: (
                candidate_order_by_id[candidate.candidate_id],
                candidate.start_offset,
                candidate.candidate_id,
            ),
        )
        oversized = _make_candidate(
            category=StructureInspectionCandidateCategory.OVERSIZED_UNSPLIT_SCOPE,
            source_ref=anchor.source_ref,
            structure_node_id=draft.node.structure_node_id,
            start_offset=anchor.start_offset,
            end_offset=anchor.end_offset,
            page_number=anchor.page_number,
            bbox=anchor.bbox,
            confidence=anchor.confidence,
            disposition=StructureInspectionDisposition.ACTIONABLE,
            safe_marker=anchor.safe_marker,
            reason_codes=(
                "length_outlier",
                "unconsumed_strong_boundary",
            ),
        )
        oversized_candidates.append(oversized)
        candidate_order_by_id[oversized.candidate_id] = candidate_order_by_id[
            anchor.candidate_id
        ]

    candidates = tuple(
        sorted(
            (*initial_candidates, *oversized_candidates),
            key=lambda candidate: (
                candidate_order_by_id[candidate.candidate_id],
                candidate.start_offset,
                candidate.end_offset,
                candidate.category.value,
                candidate.disposition.value,
                candidate.candidate_id,
            ),
        )
    )
    node_metrics = _materialize_metrics(
        metric_drafts,
        candidates=candidates,
    )
    visual_text_snapshot_ids = tuple(
        sorted(
            snapshot.visual_text_snapshot_id
            for snapshot in source_ir.visual_text_snapshots
        )
    )
    inspected_visual_observation_count = sum(
        len(snapshot.observations)
        for snapshot in source_ir.visual_text_snapshots
    )
    canonical_sha256 = _inspection_canonical_sha256(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        structure_snapshot_id=structure_tree.structure_snapshot_id,
        resolver_id=structure_tree.resolver_id,
        resolver_version=structure_tree.resolver_version,
        inspection_runtime_version=STRUCTURE_INSPECTION_RUNTIME_VERSION,
        boundary_ruleset_version=STRUCTURE_BOUNDARY_RULESET_VERSION,
        visual_text_snapshot_ids=visual_text_snapshot_ids,
        inspected_visual_observation_count=(
            inspected_visual_observation_count
        ),
        inspected_source_node_count=len(source_ir.nodes),
        grounded_source_node_count=len(
            grounded_source_node_ids.intersection(source_nodes_by_id)
        ),
        candidates=candidates,
        node_metrics=node_metrics,
    )
    return StructureInspectionSnapshotV1(
        inspection_id=f"structure-inspection-{canonical_sha256[:32]}",
        canonical_sha256=canonical_sha256,
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        structure_snapshot_id=structure_tree.structure_snapshot_id,
        resolver_id=structure_tree.resolver_id,
        resolver_version=structure_tree.resolver_version,
        inspection_runtime_version=STRUCTURE_INSPECTION_RUNTIME_VERSION,
        boundary_ruleset_version=STRUCTURE_BOUNDARY_RULESET_VERSION,
        visual_text_snapshot_ids=visual_text_snapshot_ids,
        inspected_visual_observation_count=(
            inspected_visual_observation_count
        ),
        inspected_source_node_count=len(source_ir.nodes),
        grounded_source_node_count=len(
            grounded_source_node_ids.intersection(source_nodes_by_id)
        ),
        candidates=candidates,
        node_metrics=node_metrics,
    )


def _text_facts(source_ir: SourceIRSnapshot) -> tuple[_TextFact, ...]:
    facts: list[_TextFact] = []
    for node in source_ir.nodes:
        source_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=node.source_node_id,
        )
        facts.append(
            _TextFact(
                source_ref=source_ref,
                raw_text=node.raw_text,
                native_locator=node.native_locator,
                source_order=node.source_order,
                source_node=node,
                pdf_text_lines=node.pdf_text_lines,
            )
        )
        if node.table_payload is None:
            continue
        for cell in sorted(
            node.table_payload.cells,
            key=lambda item: (item.row, item.column, item.cell_id),
        ):
            facts.append(
                _TextFact(
                    source_ref=StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=cell.cell_id,
                    ),
                    raw_text=cell.raw_text,
                    native_locator=cell.native_locator,
                    source_order=node.source_order,
                    source_node=node,
                )
            )
    return tuple(facts)


def _visual_region_candidate_confidence(
    evidence: VisualDocumentRegionEvidence,
) -> StructureInspectionConfidence:
    """Keep one-provider visual evidence below independent-source confidence."""

    if (
        evidence.recognition_confidence is not None
        and evidence.recognition_confidence
        < (
            DEFAULT_VISUAL_DOCUMENT_REGION_EVIDENCE_POLICY
            .minimum_calibrated_confidence
        )
    ):
        return StructureInspectionConfidence.LOW
    return StructureInspectionConfidence.MEDIUM


def _matching_logical_region_candidate_index(
    candidates: list[StructureInspectionCandidateV1],
    *,
    candidate: StructureInspectionCandidateV1,
) -> int | None:
    for index, existing in enumerate(candidates):
        if (
            existing.category
            is StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY
            and existing.page_number == candidate.page_number
            and _normalized_region_marker(existing.safe_marker)
            == _normalized_region_marker(candidate.safe_marker)
            and _region_bboxes_represent_same_boundary(
                existing.bbox,
                candidate.bbox,
            )
        ):
            return index
    return None


def _normalized_region_marker(value: str | None) -> str:
    return re.sub(r"[\s\u3000]+", "", value or "").casefold()


def _region_bboxes_represent_same_boundary(
    first: tuple[float, float, float, float] | None,
    second: tuple[float, float, float, float] | None,
) -> bool:
    if first is None or second is None:
        return False
    intersection_width = max(
        0.0,
        min(first[2], second[2]) - max(first[0], second[0]),
    )
    intersection_height = max(
        0.0,
        min(first[3], second[3]) - max(first[1], second[1]),
    )
    intersection_area = intersection_width * intersection_height
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    smaller_area = min(first_area, second_area)
    return smaller_area > 0.0 and intersection_area / smaller_area >= 0.50


def _prefer_visual_region_candidate(
    *,
    source_status: SourceIRStatus,
    existing: StructureInspectionCandidateV1,
    visual: StructureInspectionCandidateV1,
) -> bool:
    if (
        existing.source_ref is not None
        and existing.source_ref.kind
        is not StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
    ):
        return source_status is not SourceIRStatus.COMPLETE
    existing_preference = (
        existing.disposition is StructureInspectionDisposition.ACTIONABLE,
        _inspection_confidence_rank(existing.confidence),
    )
    visual_preference = (
        visual.disposition is StructureInspectionDisposition.ACTIONABLE,
        _inspection_confidence_rank(visual.confidence),
    )
    if visual_preference != existing_preference:
        return visual_preference > existing_preference
    return visual.candidate_id < existing.candidate_id


def _inspection_confidence_rank(
    confidence: StructureInspectionConfidence,
) -> int:
    return {
        StructureInspectionConfidence.LOW: 0,
        StructureInspectionConfidence.MEDIUM: 1,
        StructureInspectionConfidence.HIGH: 2,
    }[confidence]


def _visual_boundary_is_materialized(
    *,
    source_ref: StructureSourceRef,
    raw_text: str,
    start_offset: int,
    owners: tuple[StructureNode, ...],
) -> bool:
    first_text_offset = _first_non_whitespace_offset(raw_text)
    for owner in owners:
        if owner.kind is not StructureNodeKind.SECTION:
            continue
        spans = tuple(
            span
            for span in owner.semantic_source_spans
            if span.source_ref == source_ref
        )
        starts_here = (
            any(
                span.start_offset <= start_offset < span.end_offset
                and not raw_text[span.start_offset:start_offset].strip()
                for span in spans
            )
            if spans
            else start_offset == first_text_offset
        )
        if starts_here and owner.boundary_basis in {
            StructureBoundaryBasis.VISUAL_TEXT,
            StructureBoundaryBasis.MODEL_SUGGESTION,
        }:
            return True
    return False


def _owners_by_ref(
    structure_tree: StructureTreeSnapshot,
) -> dict[StructureSourceRef, tuple[StructureNode, ...]]:
    owners: dict[StructureSourceRef, list[StructureNode]] = {}
    for node in structure_tree.nodes:
        for source_ref in node.source_refs:
            owners.setdefault(source_ref, []).append(node)
    return {
        source_ref: tuple(sorted(nodes, key=lambda item: item.source_order))
        for source_ref, nodes in owners.items()
    }


def _structure_depths(
    structure_tree: StructureTreeSnapshot,
) -> dict[str, int]:
    nodes_by_id = {node.structure_node_id: node for node in structure_tree.nodes}
    depths: dict[str, int] = {}

    def depth(node: StructureNode) -> int:
        cached = depths.get(node.structure_node_id)
        if cached is not None:
            return cached
        parent_id = node.parent_structure_node_id
        value = 0 if parent_id is None else depth(nodes_by_id[parent_id]) + 1
        depths[node.structure_node_id] = value
        return value

    for node in structure_tree.nodes:
        depth(node)
    return depths


def _outline_parent_mismatch_candidates(
    structure_tree: StructureTreeSnapshot,
    *,
    facts_by_ref: dict[StructureSourceRef, _TextFact],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
    structure_nodes_by_id: dict[str, StructureNode],
    source_nodes_by_id: dict[str, SourceNode],
    capture_gap_reasons: tuple[str, ...],
) -> tuple[StructureInspectionCandidateV1, ...]:
    """Find explicit outline nodes whose ancestry contradicts their label.

    A dotted label carries a deterministic namespace: ``N.x`` belongs below
    ``N`` and ``N.x.y`` belongs below ``N.x``.  Non-outline wrapper nodes may
    exist between them, so the comparison uses the nearest explicit SECTION
    ancestor rather than requiring a direct parent.

    The source grammar also supports parenthesized outline families.  A
    source-backed ``N.x）`` may be a direct child of the exact ``N）`` CLAUSE;
    that is a valid local outline namespace, not a parent mismatch.
    """

    candidates: list[StructureInspectionCandidateV1] = []
    for node in structure_tree.nodes:
        explicit_label = node.explicit_label
        if (
            node.kind is not StructureNodeKind.SECTION
            or explicit_label is None
            or (expected_parent_label := _expected_outline_parent_label(explicit_label))
            is None
        ):
            continue
        evidence = _explicit_outline_boundary_evidence(
            node,
            explicit_label=explicit_label,
            facts_by_ref=facts_by_ref,
        )
        if evidence is None:
            # The detect-only contract must remain source-bound.  A malformed
            # tree without locatable label evidence is handled by existing
            # grounding checks rather than inventing an interval here.
            continue
        fact, start_offset, end_offset = evidence
        direct_parent = (
            structure_nodes_by_id.get(node.parent_structure_node_id)
            if node.parent_structure_node_id is not None
            else None
        )
        if _is_valid_parenthesized_outline_child(
            explicit_label=explicit_label,
            fact=fact,
            marker_end_offset=end_offset,
            direct_parent=direct_parent,
        ):
            continue
        actual_parent = _nearest_explicit_section_ancestor(
            node,
            structure_nodes_by_id=structure_nodes_by_id,
        )
        if (
            actual_parent is not None
            and actual_parent.explicit_label == expected_parent_label
        ):
            continue

        suppression_reasons = _merged_reason_codes(
            _suppression_reasons(
                fact,
                owners=owners_by_ref.get(fact.source_ref, ()),
                source_nodes_by_id=source_nodes_by_id,
            ),
            capture_gap_reasons,
        )
        mismatch_reason = (
            "outline_parent_missing"
            if actual_parent is None
            else "outline_parent_label_mismatch"
        )
        reasons = tuple(
            sorted(
                {
                    mismatch_reason,
                    *suppression_reasons,
                }
            )
        )
        page_number, bbox = _fact_location(
            fact,
            start_offset=start_offset,
            end_offset=end_offset,
        )
        candidates.append(
            _make_candidate(
                category=(StructureInspectionCandidateCategory.OUTLINE_PARENT_MISMATCH),
                source_ref=fact.source_ref,
                structure_node_id=node.structure_node_id,
                start_offset=start_offset,
                end_offset=end_offset,
                page_number=page_number,
                bbox=bbox,
                confidence=(
                    StructureInspectionConfidence.HIGH
                    if actual_parent is not None
                    else StructureInspectionConfidence.MEDIUM
                ),
                disposition=_disposition(suppression_reasons),
                safe_marker=explicit_label,
                reason_codes=reasons,
            )
        )
    return tuple(candidates)


def _expected_outline_parent_label(explicit_label: str) -> str | None:
    parts = explicit_label.split(".")
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "0"):
        return None
    return ".".join(parts[:-1])


def _explicit_outline_boundary_evidence(
    node: StructureNode,
    *,
    explicit_label: str,
    facts_by_ref: dict[StructureSourceRef, _TextFact],
) -> tuple[_TextFact, int, int] | None:
    label_pattern = re.compile(
        r"(?m)^[ \t\u3000]{0,8}"
        r"(?P<marker>"
        + r"[ \t]*\.[ \t]*".join(re.escape(part) for part in explicit_label.split("."))
        + r")(?=$|[ \t\u3000、.)）:：])"
    )
    spans_by_ref = {span.source_ref: span for span in node.semantic_source_spans}
    for source_ref in node.effective_semantic_source_refs:
        fact = facts_by_ref.get(source_ref)
        if fact is None or not fact.raw_text:
            continue
        span = spans_by_ref.get(source_ref)
        for match in label_pattern.finditer(fact.raw_text):
            start_offset, end_offset = match.span("marker")
            if span is not None and not (
                span.start_offset <= start_offset and end_offset <= span.end_offset
            ):
                continue
            return fact, start_offset, end_offset
    return None


def _nearest_explicit_section_ancestor(
    node: StructureNode,
    *,
    structure_nodes_by_id: dict[str, StructureNode],
) -> StructureNode | None:
    current_id = node.parent_structure_node_id
    visited: set[str] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        current = structure_nodes_by_id.get(current_id)
        if current is None:
            return None
        if (
            current.kind is StructureNodeKind.SECTION
            and current.explicit_label is not None
        ):
            return current
        current_id = current.parent_structure_node_id
    return None


def _is_valid_parenthesized_outline_child(
    *,
    explicit_label: str,
    fact: _TextFact,
    marker_end_offset: int,
    direct_parent: StructureNode | None,
) -> bool:
    parts = explicit_label.split(".")
    if (
        len(parts) != 2
        or direct_parent is None
        or direct_parent.kind is not StructureNodeKind.CLAUSE
        or direct_parent.list_ordinal != int(parts[0])
    ):
        return False
    suffix = fact.raw_text[marker_end_offset:].lstrip(" \t")
    return suffix.startswith((")", "）"))


def _source_capture_gap_reasons(
    source_ir: SourceIRSnapshot,
) -> tuple[str, ...]:
    """Return snapshot-level blockers before any structure repair is considered.

    A partial fact layer cannot prove that an apparent long scope is a
    structure defect.  PDF snapshots without visual text observations are the
    important case: missing glyphs, hidden appendices, and image-only headings
    must be recovered at capture time rather than guessed by a structure LLM.
    """

    native_formats = {node.native_locator.format for node in source_ir.nodes}
    visual_capture_required = any(
        diagnostic.code in _PDF_VISUAL_CAPTURE_RISK_DIAGNOSTIC_CODES
        for diagnostic in source_ir.diagnostics
    )
    if (
        NativeDocumentFormat.PDF in native_formats
        and visual_capture_required
        and not _partial_pdf_visual_capture_is_reconciled(source_ir)
    ):
        reasons = {"visual_text_facts_missing"}
        if source_ir.status is not SourceIRStatus.COMPLETE:
            reasons.add("source_ir_not_complete")
        if source_ir.status is SourceIRStatus.PARTIAL:
            reasons.add("source_ir_partial")
        elif source_ir.status is SourceIRStatus.FAILED:
            reasons.add("source_ir_failed")
        return tuple(sorted(reasons))
    if source_ir.status is SourceIRStatus.COMPLETE:
        return ()
    if (
        source_ir.status is SourceIRStatus.PARTIAL
        and NativeDocumentFormat.PDF in native_formats
        and _partial_pdf_visual_capture_is_reconciled(source_ir)
    ):
        return ()
    reasons = {"source_ir_not_complete"}
    if source_ir.status is SourceIRStatus.PARTIAL:
        reasons.add("source_ir_partial")
    else:
        reasons.add("source_ir_failed")
    if (
        NativeDocumentFormat.PDF in native_formats
        and not source_ir.visual_text_snapshots
    ):
        reasons.add("visual_text_facts_missing")
    return tuple(sorted(reasons))


def _partial_pdf_visual_capture_is_reconciled(
    source_ir: SourceIRSnapshot,
) -> bool:
    """Prove that a PDF's visual-only PARTIAL causes were exactly recovered.

    ``SourceIRStatus.PARTIAL`` remains the native-fidelity truth.  Inspection
    may stop treating it as a capture gap only when every page carrying one of
    the adapter's narrow visual-routing diagnostics has a source-bound,
    non-empty TEXT snapshot.  Any other warning/error or visual-provider
    diagnostic fails closed because it may represent an independent partial
    cause.
    """

    required_pages: set[int] = set()
    for diagnostic in source_ir.diagnostics:
        if diagnostic.code not in _PDF_VISUAL_CAPTURE_RISK_DIAGNOSTIC_CODES:
            continue
        locator = diagnostic.native_locator
        if (
            locator is None
            or locator.format is not NativeDocumentFormat.PDF
            or locator.page_number is None
        ):
            return False
        required_pages.add(locator.page_number)
    if not required_pages:
        return False

    recovered_pages = {
        snapshot.page_number
        for snapshot in source_ir.visual_text_snapshots
        if snapshot.source_sha256 == source_ir.source_sha256
        and "TEXT" in snapshot.evaluated_content_types
        and any(
            observation.content_type == "TEXT"
            and observation.raw_text.strip()
            and observation.source_sha256 == source_ir.source_sha256
            and observation.page_number == snapshot.page_number
            and observation.page_image_sha256 == snapshot.page_image_sha256
            for observation in snapshot.observations
        )
    }
    if not required_pages.issubset(recovered_pages):
        return False

    allowed_visual_diagnostics = {
        *_PDF_VISUAL_CAPTURE_RISK_DIAGNOSTIC_CODES,
        _PDF_VISUAL_CAPTURE_SUCCESS_DIAGNOSTIC_CODE,
    }
    for diagnostic in source_ir.diagnostics:
        if diagnostic.code in allowed_visual_diagnostics:
            continue
        if (
            diagnostic.code
            in _PDF_VISUAL_CAPTURE_RECONCILABLE_COMPANION_DIAGNOSTIC_CODES
        ):
            locator = diagnostic.native_locator
            if (
                locator is not None
                and locator.format is NativeDocumentFormat.PDF
                and locator.page_number in required_pages
                and locator.page_number in recovered_pages
            ):
                continue
            return False
        if diagnostic.code.startswith("pdf_visual_text_"):
            return False
        if diagnostic.severity is not SourceDiagnosticSeverity.INFO:
            return False
    return True


def _coherent_alphabetic_list_candidates(
    *,
    text_facts: tuple[_TextFact, ...],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
    structure_nodes_by_id: dict[str, StructureNode],
    depths_by_id: dict[str, int],
    source_nodes_by_id: dict[str, SourceNode],
    pdf_table_reasons_by_source_id: dict[str, tuple[str, ...]],
    capture_gap_reasons: tuple[str, ...],
) -> tuple[StructureInspectionCandidateV1, ...]:
    """Detect source-backed ``a)``/``b）`` runs without treating letters alone.

    A run must start at ``a`` or ``A`` and then remain consecutive within the
    same case family, structure scope, source/page neighbourhood, and
    indentation band.  The output is detect-only; a run already represented by
    exact CLAUSE ordinals produces no candidate.
    """

    observations_by_family: dict[
        tuple[str, bool],
        list[_AlphabeticListObservation],
    ] = {}
    for fact in text_facts:
        if (
            fact.source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
            or fact.source_node.kind
            not in {
                SourceNodeKind.PARAGRAPH,
                SourceNodeKind.LIST,
                SourceNodeKind.LIST_ITEM,
            }
            or not fact.raw_text
            or (
                marker := parse_alphabetic_parenthesized_marker(
                    fact.raw_text,
                    require_payload=True,
                )
            )
            is None
        ):
            continue
        owners = owners_by_ref.get(fact.source_ref, ())
        owner = _select_owner(
            fact.source_ref,
            start_offset=marker.start_offset,
            owners=owners,
            depths_by_id=depths_by_id,
        )
        scope_id = _alphabetic_list_scope_id(
            owner,
            structure_nodes_by_id=structure_nodes_by_id,
        )
        if scope_id is None:
            continue
        position = alphabetic_peer_position(
            fact.source_node,
            marker_start_offset=marker.start_offset,
            include_text_indent=True,
        )
        suppression_reasons = _merged_reason_codes(
            _suppression_reasons(
                fact,
                owners=owners,
                source_nodes_by_id=source_nodes_by_id,
            ),
            pdf_table_reasons_by_source_id.get(
                fact.source_ref.source_id,
                (),
            ),
            capture_gap_reasons,
        )
        observation = _AlphabeticListObservation(
            fact=fact,
            scope_id=scope_id,
            marker=marker,
            position=position,
            materialized=any(
                candidate.kind is StructureNodeKind.CLAUSE
                and candidate.list_ordinal == marker.ordinal
                for candidate in owners
            ),
            suppression_reasons=suppression_reasons,
        )
        observations_by_family.setdefault(
            (scope_id, marker.uppercase),
            [],
        ).append(observation)

    proven_runs: list[tuple[_AlphabeticListObservation, ...]] = []
    for observations in observations_by_family.values():
        ordered = tuple(sorted(observations, key=lambda item: item.order_key))
        current: list[_AlphabeticListObservation] = []

        def flush() -> None:
            if len(current) >= 2:
                proven_runs.append(tuple(current))
            current.clear()

        for observation in ordered:
            if observation.ordinal == 1:
                flush()
                current.append(observation)
                continue
            if not current or not _alphabetic_observation_continues_run(
                observation,
                previous=current[-1],
                anchor=current[0],
            ):
                flush()
                continue
            current.append(observation)
        flush()

    candidates: list[StructureInspectionCandidateV1] = []
    for run in sorted(
        proven_runs,
        key=lambda items: items[0].order_key,
    ):
        run_suppression_reasons = _merged_reason_codes(
            *(observation.suppression_reasons for observation in run)
        )
        for observation in run:
            if observation.materialized:
                continue
            page_number, bbox = _fact_location(
                observation.fact,
                start_offset=observation.start_offset,
                end_offset=observation.end_offset,
            )
            disposition = _disposition(run_suppression_reasons)
            reasons = (
                (
                    *run_suppression_reasons,
                    "alphabetic_parenthesized_sequence_detected",
                )
                if run_suppression_reasons
                else ("unconsumed_alphabetic_parenthesized_boundary",)
            )
            candidates.append(
                _make_candidate(
                    category=(
                        StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                    ),
                    source_ref=observation.fact.source_ref,
                    structure_node_id=observation.scope_id,
                    start_offset=observation.start_offset,
                    end_offset=observation.end_offset,
                    page_number=page_number,
                    bbox=bbox,
                    confidence=(
                        StructureInspectionConfidence.HIGH
                        if len(run) >= 3
                        else StructureInspectionConfidence.MEDIUM
                    ),
                    disposition=disposition,
                    safe_marker=f"{observation.letter})",
                    reason_codes=tuple(sorted(set(reasons))),
                )
            )
    return tuple(candidates)


def _embedded_parenthesized_peer_candidates(
    *,
    text_facts: tuple[_TextFact, ...],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
    depths_by_id: dict[str, int],
    source_nodes_by_id: dict[str, SourceNode],
    pdf_table_reasons_by_source_id: dict[str, tuple[str, ...]],
    capture_gap_reasons: tuple[str, ...],
) -> tuple[StructureInspectionCandidateV1, ...]:
    """Expose coherent peer lines hidden inside one immutable source fact."""

    candidates: list[StructureInspectionCandidateV1] = []
    for fact in text_facts:
        if (
            fact.source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
            or fact.source_node.kind
            not in {
                SourceNodeKind.PARAGRAPH,
                SourceNodeKind.LIST,
                SourceNodeKind.LIST_ITEM,
            }
            or not fact.raw_text
        ):
            continue
        sequences = embedded_parenthesized_peer_sequences(fact.raw_text)
        if not sequences:
            continue
        owners = owners_by_ref.get(fact.source_ref, ())
        suppression_reasons = _merged_reason_codes(
            _suppression_reasons(
                fact,
                owners=owners,
                source_nodes_by_id=source_nodes_by_id,
            ),
            pdf_table_reasons_by_source_id.get(
                fact.source_ref.source_id,
                (),
            ),
            capture_gap_reasons,
        )
        for sequence in sequences:
            root_anchored = sequence[0].ordinal == 1
            high_confidence = root_anchored and len(sequence) >= 3
            for marker in sequence:
                owner = _select_owner(
                    fact.source_ref,
                    start_offset=marker.start_offset,
                    owners=owners,
                    depths_by_id=depths_by_id,
                )
                if owner is not None and any(
                    candidate.kind is StructureNodeKind.CLAUSE
                    and candidate.list_ordinal == marker.ordinal
                    and any(
                        span.source_ref == fact.source_ref
                        and span.start_offset <= marker.start_offset < span.end_offset
                        for span in candidate.semantic_source_spans
                    )
                    for candidate in owners
                ):
                    continue
                page_number, bbox = _fact_location(
                    fact,
                    start_offset=marker.start_offset,
                    end_offset=marker.end_offset,
                )
                reason = (
                    "embedded_alphabetic_parenthesized_peer_sequence"
                    if marker.family == "alphabetic_parenthesized"
                    else "embedded_numeric_parenthesized_peer_sequence"
                )
                discovery_reasons = {
                    reason,
                    *(
                        ()
                        if root_anchored
                        else ("embedded_peer_sequence_not_root_anchored",)
                    ),
                    *(
                        ()
                        if len(sequence) >= 3
                        else ("embedded_two_item_peer_sequence",)
                    ),
                }
                reasons = (
                    (*suppression_reasons, *discovery_reasons)
                    if suppression_reasons
                    else tuple(discovery_reasons)
                )
                candidates.append(
                    _make_candidate(
                        category=(
                            StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                        ),
                        source_ref=fact.source_ref,
                        structure_node_id=(
                            owner.structure_node_id if owner is not None else None
                        ),
                        start_offset=marker.start_offset,
                        end_offset=marker.end_offset,
                        page_number=page_number,
                        bbox=bbox,
                        confidence=(
                            StructureInspectionConfidence.HIGH
                            if high_confidence
                            else StructureInspectionConfidence.MEDIUM
                        ),
                        disposition=_disposition(suppression_reasons),
                        safe_marker=marker.safe_marker,
                        reason_codes=tuple(sorted(set(reasons))),
                    )
                )
    return tuple(candidates)


def _alphabetic_list_scope_id(
    owner: StructureNode | None,
    *,
    structure_nodes_by_id: dict[str, StructureNode],
) -> str | None:
    if owner is None:
        return None
    if owner.kind in {
        StructureNodeKind.CLAUSE,
        StructureNodeKind.CONTENT,
        StructureNodeKind.CONTINUATION,
        StructureNodeKind.LIST_GROUP,
    }:
        return owner.parent_structure_node_id or owner.structure_node_id
    if owner.kind is StructureNodeKind.DOCUMENT:
        return owner.structure_node_id
    if owner.structure_node_id not in structure_nodes_by_id:
        return None
    return owner.structure_node_id


def _alphabetic_observation_continues_run(
    current: _AlphabeticListObservation,
    *,
    previous: _AlphabeticListObservation,
    anchor: _AlphabeticListObservation,
) -> bool:
    return evaluate_alphabetic_peer_transition(
        current.marker,
        previous_marker=previous.marker,
        current_position=current.position,
        previous_position=previous.position,
        anchor_position=anchor.position,
        policy=INSPECTOR_ALPHABETIC_PEER_POLICY,
    ).accepted


def _pdf_table_suppression_reasons(
    source_ir: SourceIRSnapshot,
) -> dict[str, tuple[str, ...]]:
    """Reuse the resolver's source-bound PDF table arbitration as negatives."""

    (
        table_level,
        row_level,
        _outline_source_ids,
        ambiguous_source_ids,
    ) = folded_pdf_table_text_nodes(source_ir)
    reasons_by_id: dict[str, set[str]] = {}
    for nodes in table_level.values():
        for node in nodes:
            reasons_by_id.setdefault(node.source_node_id, set()).add(
                "folded_pdf_table_text"
            )
    for selections in row_level.values():
        for selection in selections:
            reasons_by_id.setdefault(
                selection.source_node.source_node_id,
                set(),
            ).add("folded_pdf_table_text")
    for source_node_id in ambiguous_source_ids:
        reasons_by_id.setdefault(source_node_id, set()).add(
            "ambiguous_pdf_table_geometry"
        )
    return {
        source_node_id: tuple(sorted(reasons))
        for source_node_id, reasons in reasons_by_id.items()
    }


def _coherent_single_dot_root_candidates(
    source_ir: SourceIRSnapshot,
    *,
    text_facts: tuple[_TextFact, ...],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
    structure_nodes_by_id: dict[str, StructureNode],
    depths_by_id: dict[str, int],
    source_nodes_by_id: dict[str, SourceNode],
    pdf_table_reasons_by_source_id: dict[str, tuple[str, ...]],
    capture_gap_reasons: tuple[str, ...],
) -> tuple[StructureInspectionCandidateV1, ...]:
    """Surface a root outline only after a source-bound sequence proves it.

    A line-leading ``1.`` is deliberately not an outline boundary by itself:
    it is also the common shape of an ordinary numbered list.  A sequence is
    credible only when non-table roots ``1.`` and ``2.`` share one structure
    scope and the interval between them contains at least two direct ``1.x``
    children.  Further consecutive roots may extend the sequence.

    Table content never establishes the sequence.  Once the non-table anchor
    exists, a later root may extend it only when immutable table topology and
    line geometry prove that the line is inside a full-width merged/title row.
    The result remains detect-only; it does not authorize a tree mutation.
    """

    observations = _single_dot_root_observations(
        source_ir,
        text_facts=text_facts,
        owners_by_ref=owners_by_ref,
        structure_nodes_by_id=structure_nodes_by_id,
        depths_by_id=depths_by_id,
        source_nodes_by_id=source_nodes_by_id,
        pdf_table_reasons_by_source_id=pdf_table_reasons_by_source_id,
        capture_gap_reasons=capture_gap_reasons,
    )
    direct_children = _credible_direct_child_observations(
        text_facts=text_facts,
        owners_by_ref=owners_by_ref,
        structure_nodes_by_id=structure_nodes_by_id,
        depths_by_id=depths_by_id,
        source_nodes_by_id=source_nodes_by_id,
        pdf_table_reasons_by_source_id=pdf_table_reasons_by_source_id,
        capture_gap_reasons=capture_gap_reasons,
    )
    observations_by_scope: dict[str, list[_SingleDotRootObservation]] = {}
    for observation in observations:
        observations_by_scope.setdefault(
            observation.scope_owner.structure_node_id,
            [],
        ).append(observation)

    proven: dict[
        tuple[StructureSourceRef, int],
        _SingleDotRootObservation,
    ] = {}
    for scope_id, scope_observations in observations_by_scope.items():
        ordered = tuple(sorted(scope_observations, key=lambda item: item.order_key))
        starts = tuple(
            observation for observation in ordered if observation.ordinal == 1
        )
        for start_index, root_one in enumerate(starts):
            if root_one.table_boundary_heading:
                continue
            interval_end = (
                starts[start_index + 1].order_key
                if start_index + 1 < len(starts)
                else None
            )
            root_twos = tuple(
                observation
                for observation in ordered
                if observation.ordinal == 2
                and not observation.table_boundary_heading
                and observation.order_key > root_one.order_key
                and (interval_end is None or observation.order_key < interval_end)
            )
            if len(root_twos) != 1:
                continue
            root_two = root_twos[0]
            if any(
                root_one.order_key < observation.order_key < root_two.order_key
                for observation in ordered
                if observation is not root_one and observation is not root_two
            ):
                continue
            nested_child_ordinals = {
                child_ordinal
                for (
                    child_scope_id,
                    child_order_key,
                    child_root_ordinal,
                    child_ordinal,
                ) in direct_children
                if child_scope_id == scope_id
                and child_root_ordinal == 1
                and root_one.order_key < child_order_key < root_two.order_key
            }
            if len(nested_child_ordinals) < 2:
                continue

            run = [root_one, root_two]
            cursor = root_two
            expected_ordinal = 3
            while True:
                remaining = tuple(
                    observation
                    for observation in ordered
                    if observation.order_key > cursor.order_key
                    and (interval_end is None or observation.order_key < interval_end)
                )
                expected = tuple(
                    observation
                    for observation in remaining
                    if observation.ordinal == expected_ordinal
                )
                if len(expected) != 1:
                    break
                next_root = expected[0]
                if any(
                    observation.order_key < next_root.order_key
                    for observation in remaining
                    if observation is not next_root
                ):
                    break
                run.append(next_root)
                cursor = next_root
                expected_ordinal += 1

            for observation in run:
                proven.setdefault(
                    (observation.fact.source_ref, observation.start_offset),
                    observation,
                )

    candidates: list[StructureInspectionCandidateV1] = []
    for observation in sorted(proven.values(), key=lambda item: item.order_key):
        page_number, bbox = _fact_location(
            observation.fact,
            start_offset=observation.start_offset,
            end_offset=observation.end_offset,
        )
        reasons = {"coherent_single_dot_root_sequence"}
        if observation.table_boundary_heading:
            reasons.add("full_width_table_boundary_heading")
        candidates.append(
            _make_candidate(
                category=(
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                ),
                source_ref=observation.fact.source_ref,
                structure_node_id=observation.scope_owner.structure_node_id,
                start_offset=observation.start_offset,
                end_offset=observation.end_offset,
                page_number=page_number,
                bbox=bbox,
                confidence=StructureInspectionConfidence.HIGH,
                disposition=StructureInspectionDisposition.ACTIONABLE,
                safe_marker=str(observation.ordinal),
                reason_codes=tuple(sorted(reasons)),
            )
        )
    return tuple(candidates)


def _single_dot_root_observations(
    source_ir: SourceIRSnapshot,
    *,
    text_facts: tuple[_TextFact, ...],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
    structure_nodes_by_id: dict[str, StructureNode],
    depths_by_id: dict[str, int],
    source_nodes_by_id: dict[str, SourceNode],
    pdf_table_reasons_by_source_id: dict[str, tuple[str, ...]],
    capture_gap_reasons: tuple[str, ...],
) -> tuple[_SingleDotRootObservation, ...]:
    observations: list[_SingleDotRootObservation] = []
    for fact in text_facts:
        if (
            fact.source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
            or not fact.raw_text
        ):
            continue
        owners = owners_by_ref.get(fact.source_ref, ())
        base_suppression_reasons = _merged_reason_codes(
            _suppression_reasons(
                fact,
                owners=owners,
                source_nodes_by_id=source_nodes_by_id,
            ),
            pdf_table_reasons_by_source_id.get(
                fact.source_ref.source_id,
                (),
            ),
            capture_gap_reasons,
        )
        for match in _SINGLE_DOT_ROOT_BOUNDARY_PATTERN.finditer(fact.raw_text):
            start_offset, end_offset = match.span("marker")
            owner = _select_owner(
                fact.source_ref,
                start_offset=start_offset,
                owners=owners,
                depths_by_id=depths_by_id,
            )
            if owner is None:
                continue
            scope_owner = _outermost_structure_scope(
                owner,
                structure_nodes_by_id=structure_nodes_by_id,
            )
            marker = match.group("marker")
            if _is_materialized_boundary(
                category=(
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                ),
                marker=marker,
                start_offset=start_offset,
                fact=fact,
                owners=owners,
            ):
                continue
            full_width_table_heading = _is_full_width_table_boundary_heading(
                source_ir,
                fact=fact,
                match=match,
            )
            table_boundary_heading = False
            if base_suppression_reasons:
                remaining_suppression_reasons = (
                    set(base_suppression_reasons)
                    - _ALLOWED_TABLE_BOUNDARY_SUPPRESSION_REASONS
                )
                if (
                    remaining_suppression_reasons
                    or not set(base_suppression_reasons).intersection(
                        _ALLOWED_TABLE_BOUNDARY_SUPPRESSION_REASONS
                    )
                    or not full_width_table_heading
                ):
                    continue
                table_boundary_heading = True
            elif full_width_table_heading and int(marker) > 2:
                # Resolver arbitration may retain a full-width table fact as
                # outline evidence instead of folding it.  Roots 1/2 still
                # have to prove the namespace through direct 1.x children;
                # later roots are only an extension and stay explicitly
                # identified as table-boundary headings.
                table_boundary_heading = True
            shape_reasons = set(
                _single_dot_root_shape_reasons(
                    fact,
                    match=match,
                )
            )
            if table_boundary_heading:
                shape_reasons.discard("wrapped_paragraph_continuation")
            if shape_reasons:
                continue
            observations.append(
                _SingleDotRootObservation(
                    fact=fact,
                    scope_owner=scope_owner,
                    ordinal=int(marker),
                    start_offset=start_offset,
                    end_offset=end_offset,
                    table_boundary_heading=table_boundary_heading,
                )
            )
    return tuple(observations)


def _credible_direct_child_observations(
    *,
    text_facts: tuple[_TextFact, ...],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
    structure_nodes_by_id: dict[str, StructureNode],
    depths_by_id: dict[str, int],
    source_nodes_by_id: dict[str, SourceNode],
    pdf_table_reasons_by_source_id: dict[str, tuple[str, ...]],
    capture_gap_reasons: tuple[str, ...],
) -> tuple[tuple[str, tuple[int, int, str], int, int], ...]:
    observations: list[tuple[str, tuple[int, int, str], int, int]] = []
    for fact in text_facts:
        if (
            fact.source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
            or not fact.raw_text
        ):
            continue
        owners = owners_by_ref.get(fact.source_ref, ())
        if _merged_reason_codes(
            _suppression_reasons(
                fact,
                owners=owners,
                source_nodes_by_id=source_nodes_by_id,
            ),
            pdf_table_reasons_by_source_id.get(
                fact.source_ref.source_id,
                (),
            ),
            capture_gap_reasons,
        ):
            continue
        for match in _NUMERIC_BOUNDARY_PATTERN.finditer(fact.raw_text):
            parts = tuple(int(part) for part in match.group("marker").split("."))
            if (
                len(parts) != 2
                or parts[1] == 0
                or _numeric_boundary_suppression_reasons(
                    fact,
                    match=match,
                )
                or not _is_materialized_boundary(
                    category=(
                        StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                    ),
                    marker=match.group("marker"),
                    start_offset=match.start("marker"),
                    fact=fact,
                    owners=owners,
                )
            ):
                continue
            owner = _select_owner(
                fact.source_ref,
                start_offset=match.start("marker"),
                owners=owners,
                depths_by_id=depths_by_id,
            )
            if owner is None:
                continue
            scope_owner = _outermost_structure_scope(
                owner,
                structure_nodes_by_id=structure_nodes_by_id,
            )
            observations.append(
                (
                    scope_owner.structure_node_id,
                    (
                        fact.source_order,
                        match.start("marker"),
                        fact.source_ref.source_id,
                    ),
                    parts[0],
                    parts[1],
                )
            )
    return tuple(observations)


def _single_dot_root_shape_reasons(
    fact: _TextFact,
    *,
    match: re.Match[str],
) -> tuple[str, ...]:
    title = " ".join(match.group("tail").lstrip(" \t\u3000、)）:：-—").split())
    reasons: set[str] = set()
    if (
        not title
        or len(title) > _SINGLE_DOT_ROOT_TITLE_MAX_CHARACTERS
        or title[0] in ".．+-±≤≥<>"
    ):
        reasons.add("not_credible_root_heading_shape")
    elif _SINGLE_DOT_MEASUREMENT_PREFIX.match(title) is not None:
        reasons.add("numeric_scale_or_measurement")
    if _SINGLE_DOT_REFERENCE_PREFIX.match(title) is not None:
        reasons.add("reference_not_outline_boundary")
    if _ENGLISH_CONTINUATION_PREFIX.match(title) is not None:
        reasons.add("english_sentence_continuation")
    line_start = fact.raw_text.rfind("\n", 0, match.start()) + 1
    if line_start > 0 and not _previous_line_ends_sentence(
        fact.raw_text,
        before_offset=line_start,
    ):
        reasons.add("wrapped_paragraph_continuation")
    return tuple(sorted(reasons))


def _outermost_structure_scope(
    owner: StructureNode,
    *,
    structure_nodes_by_id: dict[str, StructureNode],
) -> StructureNode:
    current = owner
    visited: set[str] = set()
    while current.structure_node_id not in visited:
        visited.add(current.structure_node_id)
        parent_id = current.parent_structure_node_id
        if parent_id is None:
            return current
        parent = structure_nodes_by_id.get(parent_id)
        if parent is None or parent.kind is StructureNodeKind.DOCUMENT:
            return current
        current = parent
    return current


def _is_full_width_table_boundary_heading(
    source_ir: SourceIRSnapshot,
    *,
    fact: _TextFact,
    match: re.Match[str],
) -> bool:
    page_number = fact.native_locator.page_number
    if (
        fact.native_locator.format is not NativeDocumentFormat.PDF
        or page_number is None
    ):
        return False
    overlapping_lines = tuple(
        line
        for line in fact.pdf_text_lines
        if line.start_offset <= match.start("marker") < line.end_offset
    )
    if len(overlapping_lines) != 1:
        return False
    line_bbox = overlapping_lines[0].bbox
    line_text = " ".join(match.group(0).strip().split())
    line_center_y = (line_bbox[1] + line_bbox[3]) / 2.0

    for table_node in source_ir.nodes:
        table = table_node.table_payload
        table_bbox = table_node.native_locator.bbox
        if (
            table is None
            or table.column_count < 2
            or table_bbox is None
            or table_node.native_locator.format is not NativeDocumentFormat.PDF
            or table_node.native_locator.page_number != page_number
        ):
            continue
        table_width = table_bbox[2] - table_bbox[0]
        if table_width <= 0:
            continue
        for cell in table.cells:
            cell_bbox = cell.native_locator.bbox
            if (
                cell_bbox is None
                or cell.column != 0
                or cell.column_span < table.column_count
                or (cell_bbox[2] - cell_bbox[0]) / table_width
                < _FULL_WIDTH_TABLE_CELL_MIN_WIDTH_RATIO
                or abs(cell_bbox[0] - table_bbox[0])
                > _FULL_WIDTH_TABLE_EDGE_TOLERANCE_POINTS
                or abs(cell_bbox[2] - table_bbox[2])
                > _FULL_WIDTH_TABLE_EDGE_TOLERANCE_POINTS
                or not cell_bbox[1] <= line_center_y <= cell_bbox[3]
                or abs(line_bbox[0] - cell_bbox[0])
                > _FULL_WIDTH_TABLE_EDGE_TOLERANCE_POINTS
            ):
                continue
            cell_lines = {
                " ".join(line.strip().split())
                for line in cell.raw_text.splitlines()
                if line.strip()
            }
            if line_text in cell_lines:
                return True
    return False


def _numeric_boundary_suppression_reasons(
    fact: _TextFact,
    *,
    match: re.Match[str],
) -> tuple[str, ...]:
    """Reject numeric values and scales that only resemble dotted headings."""

    line_start = fact.raw_text.rfind("\n", 0, match.start()) + 1
    line_end = fact.raw_text.find("\n", match.end())
    if line_end < 0:
        line_end = len(fact.raw_text)
    line_text = fact.raw_text[line_start:line_end]
    marker = match.group("marker")
    native_heading = fact.source_node.kind is SourceNodeKind.HEADING and line_start == 0
    reasons: set[str] = set()
    if (
        explicit_section_label_from_text(
            line_text,
            native_heading=native_heading,
        )
        != marker
    ):
        reasons.add("not_credible_outline_shape")
    if (
        not native_heading
        and NUMERIC_VALUE_LINE_PATTERN.fullmatch(line_text) is not None
    ):
        reasons.add("numeric_scale_or_measurement")
    if (
        marker.count(".") == 1
        and line_start > 0
        and not _previous_line_ends_sentence(
            fact.raw_text,
            before_offset=line_start,
        )
    ):
        reasons.add("wrapped_paragraph_continuation")
    tail = line_text[match.end() - line_start :].lstrip(" \t\u3000、.)）:：-—")
    if _ENGLISH_CONTINUATION_PREFIX.match(tail) is not None:
        reasons.add("english_sentence_continuation")
    if _has_inline_numeric_peer(
        fact.raw_text,
        marker=marker,
        marker_end_offset=match.end(),
    ):
        reasons.add("inline_numeric_peer_sequence")
    return tuple(sorted(reasons))


def _previous_line_ends_sentence(
    raw_text: str,
    *,
    before_offset: int,
) -> bool:
    prefix = raw_text[:before_offset].rstrip()
    if not prefix:
        return True
    previous_line = prefix.rsplit("\n", 1)[-1]
    return _SENTENCE_TERMINATOR.search(previous_line) is not None


def _has_inline_numeric_peer(
    raw_text: str,
    *,
    marker: str,
    marker_end_offset: int,
) -> bool:
    parts = tuple(int(part) for part in marker.split("."))
    if not parts or parts[-1] != 0:
        return False
    for peer in _INLINE_DOTTED_NUMBER_PATTERN.finditer(
        raw_text,
        marker_end_offset,
    ):
        peer_parts = tuple(int(part) for part in peer.group("marker").split("."))
        if (
            len(peer_parts) == len(parts)
            and peer_parts[1:] == parts[1:]
            and peer_parts[0] == parts[0] + 1
        ):
            line_start = raw_text.rfind("\n", 0, peer.start()) + 1
            if raw_text[line_start : peer.start()].strip():
                return True
    return False


def _merged_reason_codes(
    *groups: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(sorted({reason for group in groups for reason in group}))


def _suppression_reasons(
    fact: _TextFact,
    *,
    owners: tuple[StructureNode, ...],
    source_nodes_by_id: dict[str, SourceNode],
) -> tuple[str, ...]:
    kinds: set[SourceNodeKind] = set()
    current: SourceNode | None = fact.source_node
    visited: set[str] = set()
    while current is not None and current.source_node_id not in visited:
        visited.add(current.source_node_id)
        kinds.add(current.kind)
        parent_id = current.parent_source_node_id
        current = source_nodes_by_id.get(parent_id) if parent_id is not None else None

    reasons: set[str] = set()
    if (
        fact.source_ref.kind is StructureSourceRefKind.TABLE_CELL
        or kinds.intersection(
            {
                SourceNodeKind.TABLE,
                SourceNodeKind.TABLE_ROW,
                SourceNodeKind.CELL,
            }
        )
        or any(
            owner.kind in {StructureNodeKind.TABLE, StructureNodeKind.TABLE_ROW}
            for owner in owners
        )
    ):
        reasons.add("inside_table_content")
    if SourceNodeKind.TOC_ENTRY in kinds or any(
        owner.boundary_basis is StructureBoundaryBasis.TOC_BLOCK for owner in owners
    ):
        reasons.add("inside_table_of_contents")
    if kinds.intersection(
        {
            SourceNodeKind.HEADER,
            SourceNodeKind.FOOTER,
            SourceNodeKind.WATERMARK,
        }
    ) or any(
        owner.boundary_basis is StructureBoundaryBasis.PAGE_FURNITURE
        for owner in owners
    ):
        reasons.add("inside_page_furniture")
    if kinds.intersection(
        {
            SourceNodeKind.IMAGE,
            SourceNodeKind.FIGURE,
        }
    ):
        reasons.add("inside_figure_content")
    return tuple(sorted(reasons))


def _represented_structure_boundary_owner(
    fact: _TextFact,
    *,
    start_offset: int,
    owners: tuple[StructureNode, ...],
    depths_by_id: dict[str, int],
) -> StructureNode | None:
    """Return a section whose own boundary fact already represents this marker.

    A resolver may preserve the document's canonical numeric label while the
    same source fact visibly carries an appendix alias.  The alias is not a
    second missing boundary when it starts the section's own immutable source
    fact (or the section's exact semantic span).
    """

    candidates: list[StructureNode] = []
    for owner in owners:
        if (
            owner.kind is not StructureNodeKind.SECTION
            or fact.source_ref not in owner.source_refs
            or not owner.source_refs
            or owner.source_refs[0] != fact.source_ref
        ):
            continue
        spans = tuple(
            span
            for span in owner.semantic_source_spans
            if span.source_ref == fact.source_ref
        )
        if spans:
            starts_boundary = any(
                span.start_offset <= start_offset < span.end_offset
                and not fact.raw_text[span.start_offset : start_offset].strip()
                for span in spans
            )
        else:
            starts_boundary = start_offset == _first_non_whitespace_offset(
                fact.raw_text
            ) or _marker_follows_only_owner_explicit_label(
                fact.raw_text,
                marker_start_offset=start_offset,
                owner_explicit_label=owner.explicit_label,
            )
        if starts_boundary:
            candidates.append(owner)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda owner: (
            depths_by_id.get(owner.structure_node_id, 0),
            -owner.source_order,
            owner.structure_node_id,
        ),
    )


def _marker_follows_only_owner_explicit_label(
    raw_text: str,
    *,
    marker_start_offset: int,
    owner_explicit_label: str | None,
) -> bool:
    if owner_explicit_label is None:
        return False
    prefix = "".join(raw_text[:marker_start_offset].replace("．", ".").split())
    return prefix in {
        owner_explicit_label,
        f"{owner_explicit_label}.",
        f"{owner_explicit_label}、",
        f"{owner_explicit_label})",
        f"{owner_explicit_label}）",
        f"{owner_explicit_label}:",
        f"{owner_explicit_label}：",
    }


def _structure_node_is_descendant_or_self(
    structure_node_id: str,
    *,
    ancestor_structure_node_id: str,
    structure_nodes_by_id: dict[str, StructureNode],
) -> bool:
    current_id: str | None = structure_node_id
    visited: set[str] = set()
    while current_id is not None and current_id not in visited:
        if current_id == ancestor_structure_node_id:
            return True
        visited.add(current_id)
        current = structure_nodes_by_id.get(current_id)
        current_id = current.parent_structure_node_id if current is not None else None
    return False


def _is_materialized_boundary(
    *,
    category: StructureInspectionCandidateCategory,
    marker: str,
    start_offset: int,
    fact: _TextFact,
    owners: tuple[StructureNode, ...],
) -> bool:
    first_text_offset = _first_non_whitespace_offset(fact.raw_text)
    for owner in owners:
        if owner.kind is not StructureNodeKind.SECTION:
            continue
        spans = tuple(
            span
            for span in owner.semantic_source_spans
            if span.source_ref == fact.source_ref
        )
        starts_here = (
            any(
                span.start_offset <= start_offset
                and start_offset < span.end_offset
                and not fact.raw_text[span.start_offset : start_offset].strip()
                for span in spans
            )
            if spans
            else start_offset == first_text_offset
        )
        if not starts_here:
            continue
        if (
            category is StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
            and owner.explicit_label == marker
        ):
            return True
        if (
            category is StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY
            and owner.boundary_basis
            in {
                StructureBoundaryBasis.NATIVE_HEADING,
                StructureBoundaryBasis.MODEL_SUGGESTION,
            }
        ):
            return True
    return False


def _select_owner(
    source_ref: StructureSourceRef,
    *,
    start_offset: int,
    owners: tuple[StructureNode, ...],
    depths_by_id: dict[str, int],
) -> StructureNode | None:
    if not owners:
        return None
    containing: list[StructureNode] = []
    for owner in owners:
        spans = tuple(
            span
            for span in owner.semantic_source_spans
            if span.source_ref == source_ref
        )
        if spans and not any(
            span.start_offset <= start_offset < span.end_offset for span in spans
        ):
            continue
        containing.append(owner)
    candidates = containing or list(owners)
    return max(
        candidates,
        key=lambda owner: (
            depths_by_id.get(owner.structure_node_id, 0),
            owner.kind is StructureNodeKind.CONTENT,
            -owner.source_order,
            owner.structure_node_id,
        ),
    )


def _first_non_whitespace_offset(value: str) -> int:
    match = re.search(r"\S", value)
    return match.start() if match is not None else len(value)


def _fact_location(
    fact: _TextFact,
    *,
    start_offset: int,
    end_offset: int,
) -> tuple[int | None, tuple[float, float, float, float] | None]:
    locator = fact.native_locator
    page_number = (
        locator.page_number if locator.format is NativeDocumentFormat.PDF else None
    )
    overlapping_lines = tuple(
        line
        for line in fact.pdf_text_lines
        if line.start_offset < end_offset and line.end_offset > start_offset
    )
    if overlapping_lines:
        return page_number, _bbox_union(tuple(line.bbox for line in overlapping_lines))
    if locator.bbox is not None:
        return page_number, locator.bbox
    parent_locator = fact.source_node.native_locator
    if (
        parent_locator.format is NativeDocumentFormat.PDF
        and parent_locator.bbox is not None
    ):
        return parent_locator.page_number, parent_locator.bbox
    return page_number, None


def _bbox_union(
    boxes: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _metric_drafts(
    structure_tree: StructureTreeSnapshot,
    *,
    facts_by_ref: dict[StructureSourceRef, _TextFact],
    source_nodes_by_id: dict[str, SourceNode],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
) -> tuple[_MetricDraft, ...]:
    child_counts: dict[str, int] = {}
    for node in structure_tree.nodes:
        if node.parent_structure_node_id is not None:
            child_counts[node.parent_structure_node_id] = (
                child_counts.get(node.parent_structure_node_id, 0) + 1
            )

    base_measurements: list[
        tuple[
            StructureNode,
            tuple[StructureSourceRef, ...],
            int,
            int,
            int | None,
            int | None,
            bool,
        ]
    ] = []
    eligible_lengths: list[int] = []
    for node in structure_tree.nodes:
        source_refs = node.effective_semantic_source_refs
        char_count, line_count = _node_text_size(
            node,
            facts_by_ref=facts_by_ref,
        )
        page_numbers = tuple(
            page_number
            for source_ref in source_refs
            if (fact := facts_by_ref.get(source_ref)) is not None
            if (
                page_number := (
                    fact.native_locator.page_number
                    if fact.native_locator.format is NativeDocumentFormat.PDF
                    else None
                )
            )
            is not None
        )
        suppressed = _node_is_suppressed(
            node,
            source_refs=source_refs,
            facts_by_ref=facts_by_ref,
            source_nodes_by_id=source_nodes_by_id,
            owners_by_ref=owners_by_ref,
        )
        eligible = (
            node.kind in _METRIC_NODE_KINDS
            and node.boundary_basis
            not in {
                StructureBoundaryBasis.TOC_BLOCK,
                StructureBoundaryBasis.PAGE_FURNITURE,
            }
            and not suppressed
            and char_count > 0
        )
        if eligible:
            eligible_lengths.append(char_count)
        base_measurements.append(
            (
                node,
                source_refs,
                char_count,
                line_count,
                min(page_numbers) if page_numbers else None,
                max(page_numbers) if page_numbers else None,
                eligible,
            )
        )

    outlier_threshold = _length_outlier_threshold(eligible_lengths)
    return tuple(
        _MetricDraft(
            node=node,
            source_refs=source_refs,
            own_text_char_count=char_count,
            own_text_line_count=line_count,
            page_start=page_start,
            page_end=page_end,
            direct_child_count=child_counts.get(node.structure_node_id, 0),
            length_outlier=(
                eligible
                and outlier_threshold is not None
                and char_count >= outlier_threshold
            ),
        )
        for (
            node,
            source_refs,
            char_count,
            line_count,
            page_start,
            page_end,
            eligible,
        ) in base_measurements
    )


def _node_text_size(
    node: StructureNode,
    *,
    facts_by_ref: dict[StructureSourceRef, _TextFact],
) -> tuple[int, int]:
    spans_by_ref = {span.source_ref: span for span in node.semantic_source_spans}
    char_count = 0
    line_count = 0
    for source_ref in node.effective_semantic_source_refs:
        fact = facts_by_ref.get(source_ref)
        if fact is None or not fact.raw_text:
            continue
        span = spans_by_ref.get(source_ref)
        if span is None:
            text = fact.raw_text
        else:
            start = min(span.start_offset, len(fact.raw_text))
            end = min(span.end_offset, len(fact.raw_text))
            text = fact.raw_text[start:end]
        char_count += len(text)
        if text:
            line_count += text.count("\n") + 1
    return char_count, line_count


def _node_is_suppressed(
    node: StructureNode,
    *,
    source_refs: tuple[StructureSourceRef, ...],
    facts_by_ref: dict[StructureSourceRef, _TextFact],
    source_nodes_by_id: dict[str, SourceNode],
    owners_by_ref: dict[StructureSourceRef, tuple[StructureNode, ...]],
) -> bool:
    facts = tuple(
        fact
        for source_ref in source_refs
        if (fact := facts_by_ref.get(source_ref)) is not None
    )
    return bool(facts) and all(
        _suppression_reasons(
            fact,
            owners=owners_by_ref.get(fact.source_ref, (node,)),
            source_nodes_by_id=source_nodes_by_id,
        )
        for fact in facts
    )


def _length_outlier_threshold(lengths: list[int]) -> int | None:
    if len(lengths) < 2:
        return None
    ordered = sorted(lengths)
    lower_half = ordered[: max(1, (len(ordered) + 1) // 2)]
    robust_baseline = statistics.median(lower_half)
    return max(
        _MIN_LONG_SCOPE_CHAR_COUNT,
        math.ceil(robust_baseline * _ROBUST_BASELINE_MULTIPLIER),
    )


def _materialize_metrics(
    drafts: tuple[_MetricDraft, ...],
    *,
    candidates: tuple[StructureInspectionCandidateV1, ...],
) -> tuple[StructureInspectionNodeMetricV1, ...]:
    candidates_by_node: dict[str, list[StructureInspectionCandidateV1]] = {}
    for candidate in candidates:
        if candidate.structure_node_id is not None:
            candidates_by_node.setdefault(candidate.structure_node_id, []).append(
                candidate
            )
    metrics: list[StructureInspectionNodeMetricV1] = []
    for draft in drafts:
        node_candidates = candidates_by_node.get(
            draft.node.structure_node_id,
            (),
        )
        metrics.append(
            StructureInspectionNodeMetricV1(
                structure_node_id=draft.node.structure_node_id,
                kind=draft.node.kind,
                source_refs=draft.source_refs,
                own_text_char_count=draft.own_text_char_count,
                own_text_line_count=draft.own_text_line_count,
                source_ref_count=len(draft.source_refs),
                page_start=draft.page_start,
                page_end=draft.page_end,
                direct_child_count=draft.direct_child_count,
                length_outlier=draft.length_outlier,
                unconsumed_strong_boundary_count=sum(
                    candidate.category
                    in {
                        StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
                        StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
                    }
                    and candidate.disposition
                    is StructureInspectionDisposition.ACTIONABLE
                    for candidate in node_candidates
                ),
                actionable_candidate_count=sum(
                    candidate.disposition is StructureInspectionDisposition.ACTIONABLE
                    for candidate in node_candidates
                ),
                suppressed_candidate_count=sum(
                    candidate.disposition is StructureInspectionDisposition.SUPPRESSED
                    for candidate in node_candidates
                ),
            )
        )
    return tuple(metrics)


def _disposition(
    suppression_reasons: tuple[str, ...],
) -> StructureInspectionDisposition:
    return (
        StructureInspectionDisposition.SUPPRESSED
        if suppression_reasons
        else StructureInspectionDisposition.ACTIONABLE
    )


def _make_candidate(
    *,
    category: StructureInspectionCandidateCategory,
    source_ref: StructureSourceRef | None,
    structure_node_id: str | None,
    start_offset: int,
    end_offset: int,
    page_number: int | None,
    bbox: tuple[float, float, float, float] | None,
    confidence: StructureInspectionConfidence,
    disposition: StructureInspectionDisposition,
    safe_marker: str | None,
    reason_codes: tuple[str, ...],
) -> StructureInspectionCandidateV1:
    candidate_id = _stable_candidate_id(
        category=category,
        source_ref=source_ref,
        structure_node_id=structure_node_id,
        start_offset=start_offset,
        end_offset=end_offset,
        confidence=confidence,
        disposition=disposition,
        safe_marker=safe_marker,
        reason_codes=reason_codes,
    )
    return StructureInspectionCandidateV1(
        candidate_id=candidate_id,
        category=category,
        source_ref=source_ref,
        structure_node_id=structure_node_id,
        start_offset=start_offset,
        end_offset=end_offset,
        page_number=page_number,
        bbox=bbox,
        confidence=confidence,
        disposition=disposition,
        safe_marker=safe_marker,
        reason_codes=reason_codes,
    )


def _stable_candidate_id(
    *,
    category: StructureInspectionCandidateCategory,
    source_ref: StructureSourceRef | None,
    structure_node_id: str | None,
    start_offset: int,
    end_offset: int,
    confidence: StructureInspectionConfidence,
    disposition: StructureInspectionDisposition,
    safe_marker: str | None,
    reason_codes: tuple[str, ...],
) -> str:
    payload = (
        category.value,
        (source_ref.model_dump(mode="json") if source_ref is not None else None),
        structure_node_id,
        start_offset,
        end_offset,
        confidence.value,
        disposition.value,
        safe_marker,
        reason_codes,
    )
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"structure-inspection-candidate-{digest[:32]}"


def _inspection_canonical_sha256(
    *,
    source_ir_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    resolver_id: str,
    resolver_version: str,
    inspection_runtime_version: str = _LEGACY_INSPECTION_IMPLEMENTATION_VERSION,
    boundary_ruleset_version: str = _LEGACY_INSPECTION_IMPLEMENTATION_VERSION,
    visual_text_snapshot_ids: tuple[str, ...] = (),
    inspected_visual_observation_count: int = 0,
    inspected_source_node_count: int,
    grounded_source_node_count: int,
    candidates: tuple[StructureInspectionCandidateV1, ...],
    node_metrics: tuple[StructureInspectionNodeMetricV1, ...],
) -> str:
    payload = {
        "schema_version": STRUCTURE_INSPECTION_SCHEMA_VERSION,
        "source_ir_id": source_ir_id,
        "source_sha256": source_sha256,
        "structure_snapshot_id": structure_snapshot_id,
        "resolver_id": resolver_id,
        "resolver_version": resolver_version,
        "inspected_source_node_count": inspected_source_node_count,
        "grounded_source_node_count": grounded_source_node_count,
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "node_metrics": [metric.model_dump(mode="json") for metric in node_metrics],
    }
    if (
        inspection_runtime_version != _LEGACY_INSPECTION_IMPLEMENTATION_VERSION
        or boundary_ruleset_version != _LEGACY_INSPECTION_IMPLEMENTATION_VERSION
    ):
        payload["inspection_runtime_version"] = inspection_runtime_version
        payload["boundary_ruleset_version"] = boundary_ruleset_version
    if visual_text_snapshot_ids or inspected_visual_observation_count:
        payload["visual_text_snapshot_ids"] = visual_text_snapshot_ids
        payload["inspected_visual_observation_count"] = (
            inspected_visual_observation_count
        )
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "STRUCTURE_INSPECTION_SCHEMA_VERSION",
    "STRUCTURE_INSPECTION_RUNTIME_VERSION",
    "STRUCTURE_INSPECTION_REASON_CODES",
    "StructureInspectionCandidateCategory",
    "StructureInspectionCandidateV1",
    "StructureInspectionConfidence",
    "StructureInspectionDisposition",
    "StructureInspectionNodeMetricV1",
    "StructureInspectionSnapshotV1",
    "inspect_structure",
]
