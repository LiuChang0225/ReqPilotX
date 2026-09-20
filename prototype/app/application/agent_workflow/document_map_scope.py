from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCompilationV1,
    DocumentMapVisualCoverageReceiptV1,
)
from app.application.agent_workflow.native_heading_skeleton import (
    NativeHeadingCandidateState,
    NativeHeadingCandidateV1,
    NativeHeadingSkeletonArtifactV1,
)
from app.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind


DOCUMENT_MAP_SCOPE_REQUEST_SCHEMA_VERSION = "document-map-scope-request/v1"
DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_SCHEMA_VERSION = (
    "document-map-heading-scope-candidate/v1"
)
DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_V2_SCHEMA_VERSION = (
    "document-map-heading-scope-candidate/v2"
)
DOCUMENT_MAP_HEADING_SCOPE_COMPILATION_SCHEMA_VERSION = (
    "document-map-heading-scope-compilation/v1"
)
_SCOPE_EVIDENCE_REF_PATTERN = re.compile(
    r"^(?:page:[1-9][0-9]{0,2}|node-[0-9a-f]{32}|"
    r"table-[0-9a-f]{32}|asset-[0-9a-f]{32})$"
)
DocumentMapScopeEvidenceRef = Annotated[
    str,
    Field(pattern=_SCOPE_EVIDENCE_REF_PATTERN.pattern),
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class DocumentMapDetailScopeKind(StrEnum):
    CHAPTER = "CHAPTER"
    SECTION_GROUP = "SECTION_GROUP"
    FRONT_MATTER = "FRONT_MATTER"


class DocumentMapHeadingDisposition(StrEnum):
    HEADING = "HEADING"
    NOT_HEADING = "NOT_HEADING"


class DocumentMapHeadingScopeAction(StrEnum):
    ACCEPT_SCOPE = "ACCEPT_SCOPE"
    COORDINATE_WITH_PREVIOUS = "COORDINATE_WITH_PREVIOUS"
    COORDINATE_WITH_NEXT = "COORDINATE_WITH_NEXT"


class DocumentMapHeadingScopeActionV2(StrEnum):
    ACCEPT_SCOPE = "ACCEPT_SCOPE"
    COORDINATE_WITH_PREVIOUS = "COORDINATE_WITH_PREVIOUS"
    COORDINATE_WITH_NEXT = "COORDINATE_WITH_NEXT"
    EXPAND_HEADING_CANDIDATES = "EXPAND_HEADING_CANDIDATES"


class DocumentMapSourceEndpointV1(_StrictFrozenModel):
    source_node_id: str = Field(pattern=r"^node-[0-9a-f]{32}$")
    offset: int = Field(ge=0)


class DocumentMapSourceSpanV1(_StrictFrozenModel):
    start: DocumentMapSourceEndpointV1
    end: DocumentMapSourceEndpointV1


class DocumentMapHeadingCandidateRefV1(_StrictFrozenModel):
    candidate_id: str = Field(pattern=r"^heading-candidate-[0-9a-f]{32}$")
    source_node_id: str = Field(pattern=r"^node-[0-9a-f]{32}$")
    source_order: int = Field(ge=0)
    page_number: int = Field(ge=1, le=250)
    number: str | None = Field(default=None, min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    level: int = Field(ge=1, le=6)
    advisory_parent_candidate_id: str | None = Field(
        default=None,
        pattern=r"^heading-candidate-[0-9a-f]{32}$",
    )
    advisory_state: NativeHeadingCandidateState


class DocumentMapScopeRequestV1(_StrictFrozenModel):
    schema_version: Literal["document-map-scope-request/v1"] = (
        DOCUMENT_MAP_SCOPE_REQUEST_SCHEMA_VERSION
    )
    private_stage_input_only: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_workspace_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    survey_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_coverage_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_skeleton_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    kind: DocumentMapDetailScopeKind
    advisory_number: str | None = Field(
        default=None,
        pattern=r"^[0-9]+(?:\.[0-9]+){0,5}$",
    )
    advisory_title: str = Field(min_length=1, max_length=300)
    suggested_source_span: DocumentMapSourceSpanV1
    source_node_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    page_numbers: tuple[int, ...] = Field(min_length=1, max_length=250)
    physical_table_ids: tuple[str, ...] = Field(default=(), max_length=4096)
    asset_ids: tuple[str, ...] = Field(default=(), max_length=4096)
    heading_candidates: tuple[DocumentMapHeadingCandidateRefV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    preceding_context_source_node_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8,
    )
    following_context_source_node_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8,
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request(self) -> DocumentMapScopeRequestV1:
        for values, subject in (
            (self.source_node_ids, "source nodes"),
            (self.page_numbers, "pages"),
            (self.physical_table_ids, "tables"),
            (self.asset_ids, "assets"),
            (self.preceding_context_source_node_ids, "preceding context"),
            (self.following_context_source_node_ids, "following context"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"document-map scope {subject} must be unique")
        if self.page_numbers != tuple(sorted(self.page_numbers)):
            raise ValueError("document-map scope pages must use numeric order")
        candidate_ids = tuple(item.candidate_id for item in self.heading_candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("document-map scope heading candidates must be unique")
        if tuple(item.source_order for item in self.heading_candidates) != tuple(
            sorted(item.source_order for item in self.heading_candidates)
        ):
            raise ValueError(
                "document-map scope heading candidates must use source order"
            )
        if not set(item.source_node_id for item in self.heading_candidates).issubset(
            self.source_node_ids
        ):
            raise ValueError(
                "document-map scope heading candidate leaves source domain"
            )
        if set(self.preceding_context_source_node_ids).intersection(
            self.source_node_ids
        ) or set(self.following_context_source_node_ids).intersection(
            self.source_node_ids
        ):
            raise ValueError("document-map scope context overlaps owned source")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"request_sha256"})
        )
        if self.request_sha256 != expected:
            raise ValueError("document-map scope request SHA drifted")
        return self


class DocumentMapHeadingDecisionV1(_StrictFrozenModel):
    candidate_id: str = Field(pattern=r"^heading-candidate-[0-9a-f]{32}$")
    disposition: DocumentMapHeadingDisposition
    parent_candidate_id: str | None = Field(
        default=None,
        pattern=r"^heading-candidate-[0-9a-f]{32}$",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    public_summary: str = Field(min_length=1, max_length=600)

    @field_validator("public_summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("heading decision summary must be plain text")
        return normalized

    @model_validator(mode="after")
    def validate_parent_shape(self) -> DocumentMapHeadingDecisionV1:
        if (
            self.disposition is DocumentMapHeadingDisposition.NOT_HEADING
            and self.parent_candidate_id is not None
        ):
            raise ValueError("non-heading candidate cannot have a heading parent")
        if self.parent_candidate_id == self.candidate_id:
            raise ValueError("heading candidate cannot own itself")
        return self


class DocumentMapHeadingScopeFindingV1(_StrictFrozenModel):
    finding_code: str = Field(min_length=1, max_length=120)
    evidence_refs: tuple[DocumentMapScopeEvidenceRef, ...] = Field(
        min_length=1,
        max_length=250,
    )
    public_summary: str = Field(min_length=1, max_length=1000)

    @field_validator("finding_code", mode="before")
    @classmethod
    def normalize_code(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("heading scope finding code must be text")
        normalized = value.strip().casefold()
        if re.fullmatch(r"[a-z][a-z0-9_]{0,119}", normalized) is None:
            raise ValueError("heading scope finding code must be snake_case")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def validate_page_order(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        pages = tuple(
            int(item.split(":", 1)[1]) for item in value if item.startswith("page:")
        )
        if len(value) != len(set(value)) or pages != tuple(sorted(set(pages))):
            raise ValueError(
                "heading scope finding evidence must be unique with numeric page order"
            )
        return value


class DocumentMapHeadingScopeCandidateV1(_StrictFrozenModel):
    schema_version: Literal["document-map-heading-scope-candidate/v1"] = (
        DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_SCHEMA_VERSION
    )
    request_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: DocumentMapHeadingScopeAction
    accepted_source_span: DocumentMapSourceSpanV1
    heading_decisions: tuple[DocumentMapHeadingDecisionV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    findings: tuple[DocumentMapHeadingScopeFindingV1, ...] = Field(
        default=(),
        max_length=256,
    )
    public_summary: str = Field(min_length=1, max_length=1500)

    @field_validator("public_summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("heading scope summary must be plain text")
        return normalized

    @model_validator(mode="after")
    def validate_action_shape(self) -> DocumentMapHeadingScopeCandidateV1:
        if (
            self.action is not DocumentMapHeadingScopeAction.ACCEPT_SCOPE
            and self.heading_decisions
        ):
            raise ValueError("coordination request cannot publish heading decisions")
        return self


class DocumentMapHeadingExpansionProposalV2(_StrictFrozenModel):
    source_node_id: str = Field(pattern=r"^node-[0-9a-f]{32}$")
    number: str | None = Field(default=None, min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    level: int = Field(ge=2, le=6)
    parent_source_node_id: str = Field(pattern=r"^node-[0-9a-f]{32}$")
    confidence: float = Field(ge=0.0, le=1.0)
    public_summary: str = Field(min_length=1, max_length=600)

    @field_validator("number", "title", "public_summary")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("heading expansion text must be plain text")
        return normalized


class DocumentMapHeadingScopeCandidateV2(_StrictFrozenModel):
    schema_version: Literal["document-map-heading-scope-candidate/v2"] = (
        DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_V2_SCHEMA_VERSION
    )
    request_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: DocumentMapHeadingScopeActionV2
    accepted_source_span: DocumentMapSourceSpanV1
    heading_decisions: tuple[DocumentMapHeadingDecisionV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    heading_expansion_proposals: tuple[DocumentMapHeadingExpansionProposalV2, ...] = (
        Field(default=(), max_length=256)
    )
    findings: tuple[DocumentMapHeadingScopeFindingV1, ...] = Field(
        default=(),
        max_length=256,
    )
    public_summary: str = Field(min_length=1, max_length=1500)

    @field_validator("public_summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("heading scope summary must be plain text")
        return normalized

    @model_validator(mode="after")
    def validate_action_shape(self) -> DocumentMapHeadingScopeCandidateV2:
        expanding = (
            self.action is DocumentMapHeadingScopeActionV2.EXPAND_HEADING_CANDIDATES
        )
        if expanding != bool(self.heading_expansion_proposals):
            raise ValueError("heading expansion proposals require the expansion action")
        if self.action is not DocumentMapHeadingScopeActionV2.ACCEPT_SCOPE and (
            self.heading_decisions
        ):
            raise ValueError("coordination request cannot publish heading decisions")
        proposal_ids = tuple(
            item.source_node_id for item in self.heading_expansion_proposals
        )
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("heading expansion source nodes must be unique")
        return self


class DocumentMapHeadingNodeV1(_StrictFrozenModel):
    stable_node_id: str = Field(pattern=r"^document-map-node-[0-9a-f]{32}$")
    candidate_id: str = Field(pattern=r"^heading-candidate-[0-9a-f]{32}$")
    source_node_id: str = Field(pattern=r"^node-[0-9a-f]{32}$")
    source_order: int = Field(ge=0)
    page_number: int = Field(ge=1, le=250)
    number: str | None = Field(default=None, min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    level: int = Field(ge=1, le=6)
    parent_stable_node_id: str | None = Field(
        default=None,
        pattern=r"^document-map-node-[0-9a-f]{32}$",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    public_summary: str = Field(min_length=1, max_length=600)


class DocumentMapHeadingScopeCompilationV1(_StrictFrozenModel):
    schema_version: Literal["document-map-heading-scope-compilation/v1"] = (
        DOCUMENT_MAP_HEADING_SCOPE_COMPILATION_SCHEMA_VERSION
    )
    private_checkpoint_only: Literal[True] = True
    document_map_complete: Literal[False] = False
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    survey_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_source_span: DocumentMapSourceSpanV1
    heading_nodes: tuple[DocumentMapHeadingNodeV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    non_heading_candidate_ids: tuple[str, ...] = Field(default=(), max_length=4096)
    findings: tuple[DocumentMapHeadingScopeFindingV1, ...] = Field(
        default=(),
        max_length=256,
    )
    public_summary: str = Field(min_length=1, max_length=1500)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_compilation(self) -> DocumentMapHeadingScopeCompilationV1:
        node_ids = tuple(item.stable_node_id for item in self.heading_nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("compiled heading node IDs must be unique")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"compilation_sha256"})
        )
        if self.compilation_sha256 != expected:
            raise ValueError("heading scope compilation SHA drifted")
        return self


@dataclass(frozen=True, slots=True)
class _ScopeSourceRecord:
    source_node_id: str
    source_order: int
    page_number: int | None
    table_id: str | None
    asset_id: str | None
    bbox: tuple[float, float, float, float] | None = None


def build_document_map_scope_request(
    *,
    source_ir: SourceIRSnapshot,
    survey: DocumentMapSurveyCompilationV1,
    visual_coverage: DocumentMapVisualCoverageReceiptV1,
    heading_skeleton: NativeHeadingSkeletonArtifactV1,
    evidence_workspace_bundle_sha256: str,
    start_candidate_id: str,
    next_candidate_id: str,
    context_nodes_per_side: int = 3,
) -> DocumentMapScopeRequestV1:
    source = SourceIRSnapshot.model_validate(source_ir.model_dump(mode="json"))
    survey = DocumentMapSurveyCompilationV1.model_validate(
        survey.model_dump(mode="json")
    )
    visual = DocumentMapVisualCoverageReceiptV1.model_validate(
        visual_coverage.model_dump(mode="json")
    )
    skeleton = NativeHeadingSkeletonArtifactV1.model_validate(
        heading_skeleton.model_dump(mode="json")
    )
    if (
        isinstance(context_nodes_per_side, bool)
        or not isinstance(context_nodes_per_side, int)
        or not 0 <= context_nodes_per_side <= 8
    ):
        raise ValueError("document-map scope context size must be between 0 and 8")
    if (
        source.source_sha256 != survey.source_sha256
        or source.source_sha256 != visual.source_sha256
        or source.source_sha256 != skeleton.source_sha256
        or source.source_ir_id != skeleton.source_ir_id
        or survey.visual_coverage_receipt_sha256 != visual.receipt_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", evidence_workspace_bundle_sha256)
    ):
        raise ValueError("document-map scope source lineage drifted")
    records = tuple(
        _ScopeSourceRecord(
            source_node_id=item.source_node_id,
            source_order=item.source_order,
            page_number=item.native_locator.page_number,
            table_id=(
                item.table_payload.table_id if item.table_payload is not None else None
            ),
            asset_id=item.asset_ref,
            bbox=item.native_locator.bbox,
        )
        for item in source.nodes
    )
    return _build_document_map_scope_request(
        run_id=survey.run_id,
        source_sha256=source.source_sha256,
        source_ir_id=source.source_ir_id,
        source_ir_canonical_digest=source_ir_canonical_sha256(source),
        evidence_workspace_bundle_sha256=evidence_workspace_bundle_sha256,
        survey=survey,
        visual=visual,
        skeleton=skeleton,
        records=records,
        start_candidate_id=start_candidate_id,
        next_candidate_id=next_candidate_id,
        context_nodes_per_side=context_nodes_per_side,
    )


def build_document_map_scope_request_from_evidence_index(
    *,
    run_id: str,
    source_sha256: str,
    source_ir_id: str,
    source_ir_canonical_sha256: str,
    evidence_workspace_bundle_sha256: str,
    source_ordinal_records: tuple[Mapping[str, Any], ...],
    survey: DocumentMapSurveyCompilationV1,
    visual_coverage: DocumentMapVisualCoverageReceiptV1,
    heading_skeleton: NativeHeadingSkeletonArtifactV1,
    start_candidate_id: str,
    next_candidate_id: str,
    context_nodes_per_side: int = 3,
) -> DocumentMapScopeRequestV1:
    survey = DocumentMapSurveyCompilationV1.model_validate(
        survey.model_dump(mode="json")
    )
    visual = DocumentMapVisualCoverageReceiptV1.model_validate(
        visual_coverage.model_dump(mode="json")
    )
    skeleton = NativeHeadingSkeletonArtifactV1.model_validate(
        heading_skeleton.model_dump(mode="json")
    )
    if (
        re.fullmatch(r"agent-run-[0-9a-f]{32}", run_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or not source_ir_id
        or re.fullmatch(r"[0-9a-f]{64}", source_ir_canonical_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", evidence_workspace_bundle_sha256) is None
        or survey.run_id != run_id
        or survey.source_sha256 != source_sha256
        or visual.source_sha256 != source_sha256
        or survey.visual_coverage_receipt_sha256 != visual.receipt_sha256
        or skeleton.source_sha256 != source_sha256
        or skeleton.source_ir_id != source_ir_id
    ):
        raise ValueError("document-map evidence-index lineage drifted")
    records = _scope_records_from_evidence_index(source_ordinal_records)
    return _build_document_map_scope_request(
        run_id=run_id,
        source_sha256=source_sha256,
        source_ir_id=source_ir_id,
        source_ir_canonical_digest=source_ir_canonical_sha256,
        evidence_workspace_bundle_sha256=evidence_workspace_bundle_sha256,
        survey=survey,
        visual=visual,
        skeleton=skeleton,
        records=records,
        start_candidate_id=start_candidate_id,
        next_candidate_id=next_candidate_id,
        context_nodes_per_side=context_nodes_per_side,
    )


def _build_document_map_scope_request(
    *,
    run_id: str,
    source_sha256: str,
    source_ir_id: str,
    source_ir_canonical_digest: str,
    evidence_workspace_bundle_sha256: str,
    survey: DocumentMapSurveyCompilationV1,
    visual: DocumentMapVisualCoverageReceiptV1,
    skeleton: NativeHeadingSkeletonArtifactV1,
    records: tuple[_ScopeSourceRecord, ...],
    start_candidate_id: str,
    next_candidate_id: str,
    context_nodes_per_side: int,
) -> DocumentMapScopeRequestV1:
    if (
        isinstance(context_nodes_per_side, bool)
        or not isinstance(context_nodes_per_side, int)
        or not 0 <= context_nodes_per_side <= 8
    ):
        raise ValueError("document-map scope context size must be between 0 and 8")
    candidates = {item.candidate_id: item for item in skeleton.candidates}
    start_candidate = candidates.get(start_candidate_id)
    next_candidate = candidates.get(next_candidate_id)
    if (
        start_candidate is None
        or next_candidate is None
        or start_candidate.level != 1
        or next_candidate.level != 1
        or next_candidate.source_order <= start_candidate.source_order
    ):
        raise ValueError("document-map scope chapter boundary suggestion is invalid")
    ordered_nodes = tuple(sorted(records, key=lambda item: item.source_order))
    if (
        not ordered_nodes
        or records != ordered_nodes
        or len({item.source_node_id for item in ordered_nodes}) != len(ordered_nodes)
        or len({item.source_order for item in ordered_nodes}) != len(ordered_nodes)
    ):
        raise ValueError("document-map scope source index is invalid")
    index_by_id = {
        item.source_node_id: index for index, item in enumerate(ordered_nodes)
    }
    try:
        start_index = index_by_id[start_candidate.source_node_id]
        end_index = index_by_id[next_candidate.source_node_id]
    except KeyError as exc:
        raise ValueError("document-map scope boundary source node is missing") from exc
    if end_index <= start_index:
        raise ValueError("document-map scope source span is empty")
    textual_interval_ids = {
        item.source_node_id for item in ordered_nodes[start_index:end_index]
    }
    owned_nodes = tuple(
        item
        for item in ordered_nodes
        if (
            _object_within_heading_extent(item, start_candidate, next_candidate)
            if (item.table_id is not None or item.asset_id is not None)
            and item.bbox is not None
            else item.source_node_id in textual_interval_ids
        )
    )
    owned_node_ids = tuple(item.source_node_id for item in owned_nodes)
    pages = tuple(
        sorted({page for item in owned_nodes if (page := item.page_number) is not None})
    )
    if not pages or not any(
        start_candidate.page_number in item.page_numbers for item in survey.scopes
    ):
        raise ValueError("document-map scope leaves the surveyed main document")
    scope_candidates = tuple(
        item
        for item in skeleton.candidates
        if start_candidate.source_order
        <= item.source_order
        < next_candidate.source_order
    )
    candidate_refs = tuple(
        DocumentMapHeadingCandidateRefV1(
            candidate_id=item.candidate_id,
            source_node_id=item.source_node_id,
            source_order=item.source_order,
            page_number=item.page_number,
            number=item.number,
            title=item.title,
            level=item.level,
            advisory_parent_candidate_id=item.parent_candidate_id,
            advisory_state=item.state,
        )
        for item in scope_candidates
    )
    span = DocumentMapSourceSpanV1(
        start=DocumentMapSourceEndpointV1(
            source_node_id=start_candidate.source_node_id,
            offset=0,
        ),
        end=DocumentMapSourceEndpointV1(
            source_node_id=next_candidate.source_node_id,
            offset=0,
        ),
    )
    scope_identity = {
        "source_sha256": source_sha256,
        "start": span.start.model_dump(mode="json"),
        "end": span.end.model_dump(mode="json"),
    }
    scope_id = "document-map-detail-scope-" + _canonical_sha256(scope_identity)[:32]
    values = {
        "run_id": run_id,
        "source_sha256": source_sha256,
        "source_ir_id": source_ir_id,
        "source_ir_canonical_sha256": source_ir_canonical_digest,
        "evidence_workspace_bundle_sha256": evidence_workspace_bundle_sha256,
        "survey_compilation_sha256": survey.compilation_sha256,
        "visual_coverage_receipt_sha256": visual.receipt_sha256,
        "heading_skeleton_sha256": skeleton.artifact_sha256,
        "scope_id": scope_id,
        "kind": DocumentMapDetailScopeKind.CHAPTER,
        "advisory_number": start_candidate.number,
        "advisory_title": start_candidate.title,
        "suggested_source_span": span,
        "source_node_ids": owned_node_ids,
        "page_numbers": pages,
        "physical_table_ids": tuple(
            dict.fromkeys(
                item.table_id for item in owned_nodes if item.table_id is not None
            )
        ),
        "asset_ids": tuple(
            dict.fromkeys(
                item.asset_id for item in owned_nodes if item.asset_id is not None
            )
        ),
        "heading_candidates": candidate_refs,
        "preceding_context_source_node_ids": tuple(
            item.source_node_id
            for item in ordered_nodes[
                max(0, start_index - context_nodes_per_side) : start_index
            ]
            if item.source_node_id not in owned_node_ids
        ),
        "following_context_source_node_ids": tuple(
            item.source_node_id
            for item in ordered_nodes[end_index : end_index + context_nodes_per_side]
            if item.source_node_id not in owned_node_ids
        ),
    }
    draft = DocumentMapScopeRequestV1.model_construct(
        **values,
        request_sha256="0" * 64,
    )
    return DocumentMapScopeRequestV1(
        **values,
        request_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"request_sha256"})
        ),
    )


def _object_within_heading_extent(
    item: _ScopeSourceRecord,
    start: NativeHeadingCandidateV1,
    end: NativeHeadingCandidateV1,
) -> bool:
    """Bind late-emitted parser objects by location, never parser append order."""
    if item.bbox is None or item.page_number is None:
        return False
    if not start.page_number <= item.page_number <= end.page_number:
        return False
    if item.page_number == start.page_number and item.bbox[1] < start.bbox[1]:
        return False
    if item.page_number == end.page_number and item.bbox[3] > end.bbox[1]:
        return False
    return True


def _scope_records_from_evidence_index(
    values: tuple[Mapping[str, Any], ...],
) -> tuple[_ScopeSourceRecord, ...]:
    records: list[_ScopeSourceRecord] = []
    for expected_ordinal, value in enumerate(values):
        source_node_id = value.get("source_node_id")
        source_order = value.get("source_order")
        source_ordinal = value.get("source_ordinal")
        page_number = value.get("page_number")
        table_id = value.get("table_id")
        asset_id = value.get("asset_ref")
        raw_bbox = value.get("bbox")
        bbox = tuple(raw_bbox) if isinstance(raw_bbox, (list, tuple)) else None
        if raw_bbox is not None and (
            bbox is None
            or len(bbox) != 4
            or any(
                type(item) not in (int, float) or not math.isfinite(item)
                for item in bbox
            )
            or bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
        ):
            raise ValueError("document-map source object locator is invalid")
        if (
            source_ordinal != expected_ordinal
            or not isinstance(source_node_id, str)
            or re.fullmatch(r"node-[0-9a-f]{32}", source_node_id) is None
            or isinstance(source_order, bool)
            or not isinstance(source_order, int)
            or source_order < 0
            or (
                page_number is not None
                and (
                    isinstance(page_number, bool)
                    or not isinstance(page_number, int)
                    or page_number < 1
                )
            )
            or (
                table_id is not None
                and (
                    not isinstance(table_id, str)
                    or not table_id.strip()
                    or len(table_id) > 160
                )
            )
            or (
                asset_id is not None
                and (
                    not isinstance(asset_id, str)
                    or not asset_id.strip()
                    or len(asset_id) > 160
                )
            )
        ):
            raise ValueError("document-map source ordinal ledger is invalid")
        records.append(
            _ScopeSourceRecord(
                source_node_id=source_node_id,
                source_order=source_order,
                page_number=page_number,
                table_id=table_id,
                asset_id=asset_id,
                bbox=bbox,
            )
        )
    return tuple(records)


def expand_document_map_heading_candidates(
    *,
    request: DocumentMapScopeRequestV1,
    candidate: DocumentMapHeadingScopeCandidateV2,
    source_ir: SourceIRSnapshot,
) -> DocumentMapScopeRequestV1:
    """Materialize Agent-proposed headings from exact owned SourceIR nodes.

    Expansion creates a new request and therefore requires another Agent
    decision turn. It never promotes a proposed heading directly into the Map.
    """

    request = DocumentMapScopeRequestV1.model_validate(request.model_dump(mode="json"))
    candidate = DocumentMapHeadingScopeCandidateV2.model_validate(
        candidate.model_dump(mode="json")
    )
    source = SourceIRSnapshot.model_validate(source_ir.model_dump(mode="json"))
    if (
        candidate.action
        is not DocumentMapHeadingScopeActionV2.EXPAND_HEADING_CANDIDATES
        or candidate.request_id != request.scope_id
        or candidate.request_sha256 != request.request_sha256
        or candidate.accepted_source_span != request.suggested_source_span
        or source.source_sha256 != request.source_sha256
        or source.source_ir_id != request.source_ir_id
        or source_ir_canonical_sha256(source) != request.source_ir_canonical_sha256
    ):
        raise ValueError("heading expansion lineage drifted")
    source_nodes = {item.source_node_id: item for item in source.nodes}
    owned_ids = set(request.source_node_ids)
    existing_by_source = {
        item.source_node_id: item for item in request.heading_candidates
    }
    proposed_by_source = {
        item.source_node_id: item for item in candidate.heading_expansion_proposals
    }
    if set(proposed_by_source).intersection(existing_by_source) or not set(
        proposed_by_source
    ).issubset(owned_ids):
        raise ValueError("heading expansion leaves the owned non-candidate source")
    allowed_kinds = {
        SourceNodeKind.HEADING,
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST,
        SourceNodeKind.LIST_ITEM,
        SourceNodeKind.NOTE,
        SourceNodeKind.UNKNOWN,
    }
    level_by_source = {
        item.source_node_id: item.level for item in request.heading_candidates
    }
    level_by_source.update(
        {
            item.source_node_id: item.level
            for item in candidate.heading_expansion_proposals
        }
    )
    additions: list[DocumentMapHeadingCandidateRefV1] = []
    for proposal in sorted(
        candidate.heading_expansion_proposals,
        key=lambda item: source_nodes[item.source_node_id].source_order
        if item.source_node_id in source_nodes
        else -1,
    ):
        node = source_nodes.get(proposal.source_node_id)
        parent = source_nodes.get(proposal.parent_source_node_id)
        if (
            node is None
            or parent is None
            or node.kind not in allowed_kinds
            or node.native_locator.page_number is None
            or node.native_locator.page_number not in request.page_numbers
            or not node.raw_text.strip()
            or proposal.parent_source_node_id not in level_by_source
            or level_by_source[proposal.parent_source_node_id] != proposal.level - 1
            or parent.source_order >= node.source_order
        ):
            raise ValueError("heading expansion source or parent is invalid")
        normalized_source = re.sub(r"\s+", " ", node.raw_text).strip().casefold()
        normalized_title = re.sub(r"\s+", " ", proposal.title).strip().casefold()
        if normalized_title not in normalized_source:
            raise ValueError("heading expansion title is not source-bound")
        if proposal.number is not None:
            normalized_number = re.sub(r"\s+", " ", proposal.number).strip().casefold()
            if normalized_number not in normalized_source:
                raise ValueError("heading expansion number is not source-bound")
        source_text = node.raw_text.strip()
        source_text_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        candidate_id = (
            "heading-candidate-"
            + _canonical_sha256(
                {
                    "source_sha256": source.source_sha256,
                    "source_node_id": node.source_node_id,
                    "source_order": node.source_order,
                    "source_text_sha256": source_text_sha256,
                }
            )[:32]
        )
        parent_ref = existing_by_source.get(proposal.parent_source_node_id)
        if parent_ref is None:
            parent_candidate_id = next(
                (
                    item.candidate_id
                    for item in additions
                    if item.source_node_id == proposal.parent_source_node_id
                ),
                None,
            )
        else:
            parent_candidate_id = parent_ref.candidate_id
        if parent_candidate_id is None:
            raise ValueError("heading expansion parent has not been materialized")
        additions.append(
            DocumentMapHeadingCandidateRefV1(
                candidate_id=candidate_id,
                source_node_id=node.source_node_id,
                source_order=node.source_order,
                page_number=node.native_locator.page_number,
                number=proposal.number,
                title=proposal.title,
                level=proposal.level,
                advisory_parent_candidate_id=parent_candidate_id,
                advisory_state=NativeHeadingCandidateState.REVIEW_REQUIRED,
            )
        )
    values = request.model_dump(mode="json", exclude={"request_sha256"})
    values["heading_candidates"] = [
        item.model_dump(mode="json")
        for item in sorted(
            (*request.heading_candidates, *additions),
            key=lambda item: item.source_order,
        )
    ]
    return DocumentMapScopeRequestV1(
        **values,
        request_sha256=_canonical_sha256(values),
    )


def compile_document_map_heading_scope(
    *,
    request: DocumentMapScopeRequestV1,
    candidate: DocumentMapHeadingScopeCandidateV1 | DocumentMapHeadingScopeCandidateV2,
    authorized_external_evidence_refs: frozenset[str] = frozenset(),
) -> DocumentMapHeadingScopeCompilationV1:
    request = DocumentMapScopeRequestV1.model_validate(request.model_dump(mode="json"))
    candidate = type(candidate).model_validate(candidate.model_dump(mode="json"))
    if any(
        not isinstance(item, str) or _SCOPE_EVIDENCE_REF_PATTERN.fullmatch(item) is None
        for item in authorized_external_evidence_refs
    ):
        raise ValueError("authorized external heading evidence is invalid")
    accepted_action = (
        DocumentMapHeadingScopeAction.ACCEPT_SCOPE
        if isinstance(candidate, DocumentMapHeadingScopeCandidateV1)
        else DocumentMapHeadingScopeActionV2.ACCEPT_SCOPE
    )
    if (
        candidate.request_id != request.scope_id
        or candidate.request_sha256 != request.request_sha256
        or candidate.action is not accepted_action
        or candidate.accepted_source_span != request.suggested_source_span
    ):
        raise ValueError("heading scope candidate requires adjacent coordination")
    decisions = {item.candidate_id: item for item in candidate.heading_decisions}
    expected_ids = tuple(item.candidate_id for item in request.heading_candidates)
    if len(decisions) != len(candidate.heading_decisions) or set(decisions) != set(
        expected_ids
    ):
        raise ValueError("heading scope decisions must cover every advisory candidate")
    refs = {item.candidate_id: item for item in request.heading_candidates}
    heading_ids = {
        candidate_id
        for candidate_id, decision in decisions.items()
        if decision.disposition is DocumentMapHeadingDisposition.HEADING
    }
    nodes: list[DocumentMapHeadingNodeV1] = []
    stable_ids = {
        candidate_id: _stable_node_id(request.source_sha256, refs[candidate_id])
        for candidate_id in heading_ids
    }
    chapter_root_candidate_id = request.heading_candidates[0].candidate_id
    accepted_level_one_ids = tuple(
        ref.candidate_id
        for ref in request.heading_candidates
        if ref.candidate_id in heading_ids and ref.level == 1
    )
    if request.kind is DocumentMapDetailScopeKind.CHAPTER and accepted_level_one_ids != (chapter_root_candidate_id,):
        raise ValueError(
            "heading scope requires exactly its first candidate as chapter root"
        )
    for ref in request.heading_candidates:
        decision = decisions[ref.candidate_id]
        if decision.disposition is DocumentMapHeadingDisposition.NOT_HEADING:
            continue
        parent_id = decision.parent_candidate_id
        if parent_id is not None:
            parent_ref = refs.get(parent_id)
            if (
                parent_ref is None
                or parent_id not in heading_ids
                or parent_ref.source_order >= ref.source_order
                or parent_ref.level != ref.level - 1
            ):
                raise ValueError("heading scope parent decision is invalid")
        elif ref.candidate_id != chapter_root_candidate_id and not (
            request.kind is DocumentMapDetailScopeKind.SECTION_GROUP and ref.level == 1
        ):
            raise ValueError("non-root heading requires a decided parent")
        nodes.append(
            DocumentMapHeadingNodeV1(
                stable_node_id=stable_ids[ref.candidate_id],
                candidate_id=ref.candidate_id,
                source_node_id=ref.source_node_id,
                source_order=ref.source_order,
                page_number=ref.page_number,
                number=ref.number,
                title=ref.title,
                level=ref.level,
                parent_stable_node_id=(
                    stable_ids[parent_id] if parent_id is not None else None
                ),
                confidence=decision.confidence,
                public_summary=decision.public_summary,
            )
        )
    if not nodes or nodes[0].level != 1:
        raise ValueError("heading scope requires one accepted chapter root")
    allowed_finding_pages = set(request.page_numbers)
    allowed_finding_nodes = {
        *request.source_node_ids,
        *request.preceding_context_source_node_ids,
        *request.following_context_source_node_ids,
    }
    for finding in candidate.findings:
        for evidence_ref in finding.evidence_refs:
            if evidence_ref in authorized_external_evidence_refs:
                continue
            if evidence_ref.startswith("page:") and (
                int(evidence_ref.split(":", 1)[1]) not in allowed_finding_pages
            ):
                raise ValueError("heading scope finding leaves its owned page context")
            if evidence_ref.startswith("node-") and (
                evidence_ref not in allowed_finding_nodes
            ):
                raise ValueError("heading scope finding leaves its source context")
            if evidence_ref.startswith("table-") and (
                evidence_ref not in request.physical_table_ids
            ):
                raise ValueError("heading scope finding leaves its table context")
            if evidence_ref.startswith("asset-") and (
                evidence_ref not in request.asset_ids
            ):
                raise ValueError("heading scope finding leaves its asset context")
    non_heading_ids = tuple(
        ref.candidate_id
        for ref in request.heading_candidates
        if ref.candidate_id not in heading_ids
    )
    values = {
        "source_sha256": request.source_sha256,
        "survey_compilation_sha256": request.survey_compilation_sha256,
        "scope_id": request.scope_id,
        "request_sha256": request.request_sha256,
        "accepted_source_span": candidate.accepted_source_span,
        "heading_nodes": tuple(nodes),
        "non_heading_candidate_ids": non_heading_ids,
        "findings": tuple(
            sorted(
                candidate.findings,
                key=_heading_scope_finding_sort_key,
            )
        ),
        "public_summary": candidate.public_summary,
        "candidate_sha256": _canonical_sha256(candidate.model_dump(mode="json")),
    }
    draft = DocumentMapHeadingScopeCompilationV1.model_construct(
        **values,
        compilation_sha256="0" * 64,
    )
    return DocumentMapHeadingScopeCompilationV1(
        **values,
        compilation_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"compilation_sha256"})
        ),
    )


def _stable_node_id(
    source_sha256: str,
    candidate: DocumentMapHeadingCandidateRefV1,
) -> str:
    return (
        "document-map-node-"
        + _canonical_sha256(
            {
                "source_sha256": source_sha256,
                "source_node_id": candidate.source_node_id,
                "source_order": candidate.source_order,
            }
        )[:32]
    )


def _heading_scope_finding_sort_key(
    finding: DocumentMapHeadingScopeFindingV1,
) -> tuple[int, str, str]:
    pages = tuple(
        int(item.split(":", 1)[1])
        for item in finding.evidence_refs
        if item.startswith("page:")
    )
    return (
        min(pages, default=251),
        finding.evidence_refs[0],
        finding.finding_code,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=lambda item: item.value if isinstance(item, StrEnum) else item,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_SCHEMA_VERSION",
    "DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_V2_SCHEMA_VERSION",
    "DOCUMENT_MAP_HEADING_SCOPE_COMPILATION_SCHEMA_VERSION",
    "DOCUMENT_MAP_SCOPE_REQUEST_SCHEMA_VERSION",
    "DocumentMapDetailScopeKind",
    "DocumentMapHeadingDecisionV1",
    "DocumentMapHeadingDisposition",
    "DocumentMapHeadingExpansionProposalV2",
    "DocumentMapHeadingNodeV1",
    "DocumentMapHeadingScopeAction",
    "DocumentMapHeadingScopeActionV2",
    "DocumentMapHeadingScopeCandidateV1",
    "DocumentMapHeadingScopeCandidateV2",
    "DocumentMapHeadingScopeCompilationV1",
    "DocumentMapHeadingScopeFindingV1",
    "DocumentMapScopeRequestV1",
    "DocumentMapScopeEvidenceRef",
    "DocumentMapSourceEndpointV1",
    "DocumentMapSourceSpanV1",
    "build_document_map_scope_request",
    "build_document_map_scope_request_from_evidence_index",
    "compile_document_map_heading_scope",
    "expand_document_map_heading_candidates",
]
