"""Strict contracts for isolated, single-document AI reading."""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from reqpilotx_server.domain.source_visual_capture import PageReadableMode, VisualTextRunV1


_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ReadingStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    FAILED = "FAILED"


class ReadingCoverageStatus(str, Enum):
    NOT_EVALUATED = "NOT_EVALUATED"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"


class ReadingAction(str, Enum):
    EXPLANATION = "EXPLANATION"
    TRANSLATION = "TRANSLATION"
    ENGINEERING_IMPACT = "ENGINEERING_IMPACT"
    CLARIFICATION_QUESTION = "CLARIFICATION_QUESTION"


class ReadingMessageRole(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ReadingChatTaskType(str, Enum):
    FREE_QUESTION = "FREE_QUESTION"
    QUICK_READ = "QUICK_READ"
    TECHNICAL_CONDITIONS = "TECHNICAL_CONDITIONS"
    SECTION_GUIDE = "SECTION_GUIDE"
    MISSED_CONDITIONS = "MISSED_CONDITIONS"


class ReadingConversationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"


class ReadingSourceRefV1(_StrictFrozenModel):
    kind: Literal["SOURCE_TEXT", "VISUAL_TEXT"] = "SOURCE_TEXT"
    source_node_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    visual_snapshot_id: StrictStr | None = Field(
        default=None, min_length=1, max_length=160
    )
    observation_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    page_number: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    page_bbox: tuple[float, float, float, float] | None = None
    page_image_sha256: StrictStr | None = Field(default=None, pattern=_SHA256_PATTERN)
    provider_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    provider_version: StrictStr | None = Field(default=None, min_length=1, max_length=500)
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def require_non_empty_half_open_range(self) -> ReadingSourceRefV1:
        if self.end_offset <= self.start_offset:
            raise ValueError("reading source refs require start_offset < end_offset")
        visual_provenance = (
            self.visual_snapshot_id,
            self.observation_id,
            self.page_number,
            self.bbox,
            self.page_bbox,
            self.page_image_sha256,
            self.provider_id,
            self.provider_version,
        )
        if self.kind == "SOURCE_TEXT":
            if self.source_node_id is None:
                raise ValueError("SOURCE_TEXT refs require source_node_id")
            if any(value is not None for value in visual_provenance):
                raise ValueError("SOURCE_TEXT refs cannot contain visual provenance")
        else:
            if self.source_node_id is not None:
                raise ValueError("VISUAL_TEXT refs cannot claim a native source node")
            if any(value is None for value in visual_provenance):
                raise ValueError("VISUAL_TEXT refs require complete rendered-page provenance")
            _validate_visual_boxes(self.bbox, self.page_bbox)
        return self


class ReadingRangeV1(_StrictFrozenModel):
    page_start: StrictInt | None = Field(default=None, ge=1)
    page_end: StrictInt | None = Field(default=None, ge=1)
    source_node_ids: tuple[StrictStr, ...] = ()
    reason_codes: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_range(self) -> ReadingRangeV1:
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("reading ranges require both page_start and page_end")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("page_end must not precede page_start")
        if len(self.source_node_ids) != len(set(self.source_node_ids)):
            raise ValueError("source_node_ids must not contain duplicates")
        return self


class ReadingCoverageV1(_StrictFrozenModel):
    status: ReadingCoverageStatus
    evaluated_ranges: tuple[ReadingRangeV1, ...] = ()
    unevaluated_ranges: tuple[ReadingRangeV1, ...] = ()
    blocked_ranges: tuple[ReadingRangeV1, ...] = ()
    reason_codes: tuple[StrictStr, ...] = ()


class ReadingOverviewCandidateV1(_StrictFrozenModel):
    title: StrictStr | None = Field(default=None, min_length=1, max_length=500)
    label: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    page_start: StrictInt | None = Field(default=None, ge=1)
    page_end: StrictInt | None = Field(default=None, ge=1)
    source_node_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_candidate(self) -> ReadingOverviewCandidateV1:
        if not self.title and not self.label:
            raise ValueError("overview candidates require a title or label")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("candidate pages must be provided together")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("candidate page_end must not precede page_start")
        return self


class ReadingRiskRegionV1(_StrictFrozenModel):
    kind: StrictStr = Field(min_length=1, max_length=120)
    page_number: StrictInt | None = Field(default=None, ge=1)
    source_node_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: StrictStr = Field(min_length=1, max_length=160)


class DocumentReadingOverviewV1(_StrictFrozenModel):
    schema_version: Literal["document-reading-overview/v1"] = (
        "document-reading-overview/v1"
    )
    document_type_candidate: ReadingOverviewCandidateV1 | None = None
    document_format: StrictStr = Field(min_length=1, max_length=32)
    page_count: StrictInt = Field(ge=0)
    node_count: StrictInt = Field(ge=0)
    table_count: StrictInt = Field(ge=0)
    image_count: StrictInt = Field(ge=0)
    heading_count: StrictInt = Field(ge=0)
    toc_candidates: tuple[ReadingOverviewCandidateV1, ...] = ()
    section_candidates: tuple[ReadingOverviewCandidateV1, ...] = ()
    body_candidate_ranges: tuple[ReadingRangeV1, ...] = ()
    appendix_candidate_ranges: tuple[ReadingRangeV1, ...] = ()
    attachment_suspect_ranges: tuple[ReadingRangeV1, ...] = ()
    risk_regions: tuple[ReadingRiskRegionV1, ...] = ()
    coverage: ReadingCoverageV1


class ReadingStructureMapNodeV1(_StrictFrozenModel):
    map_node_id: StrictStr = Field(min_length=1, max_length=160)
    source_node_id: StrictStr = Field(min_length=1, max_length=160)
    parent_map_node_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    kind: Literal["SECTION", "TOC_ENTRY", "APPENDIX", "ATTACHMENT"]
    title: StrictStr = Field(min_length=1, max_length=500)
    level_hint: StrictInt = Field(ge=1, le=32)
    source_order: StrictInt = Field(ge=0)
    page_start: StrictInt = Field(ge=1)
    page_end: StrictInt = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: tuple[StrictStr, ...] = ()
    evidence_status: Literal["SOURCE_BOUND", "PARTIAL_EVIDENCE", "STALE"]

    @model_validator(mode="after")
    def validate_map_node(self) -> ReadingStructureMapNodeV1:
        if self.page_end < self.page_start:
            raise ValueError("map node page_end must not precede page_start")
        if self.parent_map_node_id == self.map_node_id:
            raise ValueError("map nodes cannot parent themselves")
        return self


class ReadingStructureMapV1(_StrictFrozenModel):
    schema_version: Literal["reading-structure-map/v1"] = "reading-structure-map/v1"
    map_id: StrictStr = Field(min_length=1, max_length=160)
    status: ReadingStatus
    generation_status: ReadingStatus
    coverage_status: ReadingCoverageStatus
    page_count: StrictInt = Field(ge=0)
    reason_codes: tuple[StrictStr, ...] = ()
    nodes: tuple[ReadingStructureMapNodeV1, ...] = ()

    @model_validator(mode="after")
    def validate_map_graph(self) -> ReadingStructureMapV1:
        ids = {node.map_node_id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("reading map node IDs must be unique")
        if any(
            node.parent_map_node_id is not None
            and node.parent_map_node_id not in ids
            for node in self.nodes
        ):
            raise ValueError("reading map parents must exist in the same map")
        orders = [node.source_order for node in self.nodes]
        if orders != sorted(orders):
            raise ValueError("reading map nodes must preserve source order")
        return self


class ReadingSelectableNodeV1(_StrictFrozenModel):
    source_node_id: StrictStr = Field(min_length=1, max_length=160)
    kind: StrictStr = Field(min_length=1, max_length=64)
    source_order: StrictInt = Field(ge=0)
    page_number: StrictInt | None = Field(default=None, ge=1)
    text_length: StrictInt = Field(ge=0)
    preview: StrictStr = Field(max_length=200)


class ReadingSourcePageNodeV1(ReadingSelectableNodeV1):
    text: StrictStr = Field(min_length=1, max_length=2_000_000)

    @model_validator(mode="after")
    def validate_exact_text_length(self) -> ReadingSourcePageNodeV1:
        if len(self.text) != self.text_length:
            raise ValueError("reading source-page text_length must match exact text")
        return self


class ReadingSourcePageV1(_StrictFrozenModel):
    schema_version: Literal["reading-source-page/v1"] = "reading-source-page/v1"
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    reading_session_id: StrictStr = Field(min_length=1, max_length=160)
    source_ir_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    effective_bundle_id: StrictStr = Field(min_length=1, max_length=160)
    visual_generation_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    page_number: StrictInt = Field(ge=1)
    page_count: StrictInt = Field(ge=0)
    readable_mode: PageReadableMode
    nodes: tuple[ReadingSourcePageNodeV1, ...] = ()
    visual_runs: tuple[VisualTextRunV1, ...] = ()

    @model_validator(mode="after")
    def arbitrate_page_facts(self) -> ReadingSourcePageV1:
        if self.readable_mode is PageReadableMode.NATIVE and self.visual_runs:
            raise ValueError("NATIVE pages cannot expose visual runs")
        if self.readable_mode is PageReadableMode.VISUAL and self.nodes:
            raise ValueError("VISUAL pages cannot expose conflicting native nodes")
        if self.readable_mode is PageReadableMode.NONE and (self.nodes or self.visual_runs):
            raise ValueError("unreadable pages cannot expose selectable facts")
        return self


class ReadingArtifactV1(_StrictFrozenModel):
    schema_version: Literal["reading-artifact/v1"] = "reading-artifact/v1"
    artifact_id: StrictStr = Field(min_length=1, max_length=160)
    reading_session_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    action: ReadingAction
    source_ref: ReadingSourceRefV1
    output_text: StrictStr = Field(min_length=1, max_length=20_000)
    status: Literal["ACTIVE", "STALE", "FAILED"] = "ACTIVE"
    provider_id: StrictStr = Field(min_length=1, max_length=160)
    model_name: StrictStr = Field(min_length=1, max_length=160)
    prompt_version: StrictStr = Field(min_length=1, max_length=160)
    created_at_utc: datetime

    @field_validator("created_at_utc")
    @classmethod
    def require_artifact_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "created_at_utc")


class ReadingNoteV1(_StrictFrozenModel):
    schema_version: Literal["reading-note/v1"] = "reading-note/v1"
    note_id: StrictStr = Field(min_length=1, max_length=160)
    reading_session_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    source_ref: ReadingSourceRefV1
    content: StrictStr = Field(min_length=1, max_length=20_000)
    revision: StrictInt = Field(ge=1)
    status: Literal["ACTIVE", "STALE", "DELETED"] = "ACTIVE"
    created_at_utc: datetime
    updated_at_utc: datetime

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def require_note_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "note timestamp")

    @model_validator(mode="after")
    def validate_note_timestamps(self) -> ReadingNoteV1:
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("updated_at_utc must not precede created_at_utc")
        return self


class ReadingContextV1(_StrictFrozenModel):
    schema_version: Literal["reading-context/v1"] = "reading-context/v1"
    reading_session_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    source_ir_id: StrictStr = Field(min_length=1, max_length=160)
    source_ir_document_id: StrictStr = Field(min_length=1, max_length=160)
    effective_bundle_id: StrictStr = Field(min_length=1, max_length=160)
    visual_generation_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    status: ReadingStatus
    overview: DocumentReadingOverviewV1
    structure_map: ReadingStructureMapV1
    selectable_nodes: tuple[ReadingSelectableNodeV1, ...] = ()
    artifacts: tuple[ReadingArtifactV1, ...] = ()
    notes: tuple[ReadingNoteV1, ...] = ()
    created_at_utc: datetime
    updated_at_utc: datetime

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def require_context_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "context timestamp")

    @model_validator(mode="after")
    def validate_context_scope(self) -> ReadingContextV1:
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("updated_at_utc must not precede created_at_utc")
        scope = (
            self.reading_session_id,
            self.project_id,
            self.sor_document_id,
            self.source_sha256,
        )
        for artifact in self.artifacts:
            if (
                artifact.reading_session_id,
                artifact.project_id,
                artifact.sor_document_id,
                artifact.source_sha256,
            ) != scope:
                raise ValueError("reading artifacts must match the context scope")
        for note in self.notes:
            if (
                note.reading_session_id,
                note.project_id,
                note.sor_document_id,
                note.source_sha256,
            ) != scope:
                raise ValueError("reading notes must match the context scope")
        return self


class ReadingPrepareRequestV1(_StrictFrozenModel):
    schema_version: Literal["reading-prepare-request/v1"] = "reading-prepare-request/v1"
    expected_source_sha256: StrictStr | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )


class ReadingActionRequestV1(_StrictFrozenModel):
    schema_version: Literal["reading-action-request/v1"] = "reading-action-request/v1"
    action: ReadingAction
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    source_ref: ReadingSourceRefV1
    target_language: StrictStr | None = Field(
        default=None,
        min_length=2,
        max_length=32,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
    )

    @model_validator(mode="after")
    def constrain_target_language(self) -> ReadingActionRequestV1:
        if self.action is not ReadingAction.TRANSLATION and self.target_language:
            raise ValueError("target_language is only valid for translation actions")
        return self


class ReadingNoteCreateRequestV1(_StrictFrozenModel):
    schema_version: Literal["reading-note-create-request/v1"] = (
        "reading-note-create-request/v1"
    )
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    source_ref: ReadingSourceRefV1
    content: StrictStr = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def reject_blank_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("note content must not be blank")
        return value


class ReadingNoteUpdateRequestV1(_StrictFrozenModel):
    schema_version: Literal["reading-note-update-request/v1"] = (
        "reading-note-update-request/v1"
    )
    expected_revision: StrictInt = Field(ge=1)
    content: StrictStr = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def reject_blank_note_update(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("note content must not be blank")
        return value


class ReadingNoteListV1(_StrictFrozenModel):
    schema_version: Literal["reading-note-list/v1"] = "reading-note-list/v1"
    items: tuple[ReadingNoteV1, ...] = ()


class ReadingChatCitationV1(_StrictFrozenModel):
    citation_id: StrictStr = Field(min_length=1, max_length=80)
    source_node_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    page_number: StrictInt | None = Field(default=None, ge=1)
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(gt=0)
    quote: StrictStr = Field(min_length=1, max_length=1600)
    label: StrictStr = Field(min_length=1, max_length=120)
    locator_kind: Literal["SOURCE_TEXT", "TABLE_CELL", "VISUAL_TEXT"] = "SOURCE_TEXT"
    cell_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    visual_snapshot_id: StrictStr | None = Field(
        default=None, min_length=1, max_length=160
    )
    observation_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    bbox: tuple[float, float, float, float] | None = None
    page_bbox: tuple[float, float, float, float] | None = None
    page_image_sha256: StrictStr | None = Field(default=None, pattern=_SHA256_PATTERN)
    provider_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    provider_version: StrictStr | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_citation_range(self) -> ReadingChatCitationV1:
        if self.end_offset <= self.start_offset:
            raise ValueError("reading citations require start_offset < end_offset")
        if len(self.quote) != self.end_offset - self.start_offset:
            raise ValueError("reading citation quote must exactly match its source range")
        visual_fields = (
            self.visual_snapshot_id,
            self.observation_id,
            self.bbox,
            self.page_bbox,
            self.page_image_sha256,
            self.provider_id,
            self.provider_version,
        )
        if self.locator_kind == "SOURCE_TEXT":
            if self.source_node_id is None or self.cell_id is not None:
                raise ValueError("source-text citations require only a source node")
            if any(value is not None for value in visual_fields):
                raise ValueError("source-text citations cannot claim visual provenance")
        elif self.locator_kind == "TABLE_CELL":
            if self.source_node_id is None or self.cell_id is None:
                raise ValueError("table-cell citations require source node and cell_id")
            if any(value is not None for value in visual_fields):
                raise ValueError("table-cell citations cannot claim visual provenance")
        else:
            if self.source_node_id is not None or self.cell_id is not None:
                raise ValueError("visual citations cannot claim native identities")
            if self.page_number is None or any(value is None for value in visual_fields):
                raise ValueError("visual citations require complete rendered-page provenance")
            _validate_visual_boxes(self.bbox, self.page_bbox)
        return self


class ReadingConversationV1(_StrictFrozenModel):
    schema_version: Literal["reading-conversation/v1"] = "reading-conversation/v1"
    conversation_id: StrictStr = Field(min_length=1, max_length=160)
    reading_session_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    effective_bundle_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    title: StrictStr = Field(min_length=1, max_length=120)
    revision: StrictInt = Field(default=1, ge=1)
    status: ReadingConversationStatus = ReadingConversationStatus.ACTIVE
    created_at_utc: datetime
    updated_at_utc: datetime
    last_message_at_utc: datetime | None = None

    @field_validator("title")
    @classmethod
    def reject_blank_conversation_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("conversation title must not be blank")
        return value

    @field_validator("created_at_utc", "updated_at_utc", "last_message_at_utc")
    @classmethod
    def require_conversation_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone(value, "conversation timestamp")

    @model_validator(mode="after")
    def validate_conversation_timestamps(self) -> ReadingConversationV1:
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("conversation updated_at cannot precede created_at")
        if (
            self.last_message_at_utc is not None
            and self.last_message_at_utc < self.created_at_utc
        ):
            raise ValueError("conversation last message cannot precede creation")
        return self


class ReadingChatMessageV1(_StrictFrozenModel):
    schema_version: Literal["reading-chat-message/v1"] = "reading-chat-message/v1"
    message_id: StrictStr = Field(min_length=1, max_length=160)
    conversation_id: StrictStr = Field(min_length=1, max_length=160)
    reading_session_id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    sor_document_id: StrictStr = Field(min_length=1, max_length=160)
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    effective_bundle_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    sequence_number: StrictInt = Field(ge=1)
    role: ReadingMessageRole
    content: StrictStr = Field(min_length=1, max_length=20_000)
    source_ref: ReadingSourceRefV1 | None = None
    source_refs: tuple[ReadingSourceRefV1, ...] = Field(default=(), max_length=16)
    task_type: ReadingChatTaskType | None = None
    citations: tuple[ReadingChatCitationV1, ...] = ()
    retrieval: ReadingRetrievalV1 | None = None
    provider_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    model_name: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    prompt_version: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    status: ReadingConversationStatus = ReadingConversationStatus.ACTIVE
    created_at_utc: datetime

    @field_validator("content")
    @classmethod
    def reject_blank_chat_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chat message content must not be blank")
        return value

    @field_validator("created_at_utc")
    @classmethod
    def require_message_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "message timestamp")

    @model_validator(mode="after")
    def validate_role_payload(self) -> ReadingChatMessageV1:
        citation_ids = tuple(item.citation_id for item in self.citations)
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("chat citation IDs must be unique within a message")
        metadata = (self.provider_id, self.model_name, self.prompt_version)
        if self.role is ReadingMessageRole.USER:
            if (
                self.citations
                or self.retrieval is not None
                or any(value is not None for value in metadata)
            ):
                raise ValueError("user messages cannot contain model output metadata")
            if self.source_ref is not None and (
                self.source_refs and self.source_refs[0] != self.source_ref
            ):
                raise ValueError("source_ref must match the first source_refs item")
        else:
            if self.source_ref is not None or self.source_refs:
                raise ValueError("assistant messages cannot contain a user source selection")
            if self.task_type is not None:
                raise ValueError("assistant messages cannot contain task_type")
            if any(value is None for value in metadata):
                raise ValueError("assistant messages require complete provider metadata")
            if self.retrieval is None:
                raise ValueError("assistant messages require persisted retrieval coverage")
        return self


class ReadingRetrievalV1(_StrictFrozenModel):
    schema_version: Literal["reading-retrieval/v1"] = "reading-retrieval/v1"
    # The legacy pair remains readable so existing conversations are not
    # corrupted by this serving upgrade.  New application code emits only the
    # full-document pair and the provider refuses legacy modes.
    strategy: Literal[
        "source-ir-full-document/v2",
        "effective-source-full-document/v1",
        "effective-source-full-effective-text/v1",
        "effective-source-selected-context/v1",
        "source-ir-lexical-balanced/v1",
        "document-overview",
    ] = "effective-source-full-document/v1"
    coverage_mode: Literal[
        "FULL_DOCUMENT",
        "FULL_EFFECTIVE_TEXT",
        "HIERARCHICAL_FULL_DOCUMENT",
        "SELECTED_CONTEXT",
        "TARGETED",
        "DOCUMENT_OVERVIEW",
    ]
    selected_node_count: StrictInt = Field(ge=0)
    selected_character_count: StrictInt = Field(ge=0)
    document_node_count: StrictInt = Field(ge=0)
    covered_page_numbers: tuple[StrictInt, ...] = ()
    effective_bundle_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    visual_generation_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    capture_quality_status: Literal["READY", "PARTIAL", "BLOCKED"] | None = None
    unreadable_page_numbers: tuple[StrictInt, ...] = ()

    @model_validator(mode="after")
    def validate_strategy_coverage_pair(self) -> ReadingRetrievalV1:
        full_document_modes = {"FULL_DOCUMENT", "HIERARCHICAL_FULL_DOCUMENT"}
        complete_modes = {*full_document_modes, "FULL_EFFECTIVE_TEXT"}
        legacy_modes = {"TARGETED", "DOCUMENT_OVERVIEW"}
        if self.strategy in {
            "source-ir-full-document/v2",
            "effective-source-full-document/v1",
        }:
            if self.coverage_mode not in full_document_modes:
                raise ValueError("full-document strategy requires full-document coverage")
        elif self.strategy == "effective-source-full-effective-text/v1":
            if self.coverage_mode != "FULL_EFFECTIVE_TEXT":
                raise ValueError(
                    "full-effective-text strategy requires visual-text coverage"
                )
            if self.visual_generation_id is None:
                raise ValueError(
                    "full-effective-text coverage requires a visual generation"
                )
        elif self.strategy == "effective-source-selected-context/v1":
            if self.coverage_mode != "SELECTED_CONTEXT":
                raise ValueError("selected-context strategy requires selected coverage")
        elif self.coverage_mode not in legacy_modes:
            raise ValueError("legacy strategy requires legacy coverage")
        if self.coverage_mode in complete_modes and self.capture_quality_status not in {
            None,
            "READY",
        }:
            raise ValueError("full-document coverage requires READY effective facts")
        return self

    @field_validator("covered_page_numbers")
    @classmethod
    def validate_covered_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page < 1 for page in value):
            raise ValueError("covered pages must be positive")
        if value != tuple(sorted(set(value))):
            raise ValueError("covered pages must be unique and sorted")
        return value

    @field_validator("unreadable_page_numbers")
    @classmethod
    def validate_unreadable_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page < 1 for page in value) or value != tuple(sorted(set(value))):
            raise ValueError("unreadable pages must be unique, sorted, and positive")
        return value


class ReadingConversationCreateRequestV1(_StrictFrozenModel):
    schema_version: Literal["reading-conversation-create-request/v1"] = (
        "reading-conversation-create-request/v1"
    )
    title: StrictStr | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def reject_blank_optional_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("conversation title must not be blank")
        return value


class ReadingMessageCreateRequestV1(_StrictFrozenModel):
    schema_version: Literal["reading-message-create-request/v1"] = (
        "reading-message-create-request/v1"
    )
    source_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    content: StrictStr = Field(min_length=1, max_length=20_000)
    source_ref: ReadingSourceRefV1 | None = None
    source_refs: tuple[ReadingSourceRefV1, ...] = Field(default=(), max_length=16)
    task_type: ReadingChatTaskType = ReadingChatTaskType.FREE_QUESTION
    coverage_intent: Literal["AUTO", "DOCUMENT_OVERVIEW"] = "AUTO"

    @field_validator("content")
    @classmethod
    def reject_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chat question must not be blank")
        return value

    @model_validator(mode="after")
    def validate_source_refs(self) -> ReadingMessageCreateRequestV1:
        if self.source_ref is not None and (
            self.source_refs and self.source_refs[0] != self.source_ref
        ):
            raise ValueError("source_ref must match the first source_refs item")
        return self


class ReadingConversationListV1(_StrictFrozenModel):
    schema_version: Literal["reading-conversation-list/v1"] = (
        "reading-conversation-list/v1"
    )
    items: tuple[ReadingConversationV1, ...] = ()


class ReadingMessageListV1(_StrictFrozenModel):
    schema_version: Literal["reading-message-list/v1"] = "reading-message-list/v1"
    conversation_id: StrictStr = Field(min_length=1, max_length=160)
    items: tuple[ReadingChatMessageV1, ...] = ()


class ReadingChatTurnV1(_StrictFrozenModel):
    schema_version: Literal["reading-chat-turn/v1"] = "reading-chat-turn/v1"
    conversation: ReadingConversationV1
    user_message: ReadingChatMessageV1
    assistant_message: ReadingChatMessageV1
    retrieval: ReadingRetrievalV1

    @model_validator(mode="after")
    def validate_turn_scope(self) -> ReadingChatTurnV1:
        messages = (self.user_message, self.assistant_message)
        scope = (
            self.conversation.conversation_id,
            self.conversation.reading_session_id,
            self.conversation.project_id,
            self.conversation.sor_document_id,
            self.conversation.source_sha256,
            self.conversation.effective_bundle_id,
        )
        for message in messages:
            if (
                message.conversation_id,
                message.reading_session_id,
                message.project_id,
                message.sor_document_id,
                message.source_sha256,
                message.effective_bundle_id,
            ) != scope:
                raise ValueError("chat messages must match the conversation scope")
        if self.user_message.role is not ReadingMessageRole.USER:
            raise ValueError("chat turn user_message must have USER role")
        if self.assistant_message.role is not ReadingMessageRole.ASSISTANT:
            raise ValueError("chat turn assistant_message must have ASSISTANT role")
        if self.assistant_message.sequence_number != self.user_message.sequence_number + 1:
            raise ValueError("chat turn message sequence must be consecutive")
        return self


class ReadingAIReadinessV1(_StrictFrozenModel):
    schema_version: Literal["reading-ai-readiness/v1"] = "reading-ai-readiness/v1"
    ready: bool
    provider_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    model_name: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    configuration_source: Literal[
        "CODEX_RUNTIME",
        "READING_OVERRIDE",
        "SHARED_RUNTIME",
        "NONE",
    ]
    reason_code: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    capture_ready: bool | None = None
    capture_quality_status: Literal["READY", "PARTIAL", "BLOCKED"] | None = None
    effective_bundle_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    visual_generation_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    visual_required_page_count: StrictInt | None = Field(default=None, ge=0)
    visual_ready_page_count: StrictInt | None = Field(default=None, ge=0)


class ReadingConversationDeleteResultV1(_StrictFrozenModel):
    schema_version: Literal["reading-conversation-delete-result/v1"] = (
        "reading-conversation-delete-result/v1"
    )
    conversation_id: StrictStr = Field(min_length=1, max_length=160)
    deleted: Literal[True] = True


class ReadingConversationClearResultV1(_StrictFrozenModel):
    schema_version: Literal["reading-conversation-clear-result/v1"] = (
        "reading-conversation-clear-result/v1"
    )
    conversation_id: StrictStr = Field(min_length=1, max_length=160)
    cleared: Literal[True] = True
    deleted_message_count: StrictInt = Field(ge=0)


def _require_timezone(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _validate_visual_boxes(
    bbox: tuple[float, float, float, float] | None,
    page_bbox: tuple[float, float, float, float] | None,
) -> None:
    if bbox is None or page_bbox is None:
        raise ValueError("visual provenance requires bbox and page_bbox")
    for label, value in (("bbox", bbox), ("page_bbox", page_bbox)):
        x0, y0, x1, y1 = value
        if not all(math.isfinite(item) for item in value) or x1 <= x0 or y1 <= y0:
            raise ValueError(f"{label} must be a finite, positive-area rectangle")
    bx0, by0, bx1, by1 = bbox
    px0, py0, px1, py1 = page_bbox
    if bx0 < px0 or by0 < py0 or bx1 > px1 or by1 > py1:
        raise ValueError("visual bbox must be contained by page_bbox")


__all__ = [
    "DocumentReadingOverviewV1",
    "ReadingAction",
    "ReadingActionRequestV1",
    "ReadingAIReadinessV1",
    "ReadingArtifactV1",
    "ReadingChatCitationV1",
    "ReadingChatMessageV1",
    "ReadingChatTaskType",
    "ReadingChatTurnV1",
    "ReadingConversationClearResultV1",
    "ReadingConversationCreateRequestV1",
    "ReadingConversationDeleteResultV1",
    "ReadingConversationListV1",
    "ReadingConversationStatus",
    "ReadingConversationV1",
    "ReadingContextV1",
    "ReadingCoverageStatus",
    "ReadingNoteCreateRequestV1",
    "ReadingNoteListV1",
    "ReadingNoteUpdateRequestV1",
    "ReadingNoteV1",
    "ReadingMessageCreateRequestV1",
    "ReadingMessageListV1",
    "ReadingMessageRole",
    "ReadingPrepareRequestV1",
    "ReadingSelectableNodeV1",
    "ReadingSourcePageNodeV1",
    "ReadingSourcePageV1",
    "ReadingSourceRefV1",
    "ReadingStatus",
    "ReadingStructureMapV1",
    "ReadingRetrievalV1",
]
