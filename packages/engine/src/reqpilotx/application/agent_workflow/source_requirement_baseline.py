from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from reqpilotx.domain.extraction.source_ir import NativeLocator
from reqpilotx.application.agent_workflow.source_locations import validate_original_location, original_format, source_position, native_location_fields, validate_native_table_cells

from reqpilotx.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)

DOCUMENT_MAP_ARTIFACT_SCHEMA_VERSION = "document-map-artifact/v1"
SOURCE_REQUIREMENT_SCOPE_REQUEST_SCHEMA_VERSION = "source-requirement-scope-request/v1"
SOURCE_REQUIREMENT_SLICE_CANDIDATE_SCHEMA_VERSION = (
    "source-requirement-slice-candidate/v1"
)
SOURCE_ITEM_TEXT_SCOPE_REQUEST_SCHEMA_VERSION = "source-item-text-scope-request/v1"
SOURCE_ITEM_TEXT_SLICE_CANDIDATE_SCHEMA_VERSION = "source-item-text-slice-candidate/v1"
SOURCE_REQUIREMENT_BASELINE_SCHEMA_VERSION = "source-requirement-baseline-artifact/v1"
SOURCE_REQUIREMENT_VALIDATION_RECEIPT_SCHEMA_VERSION = (
    "source-requirement-baseline-validation-receipt/v1"
)
SOURCE_REQUIREMENT_GROWTH_RECEIPT_SCHEMA_VERSION = (
    "source-requirement-baseline-growth-receipt/v1"
)
SOURCE_ITEM_SCOPE_REVISION_SCHEMA_VERSION = "source-item-scope-revision/v1"
SOURCE_ITEM_SCOPE_REVISION_RECEIPT_SCHEMA_VERSION = (
    "source-item-scope-revision-receipt/v1"
)
SOURCE_REQUIREMENT_CLASSIFICATION_COORDINATION_SCHEMA_VERSION = (
    "source-requirement-classification-coordination/v1"
)
SOURCE_REQUIREMENT_CLASSIFICATION_COORDINATION_RECEIPT_SCHEMA_VERSION = (
    "source-requirement-classification-coordination-receipt/v1"
)


class SourceRequirementBaselineError(ValueError):
    """Fail-closed contract or coverage error for the source baseline."""


FRONT_MATTER_KINDS = frozenset({"COVER", "TABLE_OF_CONTENTS", "DOCUMENT_CONTROL"})


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class DocumentMapContentBlockV1(_StrictFrozenModel):
    kind: str = Field(min_length=1, max_length=80)
    logical_table_scope_id: str = Field(default="", max_length=200)


class DocumentMapSourceContentV1(_StrictFrozenModel):
    content_id: str = Field(min_length=1, max_length=200)
    page: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    source_bounds: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    source_handles: tuple[str, ...] = Field(default=(), max_length=4096)
    text: str = Field(max_length=1_000_000)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    precision: str = Field(min_length=1, max_length=80)
    is_title: bool
    source_text_range: tuple[StrictInt, StrictInt] | None = Field(default=None, exclude_if=lambda value: value is None)

    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_content(self) -> DocumentMapSourceContentV1:
        if canonical_text_sha256(self.text) != self.text_sha256:
            raise ValueError("document-map source content hash drifted")
        if self.source_text_range is not None:
            lo, hi = self.source_text_range
            if lo < 0 or hi <= lo or hi - lo != len(self.text) or len(self.source_handles) != 1:
                raise ValueError('document-map text range must bind one exact original substring')
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.bbox, subject="document-map content")
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.source_bounds, subject="document-map content bounds")
        if len(self.source_handles) != len(set(self.source_handles)):
            raise ValueError("document-map source handles must be unique")
        return self
    @model_validator(mode="after")
    def validate_original_location(self):
        validate_original_location(page=self.page, bbox=self.bbox, source_bounds=self.source_bounds, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="DocumentMapSourceContentV1", require_geometry=True)
        return self



class DocumentMapSourceAnchorV1(_StrictFrozenModel):
    anchor_id: str = Field(min_length=1, max_length=200)
    page: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    source_bounds: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    anchor_kind: str = Field(min_length=1, max_length=80)
    precision: str = Field(min_length=1, max_length=80)
    source_handle: str = Field(default="", max_length=240)
    source_text_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    logical_table_scope_id: str = Field(default="", max_length=200)
    table_token: str = Field(default="", max_length=200)
    row_range: tuple[StrictInt, StrictInt] | None = None
    placement_id: str = Field(default="", max_length=200)
    asset_id: str = Field(default="", max_length=200)
    source_text_range: tuple[StrictInt, StrictInt] | None = Field(default=None, exclude_if=lambda value: value is None)

    content_sha256: str = Field(default='', pattern=r'^$|^[0-9a-f]{64}$', exclude_if=lambda value: not value)
    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_anchor(self) -> DocumentMapSourceAnchorV1:
        if self.source_text_range is not None and (self.source_text_range[0] < 0 or self.source_text_range[1] <= self.source_text_range[0]):
            raise ValueError('document-map anchor text range must be a non-empty half-open interval')
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.bbox, subject="document-map anchor")
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.source_bounds, subject="document-map anchor bounds")
        if self.row_range is not None and self.row_range[1] <= self.row_range[0]:
            raise ValueError("document-map anchor row range must be half-open")
        return self
    @model_validator(mode="after")
    def validate_original_location(self):
        validate_original_location(page=self.page, bbox=self.bbox, source_bounds=self.source_bounds, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="DocumentMapSourceAnchorV1", require_geometry=True)
        return self



class DocumentMapNodeV1(_StrictFrozenModel):
    node_id: str = Field(min_length=1, max_length=200)
    parent_id: str | None = Field(default=None, max_length=200)
    order: StrictInt = Field(ge=0)
    kind: str = Field(min_length=1, max_length=80)
    label: str = Field(default="", max_length=240)
    title: str = Field(min_length=1, max_length=1000)
    title_authority: str = Field(min_length=1, max_length=120)
    ancestor_path: tuple[str, ...] = Field(default=(), max_length=64)
    pages: tuple[StrictInt, ...] = Field(min_length=0, max_length=4096)
    content_blocks: tuple[DocumentMapContentBlockV1, ...] = Field(default=())
    source_anchors: tuple[DocumentMapSourceAnchorV1, ...] = Field(default=())
    source_content: tuple[DocumentMapSourceContentV1, ...] = Field(default=())
    title_reference_anchors: tuple[DocumentMapSourceAnchorV1, ...] = Field(default=(), exclude_if=lambda value: not value)
    evidence_anchors: tuple[DocumentMapSourceAnchorV1, ...] = Field(default=(), exclude_if=lambda value: not value)
    finding_codes: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("pages")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("document-map node pages must be unique and sorted")
        return value


class DocumentMapTableFragmentV1(_StrictFrozenModel):
    table_token: str = Field(min_length=1, max_length=200)
    page: StrictInt | None = Field(default=None, ge=1)
    row_range: tuple[StrictInt, StrictInt]

    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_row_range(self) -> DocumentMapTableFragmentV1:
        if self.row_range[0] < 0 or self.row_range[1] <= self.row_range[0]:
            raise ValueError("document-map table row range must be non-empty")
        return self
    @model_validator(mode="after")
    def validate_original_location(self):
        validate_original_location(page=self.page, bbox=None, source_bounds=None, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="DocumentMapTableFragmentV1", require_geometry=False)
        return self



class DocumentMapLogicalTableV1(_StrictFrozenModel):
    scope_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    agent_display_label: str | None = Field(default=None, min_length=1, max_length=1000,
        exclude_if=lambda value: value is None)
    owner_unit_id: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=120)
    pages: tuple[StrictInt, ...] = Field(min_length=0, max_length=4096)
    fragments: tuple[DocumentMapTableFragmentV1, ...] = Field(min_length=1)
    source_numbering_anomaly: str = Field(default="", max_length=1000)
    capture_gap: str = Field(default="", max_length=1000)

    @field_validator("agent_display_label")
    @classmethod
    def validate_agent_display_label(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Agent table display label cannot be blank")
        return value


def document_map_table_presentation(table) -> dict[str, str]:
    """A display decision never replaces the original Map label or source facts.

    Empty legacy presentation keeps prior artifacts and reader payloads exact.
    """
    label = table.get("agent_display_label") if isinstance(table, dict) else table.agent_display_label
    if label is None:
        return {}
    title = table.get("title", "") if isinstance(table, dict) else table.title
    return {"display_title": label, "authority": "AGENT_DISPLAY_LABEL", "map_title": title}


class DocumentMapAcceptedDecisionV1(_StrictFrozenModel):
    proposal_id: str = Field(min_length=1, max_length=200)
    node_id: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=80)
    recommended_action: str = Field(default="", max_length=160)
    decision_id: str = Field(default="", max_length=200)


class DocumentMapArtifactV1(_StrictFrozenModel):
    schema_version: Literal["document-map-artifact/v1"] = (
        DOCUMENT_MAP_ARTIFACT_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=1)
    status: Literal["ACCEPTED"] = "ACCEPTED"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_format: Literal["docx", "xlsx"] | None = Field(default=None, exclude_if=lambda value: value is None)
    native_artifact_id: str = Field(min_length=1, max_length=200)
    native_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_step: Literal["DOCUMENT_STRUCTURE_RECOVERY"] = (
        "DOCUMENT_STRUCTURE_RECOVERY"
    )
    evidence_state: Literal["SUFFICIENT", "CONFLICTING", "INSUFFICIENT"]
    document_summary: str = Field(min_length=1, max_length=4000)
    root_node_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    nodes: tuple[DocumentMapNodeV1, ...] = Field(min_length=1, max_length=8192)
    logical_tables: tuple[DocumentMapLogicalTableV1, ...] = Field(
        default=(), max_length=4096
    )
    accepted_decisions: tuple[DocumentMapAcceptedDecisionV1, ...] = Field(
        default=(), max_length=8192
    )
    predecessor_receipt_refs: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_artifact(self) -> DocumentMapArtifactV1:
        expected_format = self.source_format or 'pdf'
        for node in self.nodes:
            if expected_format == 'pdf' and not node.pages:
                raise ValueError('PDF document-map nodes require actual pages')
            if expected_format != 'pdf' and node.pages:
                raise ValueError('Native Office document-map nodes cannot claim PDF pages')
            for value in (*node.source_content, *node.source_anchors, *node.evidence_anchors):
                if original_format(value.native_locator) != expected_format:
                    raise ValueError('Document-map locator format differs from its original source')
        for table in self.logical_tables:
            if expected_format == 'pdf' and not table.pages or expected_format != 'pdf' and table.pages:
                raise ValueError('Logical-table pages differ from the original source format')
            if any(original_format(fragment.native_locator) != expected_format for fragment in table.fragments):
                raise ValueError('Logical-table locator format differs from the original source')
        node_ids = tuple(item.node_id for item in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("document-map node IDs must be unique")
        node_set = set(node_ids)
        if not set(self.root_node_ids).issubset(node_set):
            raise ValueError("document-map roots must resolve")
        for node in self.nodes:
            if node.parent_id is not None and node.parent_id not in node_set:
                raise ValueError("document-map parent must resolve")
        table_ids = tuple(item.scope_id for item in self.logical_tables)
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("document-map logical table IDs must be unique")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"artifact_id", "artifact_sha256"})
        )
        if self.artifact_sha256 != expected:
            raise ValueError("document-map artifact SHA drifted")
        if self.artifact_id != f"document-map-{expected[:32]}":
            raise ValueError("document-map artifact ID drifted")
        return self


class SourceTableCellV1(_StrictFrozenModel):
    cell_id: str = Field(min_length=1, max_length=200)
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(ge=1)
    column_span: StrictInt = Field(ge=1)
    text: str = Field(max_length=1_000_000)
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None

    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_cell(self) -> SourceTableCellV1:
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.bbox, subject="source table cell")
        return self
    @model_validator(mode="after")
    def validate_original_location(self):
        if self.native_locator is not None:
            validate_original_location(page=None, bbox=self.bbox, source_bounds=None, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="source table cell")
        return self



class SourceTableFragmentV1(_StrictFrozenModel):
    fragment_id: str = Field(min_length=1, max_length=200)
    table_token: str = Field(min_length=1, max_length=200)
    page: StrictInt | None = Field(default=None, ge=1)
    row_range: tuple[StrictInt, StrictInt]
    row_count: StrictInt = Field(ge=1)
    column_count: StrictInt = Field(ge=1)
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    source_bounds: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    cells: tuple[SourceTableCellV1, ...] = Field(min_length=1, max_length=65536)

    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_fragment(self) -> SourceTableFragmentV1:
        if self.row_range[1] <= self.row_range[0]:
            raise ValueError("source table fragment range must be non-empty")
        if len({item.cell_id for item in self.cells}) != len(self.cells):
            raise ValueError("source table cell IDs must be unique")
        if any(item.row + item.row_span > self.row_count for item in self.cells):
            raise ValueError("source table cell row exceeds fragment")
        if any(
            item.column + item.column_span > self.column_count for item in self.cells
        ):
            raise ValueError("source table cell column exceeds fragment")
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.bbox, subject="source table fragment")
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.source_bounds, subject="source table bounds")
        return self
    @model_validator(mode="after")
    def validate_original_location(self):
        validate_original_location(page=self.page, bbox=self.bbox, source_bounds=self.source_bounds, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="SourceTableFragmentV1", require_geometry=True)
        validate_native_table_cells(self)
        return self



class SourceRequirementScopeRequestV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-scope-request/v1"] = (
        SOURCE_REQUIREMENT_SCOPE_REQUEST_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_id: str = Field(min_length=1, max_length=200)
    node_label: str = Field(default="", max_length=240)
    node_title: str = Field(min_length=1, max_length=1000)
    structural_role: Literal["COVER", "TABLE_OF_CONTENTS", "DOCUMENT_CONTROL"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    ancestor_path: tuple[str, ...] = Field(default=(), max_length=64)
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
    logical_table_title: str = Field(min_length=1, max_length=1000)
    logical_table_presentation: dict[str, str] | None = Field(default=None,
        exclude_if=lambda value: value is None)
    logical_table_role: str = Field(min_length=1, max_length=120)
    source_content: tuple[DocumentMapSourceContentV1, ...] = Field(default=())
    fragments: tuple[SourceTableFragmentV1, ...] = Field(min_length=1, max_length=512)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request(self) -> SourceRequirementScopeRequestV1:
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"request_sha256"})
        )
        if self.request_sha256 != expected:
            raise ValueError("source-requirement scope request SHA drifted")
        if len({item.fragment_id for item in self.fragments}) != len(self.fragments):
            raise ValueError("scope request fragments must be unique")
        return self


class SourceItemTextScopeRequestV1(_StrictFrozenModel):
    schema_version: Literal["source-item-text-scope-request/v1"] = (
        SOURCE_ITEM_TEXT_SCOPE_REQUEST_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    node_id: str = Field(min_length=1, max_length=200)
    node_label: str = Field(default="", max_length=240)
    node_title: str = Field(min_length=1, max_length=1000)
    structural_role: Literal["COVER", "TABLE_OF_CONTENTS", "DOCUMENT_CONTROL"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    ancestor_path: tuple[str, ...] = Field(default=(), max_length=64)
    source_content: tuple[DocumentMapSourceContentV1, ...] = Field(
        min_length=1, max_length=4096
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request(self) -> SourceItemTextScopeRequestV1:
        if len({item.content_id for item in self.source_content}) != len(
            self.source_content
        ):
            raise ValueError("source-item text content IDs must be unique")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"request_sha256"})
        )
        if self.request_sha256 != expected:
            raise ValueError("source-item text scope request SHA drifted")
        return self


class Requirementness(StrEnum):
    REQUIREMENT = "REQUIREMENT"
    NON_REQUIREMENT = "NON_REQUIREMENT"


class RequirementKind(StrEnum):
    FUNCTIONAL = "FUNCTIONAL"
    QUALITY = "QUALITY"
    CONSTRAINT = "CONSTRAINT"
    PROJECT = "PROJECT"
    PROCESS = "PROCESS"


class NonRequirementRole(StrEnum):
    HEADING = "HEADING"
    METADATA = "METADATA"
    REVISION_HISTORY = "REVISION_HISTORY"
    CONTEXT = "CONTEXT"
    DEFINITION = "DEFINITION"
    RATIONALE = "RATIONALE"
    EXAMPLE = "EXAMPLE"
    NOTE = "NOTE"
    REFERENCE = "REFERENCE"


class SourceRecordForm(StrEnum):
    NUMBERED_CLAUSE = "NUMBERED_CLAUSE"
    NATIVE_LIST = "NATIVE_LIST"
    PARAGRAPH = "PARAGRAPH"
    TABLE_RECORD = "TABLE_RECORD"
    TABLE_BRANCH = "TABLE_BRANCH"
    IMAGE = "IMAGE"


class ContextRowRole(StrEnum):
    FRONT_MATTER = "FRONT_MATTER"
    HEADER = "HEADER"
    METADATA = "METADATA"
    CONTEXT = "CONTEXT"
    NOTE = "NOTE"


class SourceItemScopeRevisionOperation(StrEnum):
    REPLACE = "REPLACE"
    UNDO = "UNDO"


class SourceItemTextSpanV1(_StrictFrozenModel):
    content_id: str = Field(min_length=1, max_length=200)
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_span(self) -> SourceItemTextSpanV1:
        if self.end_offset <= self.start_offset:
            raise ValueError("source-item text span must be non-empty and half-open")
        return self


class SourceItemTextBoundaryCandidateV1(_StrictFrozenModel):
    item_key: str = Field(
        pattern=r"^[a-z][a-z0-9._:-]{0,199}$",
        description=(
            "Unique lowercase ASCII machine key. Put source-language text in label, "
            "never in item_key."
        ),
    )
    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    text_spans: tuple[SourceItemTextSpanV1, ...] = Field(min_length=1, max_length=4096)
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)
    requirementness: Requirementness
    requirement_kinds: tuple[RequirementKind, ...] = Field(default=(), max_length=5)
    non_requirement_role: NonRequirementRole | None = None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @field_validator("requirement_kinds")
    @classmethod
    def validate_kinds(
        cls, value: tuple[RequirementKind, ...]
    ) -> tuple[RequirementKind, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("source-item requirement kinds must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_item(self) -> SourceItemTextBoundaryCandidateV1:
        if self.source_form not in {
            SourceRecordForm.NUMBERED_CLAUSE,
            SourceRecordForm.NATIVE_LIST,
            SourceRecordForm.PARAGRAPH,
        }:
            raise ValueError("text source items require a textual source form")
        if self.requirementness is Requirementness.REQUIREMENT:
            if not self.requirement_kinds or self.non_requirement_role is not None:
                raise ValueError("A=REQUIREMENT requires B and forbids F")
        elif self.requirement_kinds or self.non_requirement_role is None:
            raise ValueError("A=NON_REQUIREMENT requires F and forbids B")
        if not math.isfinite(self.confidence):
            raise ValueError("source-item confidence must be finite")
        if len(self.shared_source_content_ids) != len(
            set(self.shared_source_content_ids)
        ):
            raise ValueError("shared source content IDs must be unique")
        return self


class SourceItemStructureOnlySpanV1(_StrictFrozenModel):
    text_span: SourceItemTextSpanV1
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)


class SourceItemTextSliceCandidateV1(_StrictFrozenModel):
    schema_version: Literal["source-item-text-slice-candidate/v1"] = (
        SOURCE_ITEM_TEXT_SLICE_CANDIDATE_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    items: tuple[SourceItemTextBoundaryCandidateV1, ...] = Field(
        default=(), max_length=8192
    )
    structure_only_spans: tuple[SourceItemStructureOnlySpanV1, ...] = Field(
        default=(), max_length=4096
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value, allow_empty=True)

    @model_validator(mode="after")
    def validate_candidate(self) -> SourceItemTextSliceCandidateV1:
        keys = tuple(item.item_key for item in self.items)
        if len(keys) != len(set(keys)):
            raise ValueError("source-item keys must be unique")
        if not self.items and not self.structure_only_spans:
            raise ValueError("source-item text scope must disposition content")
        return self


class SourceRequirementRowGroupV1(_StrictFrozenModel):
    fragment_id: str = Field(min_length=1, max_length=200)
    row_start: StrictInt = Field(ge=0)
    row_end: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_group(self) -> SourceRequirementRowGroupV1:
        if self.row_end <= self.row_start:
            raise ValueError("source requirement row group must be non-empty")
        return self


class SourceRequirementContextRowGroupV1(_StrictFrozenModel):
    exact_placeholder_values: tuple[str, ...] = Field(default=(), max_length=32, exclude_if=lambda value: not value)
    empty_template_reason: str = Field(default='', max_length=1000, exclude_if=lambda value: not value)
    role: ContextRowRole
    row_group: SourceRequirementRowGroupV1
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)


    @model_validator(mode="after")
    def validate_empty_template_declaration(self):
        if self.exact_placeholder_values:
            if self.role is ContextRowRole.HEADER or not self.empty_template_reason.strip():
                raise ValueError("Placeholder values require non-header context and an explicit source reason")
            if len(set(self.exact_placeholder_values)) != len(self.exact_placeholder_values) or any(not value.strip() or len(value) > 80 for value in self.exact_placeholder_values):
                raise ValueError("Placeholder values must be distinct exact nonempty short source values")
        elif self.empty_template_reason:
            raise ValueError("An empty-template reason must accompany exact placeholder values")
        return self



class SourceRequirementBoundaryCandidateV1(_StrictFrozenModel):
    record_key: str = Field(
        pattern=r"^[a-z][a-z0-9._:-]{0,199}$",
        description=(
            "Unique lowercase ASCII machine key. Put source-language text in label, "
            "never in record_key."
        ),
    )
    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    row_groups: tuple[SourceRequirementRowGroupV1, ...] = Field(
        min_length=1, max_length=512
    )
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)
    requirementness: Requirementness
    requirement_kinds: tuple[RequirementKind, ...] = Field(default=(), max_length=5)
    non_requirement_role: NonRequirementRole | None = None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @field_validator("requirement_kinds")
    @classmethod
    def validate_kinds(
        cls, value: tuple[RequirementKind, ...]
    ) -> tuple[RequirementKind, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("requirement kinds must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_abf(self) -> SourceRequirementBoundaryCandidateV1:
        if self.requirementness is Requirementness.REQUIREMENT:
            if not self.requirement_kinds or self.non_requirement_role is not None:
                raise ValueError("A=REQUIREMENT requires B and forbids F")
        elif self.requirement_kinds or self.non_requirement_role is None:
            raise ValueError("A=NON_REQUIREMENT requires F and forbids B")
        if not math.isfinite(self.confidence):
            raise ValueError("source requirement confidence must be finite")
        if len(self.shared_source_content_ids) != len(
            set(self.shared_source_content_ids)
        ):
            raise ValueError("shared source content IDs must be unique")
        return self


class SourceRequirementSliceCandidateV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-slice-candidate/v1"] = (
        SOURCE_REQUIREMENT_SLICE_CANDIDATE_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
    records: tuple[SourceRequirementBoundaryCandidateV1, ...] = Field(
        max_length=8192
    )
    context_row_groups: tuple[SourceRequirementContextRowGroupV1, ...] = Field(
        default=(), max_length=1024
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value, allow_empty=True)

    @model_validator(mode="after")
    def validate_candidate(self) -> SourceRequirementSliceCandidateV1:
        keys = tuple(item.record_key for item in self.records)
        if len(keys) != len(set(keys)):
            raise ValueError("source requirement record keys must be unique")
        return self


class SourceRequirementTableFragmentBindingV1(_StrictFrozenModel):
    fragment_id: str = Field(min_length=1, max_length=200)
    table_token: str = Field(min_length=1, max_length=200)
    page: StrictInt | None = Field(default=None, ge=1)
    row_start: StrictInt = Field(ge=0)
    row_end: StrictInt = Field(gt=0)
    cell_ids: tuple[str, ...] = Field(min_length=1, max_length=65536)
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    source_bounds: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    @model_validator(mode="after")
    def validate_original_location(self):
        validate_original_location(page=self.page, bbox=self.bbox, source_bounds=self.source_bounds, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="SourceRequirementTableFragmentBindingV1", require_geometry=True)
        return self



class SourceItemTextBindingV1(_StrictFrozenModel):
    content_id: str = Field(min_length=1, max_length=200)
    page: StrictInt | None = Field(default=None, ge=1)
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(gt=0)
    text: str = Field(min_length=1, max_length=1_000_000)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    source_bounds: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    precision: str = Field(min_length=1, max_length=80)

    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_binding(self) -> SourceItemTextBindingV1:
        if self.end_offset <= self.start_offset:
            raise ValueError("source-item text binding must be half-open")
        if canonical_text_sha256(self.text) != self.text_sha256:
            raise ValueError("source-item text binding hash drifted")
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.bbox, subject="source-item text binding")
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.source_bounds, subject="source-item text source bounds")
        return self
    @model_validator(mode="after")
    def validate_original_location(self):
        validate_original_location(page=self.page, bbox=self.bbox, source_bounds=self.source_bounds, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="SourceItemTextBindingV1", require_geometry=True)
        return self



class SourceImageBindingV1(_StrictFrozenModel):
    """Original pixels located by an accepted Document Map, never OCR prose."""

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor_id: str = Field(min_length=1, max_length=200)
    source_handle: str = Field(default="", max_length=240)
    asset_id: str = Field(default="", max_length=200)
    page: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    source_bounds: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None
    precision: str = Field(min_length=1, max_length=80)

    content_sha256: str = Field(default='', pattern=r'^$|^[0-9a-f]{64}$', exclude_if=lambda value: not value)
    native_locator: NativeLocator | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sequence: StrictInt | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_geometry(self) -> SourceImageBindingV1:
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.bbox, subject="source image binding")
        if original_format(self.native_locator) == "pdf":
            _require_bbox(self.source_bounds, subject="source image bounds")
        return self
    @model_validator(mode="after")
    def validate_original_location(self):
        if original_format(self.native_locator) != 'pdf' and not self.content_sha256:
            raise ValueError('Native Office image requires its original embedded asset hash')
        validate_original_location(page=self.page, bbox=self.bbox, source_bounds=self.source_bounds, native_locator=self.native_locator, source_sequence=self.source_sequence, subject="SourceImageBindingV1", require_geometry=True)
        return self



class SourceItemTableImageRelationV1(_StrictFrozenModel):
    """Original Map pixels already owned by cells in an existing TABLE SRC."""
    node_id: str = Field(min_length=1, max_length=200)
    image_binding: SourceImageBindingV1
    table_source_record_id: str = Field(pattern=r"^source-record-[0-9a-f]{32}$")
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
    fragment_id: str = Field(min_length=1, max_length=200)
    cell_ids: tuple[str, ...] = Field(min_length=1, max_length=65536)


class SourceRequirementRecordV1(_StrictFrozenModel):
    source_record_id: str = Field(pattern=r"^source-record-[0-9a-f]{32}$")
    source_display_id: str = Field(pattern=r"^SRC-[0-9]{4,}$")
    requirement_display_id: str | None = Field(default=None, pattern=r"^REQ-[0-9]{4,}$")
    source_order: StrictInt = Field(ge=0)
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_id: str = Field(min_length=1, max_length=200)
    ancestor_path: tuple[str, ...] = Field(default=(), max_length=64)
    logical_table_scope_id: str = Field(default="", max_length=200)
    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    table_fragments: tuple[SourceRequirementTableFragmentBindingV1, ...] = Field(
        default=(), max_length=512
    )
    text_bindings: tuple[SourceItemTextBindingV1, ...] = Field(
        default=(), max_length=4096
    )
    image_bindings: tuple[SourceImageBindingV1, ...] = Field(
        default=(), max_length=512, exclude_if=lambda value: not value
    )
    shared_header_cell_ids: tuple[str, ...] = Field(default=(), max_length=65536)
    shared_table_cell_ids: tuple[str, ...] = Field(default=(), max_length=65536)
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)
    pages: tuple[StrictInt, ...] = Field(min_length=0, max_length=4096)
    requirementness: Requirementness
    requirement_kinds: tuple[RequirementKind, ...] = Field(default=(), max_length=5)
    non_requirement_role: NonRequirementRole | None = None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    evidence_state: Literal[
        "SUFFICIENT", "CONFLICTING", "INSUFFICIENT", "BLOCKED_INPUT"
    ]
    decision_origin: Literal["AGENT_DEFAULT", "USER_OVERRIDE"] = "AGENT_DEFAULT"
    review_state: Literal["UNREVIEWED", "REVIEWED"] = "UNREVIEWED"

    @model_validator(mode="after")
    def validate_record(self) -> SourceRequirementRecordV1:
        bindings = (*self.table_fragments, *self.text_bindings, *self.image_bindings)
        formats = {original_format(binding.native_locator) for binding in bindings}
        if len(formats) > 1:
            raise ValueError('Source item cannot mix original locator formats')
        if formats == {'pdf'} and not self.pages or formats and formats != {'pdf'} and self.pages:
            raise ValueError('Source item pages differ from its original locator format')
        if sum(bool(value) for value in (self.table_fragments, self.text_bindings, self.image_bindings)) != 1:
            raise ValueError("compiled source item requires exactly one of table, text or image bindings")
        if self.table_fragments and not self.logical_table_scope_id:
            raise ValueError("table source item requires a logical table scope")
        if (self.text_bindings or self.image_bindings) and self.logical_table_scope_id:
            raise ValueError("non-table source item cannot claim a logical table scope")
        if bool(self.image_bindings) != (self.source_form is SourceRecordForm.IMAGE):
            raise ValueError("IMAGE form and original image bindings must occur together")
        if len({binding.anchor_id for binding in self.image_bindings}) != len(self.image_bindings):
            raise ValueError("source image anchors must be unique within an item")
        if self.requirementness is Requirementness.REQUIREMENT:
            if (
                not self.requirement_kinds
                or self.non_requirement_role is not None
                or self.requirement_display_id is None
            ):
                raise ValueError("compiled requirement violates A/B/F or REQ identity")
        elif (
            self.requirement_kinds
            or self.non_requirement_role is None
            or self.requirement_display_id is not None
        ):
            raise ValueError("compiled non-requirement violates A/B/F or REQ identity")
        return self


class SourceRequirementContextDispositionV1(_StrictFrozenModel):
    exact_placeholder_values: tuple[str, ...] = Field(default=(), max_length=32, exclude_if=lambda value: not value)
    empty_template_reason: str = Field(default='', max_length=1000, exclude_if=lambda value: not value)
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
    role: ContextRowRole
    fragment_id: str = Field(min_length=1, max_length=200)
    row_start: StrictInt = Field(ge=0)
    row_end: StrictInt = Field(gt=0)
    cell_ids: tuple[str, ...] = Field(min_length=1, max_length=65536)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


    @model_validator(mode="after")
    def validate_empty_template_declaration(self):
        if self.exact_placeholder_values:
            if self.role is ContextRowRole.HEADER or not self.empty_template_reason.strip():
                raise ValueError("Placeholder values require non-header context and an explicit source reason")
            if len(set(self.exact_placeholder_values)) != len(self.exact_placeholder_values) or any(not value.strip() or len(value) > 80 for value in self.exact_placeholder_values):
                raise ValueError("Placeholder values must be distinct exact nonempty short source values")
        elif self.empty_template_reason:
            raise ValueError("An empty-template reason must accompany exact placeholder values")
        return self



class SourceItemTextContextDispositionV1(_StrictFrozenModel):
    scope_id: str = Field(pattern=r"^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    node_id: str = Field(min_length=1, max_length=200)
    content_id: str = Field(min_length=1, max_length=200)
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(gt=0)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


class SourceRequirementBaselineArtifactV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-baseline-artifact/v1"] = (
        SOURCE_REQUIREMENT_BASELINE_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^source-requirement-baseline-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=1)
    status: Literal["EMPTY", "SLICE_COMPLETE", "COMPLETE"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(
        default="", pattern=r"^$|^source-requirement-baseline-[0-9a-f]{32}$"
    )
    predecessor_artifact_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    covered_scope_ids: tuple[str, ...] = Field(max_length=4096)
    records: tuple[SourceRequirementRecordV1, ...] = Field(max_length=65536)
    source_display_high_watermark: StrictInt | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    table_image_relations: tuple[SourceItemTableImageRelationV1, ...] = Field(
        default=(), max_length=16384, exclude_if=lambda value: not value
    )
    context_dispositions: tuple[SourceRequirementContextDispositionV1, ...] = Field(
        default=(), max_length=8192
    )
    text_context_dispositions: tuple[SourceItemTextContextDispositionV1, ...] = Field(
        default=(), max_length=8192
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_artifact(self) -> SourceRequirementBaselineArtifactV1:
        if self.status == "EMPTY":
            if self.records or self.covered_scope_ids or self.context_dispositions or self.text_context_dispositions:
                raise ValueError("EMPTY baseline cannot claim processed source content")
        elif not self.covered_scope_ids:
            raise ValueError("processed baseline must identify covered source scopes")
        if self.revision == 1:
            if self.predecessor_artifact_id or self.predecessor_artifact_sha256:
                raise ValueError(
                    "initial source-requirement baseline forbids predecessor"
                )
        elif not self.predecessor_artifact_id or not self.predecessor_artifact_sha256:
            raise ValueError("revised source-requirement baseline requires predecessor")
        if self.covered_scope_ids != tuple(sorted(set(self.covered_scope_ids))):
            raise ValueError("covered source-requirement scopes must be sorted")
        record_ids = tuple(item.source_record_id for item in self.records)
        source_ids = tuple(item.source_display_id for item in self.records)
        requirement_ids = tuple(
            item.requirement_display_id
            for item in self.records
            if item.requirement_display_id is not None
        )
        if (
            len(record_ids) != len(set(record_ids))
            or len(source_ids) != len(set(source_ids))
            or len(requirement_ids) != len(set(requirement_ids))
        ):
            raise ValueError("source-requirement identities must be unique")
        if (self.source_display_high_watermark is not None
                and self.source_display_high_watermark < max([0, *[int(value.removeprefix("SRC-")) for value in source_ids]])):
            raise ValueError("source display high watermark cannot precede an issued SRC/REQ identity")
        if tuple(item.source_order for item in self.records) != tuple(
            range(len(self.records))
        ):
            raise ValueError("source-requirement order must be contiguous")
        expected = canonical_json_sha256(_baseline_hash_payload(self))
        if self.artifact_sha256 != expected:
            raise ValueError("source-requirement baseline SHA drifted")
        if self.artifact_id != f"source-requirement-baseline-{expected[:32]}":
            raise ValueError("source-requirement baseline ID drifted")
        return self


class SourceRequirementBaselineValidationReceiptV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-baseline-validation-receipt/v1"] = (
        SOURCE_REQUIREMENT_VALIDATION_RECEIPT_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^source-requirement-baseline-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_count: StrictInt = Field(ge=0)
    record_count: StrictInt = Field(ge=0)
    requirement_count: StrictInt = Field(ge=0)
    non_requirement_count: StrictInt = Field(ge=0)
    owned_cell_count: StrictInt = Field(ge=0)
    context_cell_count: StrictInt = Field(ge=0)
    shared_cell_count: StrictInt = Field(ge=0)
    owned_text_character_count: StrictInt = Field(default=0, ge=0)
    context_text_character_count: StrictInt = Field(default=0, ge=0)
    owned_image_count: StrictInt = Field(default=0, ge=0, exclude_if=lambda value: not value)
    coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_id: Literal["reqpilotx-source-requirement-baseline-validator"] = (
        "reqpilotx-source-requirement-baseline-validator"
    )
    validator_version: Literal["1.0.0"] = "1.0.0"

    @model_validator(mode="after")
    def validate_receipt(self) -> SourceRequirementBaselineValidationReceiptV1:
        if self.requirement_count + self.non_requirement_count != self.record_count:
            raise ValueError("source-requirement receipt classification counts drifted")
        expected = canonical_json_sha256(_receipt_hash_payload(self))
        if self.receipt_sha256 != expected:
            raise ValueError("source-requirement receipt SHA drifted")
        return self


class SourceRequirementBaselineAppendReceiptV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-baseline-append-receipt/v1"] = (
        "source-requirement-baseline-append-receipt/v1"
    )
    predecessor_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^source-requirement-baseline-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preserved_record_count: StrictInt = Field(ge=0)
    added_record_count: StrictInt = Field(ge=0)
    added_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=4096)
    preserved_records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    added_records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    combined_coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> SourceRequirementBaselineAppendReceiptV1:
        if self.added_scope_ids != tuple(sorted(set(self.added_scope_ids))):
            raise ValueError("append receipt scope IDs must be sorted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("append receipt SHA drifted")
        return self


class SourceRequirementBaselineGrowthReceiptV1(_StrictFrozenModel):
    """Proof that a new earlier scope only reordered predecessor source_order."""

    schema_version: Literal["source-requirement-baseline-growth-receipt/v1"] = (
        SOURCE_REQUIREMENT_GROWTH_RECEIPT_SCHEMA_VERSION
    )
    growth_mode: Literal["SOURCE_ORDER_INSERT"] = "SOURCE_ORDER_INSERT"
    predecessor_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^source-requirement-baseline-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preserved_record_count: StrictInt = Field(ge=0)
    added_record_count: StrictInt = Field(ge=0)
    added_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=4096)
    predecessor_stable_records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    successor_stable_records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    added_records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    combined_coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> SourceRequirementBaselineGrowthReceiptV1:
        if (
            self.added_scope_ids != tuple(sorted(set(self.added_scope_ids)))
            or self.predecessor_stable_records_sha256
            != self.successor_stable_records_sha256
        ):
            raise ValueError("growth receipt predecessor records drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("growth receipt SHA drifted")
        return self


class SourceItemScopeReplacementBindingV1(_StrictFrozenModel):
    scope_id: str = Field(min_length=1, max_length=205)
    scope_kind: Literal["TABLE", "TEXT"]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceItemRevisionIdentityV1(_StrictFrozenModel):
    source_record_id: str = Field(pattern=r"^source-record-[0-9a-f]{32}$")
    source_display_id: str = Field(pattern=r"^SRC-[0-9]{4,}$")
    requirement_display_id: str | None = Field(default=None, pattern=r"^REQ-[0-9]{4,}$")
    scope_id: str = Field(min_length=1, max_length=205)
    exact_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> SourceItemRevisionIdentityV1:
        if self.requirement_display_id is not None and (
            self.requirement_display_id.removeprefix("REQ-")
            != self.source_display_id.removeprefix("SRC-")
        ):
            raise ValueError("scope-revision REQ identity must share the SRC suffix")
        return self


class SourceItemSuccessorRelationV1(_StrictFrozenModel):
    predecessor_source_record_id: str = Field(pattern=r"^source-record-[0-9a-f]{32}$")
    successor_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    relation: Literal["SPLIT", "MERGED", "REPLACED", "STRUCTURE_ONLY", "UNDO"]

    @field_validator("successor_ids")
    @classmethod
    def validate_successor_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("scope-revision successor IDs must be unique and sorted")
        for item in value:
            if re.fullmatch(r"source-record-[0-9a-f]{32}", item) is not None:
                continue
            if re.fullmatch(r"STRUCTURE_NODE:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", item):
                continue
            raise ValueError("scope-revision successor ID is invalid")
        return value


class SourceItemScopeRevisionV1(_StrictFrozenModel):
    """CAS-bound request to replace only the declared SourceItem scopes.

    In v1, ``UNDO`` is deliberately limited to the current artifact's direct
    predecessor. The compiler proves that ancestry from the predecessor identity
    embedded in the current artifact instead of trusting source/map equality.
    """

    schema_version: Literal["source-item-scope-revision/v1"] = (
        SOURCE_ITEM_SCOPE_REVISION_SCHEMA_VERSION
    )
    operation: SourceItemScopeRevisionOperation
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    current_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_artifact_revision: StrictInt = Field(ge=1)
    current_validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    affected_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=4096)
    comparison_scope_ids: tuple[str, ...] = Field(default=(), max_length=4096)
    feedback_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    replacement_scope_bindings: tuple[SourceItemScopeReplacementBindingV1, ...] = Field(
        default=(), max_length=4096
    )
    source_display_id_high_watermark: StrictInt = Field(ge=1)
    undo_target_artifact_id: str = Field(
        default="", pattern=r"^$|^source-requirement-baseline-[0-9a-f]{32}$"
    )
    undo_target_artifact_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    undo_target_artifact_revision: StrictInt | None = Field(default=None, ge=1)
    revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_revision(self) -> SourceItemScopeRevisionV1:
        for name, value in (
            ("affected", self.affected_scope_ids),
            ("comparison", self.comparison_scope_ids),
            ("feedback", self.feedback_ids),
        ):
            if value != tuple(sorted(set(value))):
                raise ValueError(f"scope-revision {name} IDs must be unique and sorted")
        if set(self.affected_scope_ids).intersection(self.comparison_scope_ids):
            raise ValueError("comparison scopes must remain read-only")
        binding_scope_ids = tuple(
            item.scope_id for item in self.replacement_scope_bindings
        )
        if binding_scope_ids != tuple(sorted(set(binding_scope_ids))):
            raise ValueError("scope-revision replacement bindings must be sorted")
        if self.operation is SourceItemScopeRevisionOperation.REPLACE:
            if binding_scope_ids != self.affected_scope_ids:
                raise ValueError("REPLACE must bind every affected scope exactly once")
            if (
                self.undo_target_artifact_id
                or self.undo_target_artifact_sha256
                or self.undo_target_artifact_revision is not None
            ):
                raise ValueError("REPLACE forbids an undo target")
        else:
            if self.replacement_scope_bindings:
                raise ValueError("UNDO forbids replacement candidates")
            if (
                not self.undo_target_artifact_id
                or not self.undo_target_artifact_sha256
                or self.undo_target_artifact_revision is None
            ):
                raise ValueError("UNDO requires an immutable target artifact")
            if self.undo_target_artifact_revision != self.current_artifact_revision - 1:
                raise ValueError("UNDO target must be the direct predecessor revision")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"revision_sha256"})
        )
        if self.revision_sha256 != expected:
            raise ValueError("scope-revision SHA drifted")
        return self


class SourceItemScopeRevisionReceiptV1(_StrictFrozenModel):
    schema_version: Literal["source-item-scope-revision-receipt/v1"] = (
        SOURCE_ITEM_SCOPE_REVISION_RECEIPT_SCHEMA_VERSION
    )
    operation: SourceItemScopeRevisionOperation
    scope_revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_revision: StrictInt = Field(ge=1)
    artifact_id: str = Field(pattern=r"^source-requirement-baseline-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_revision: StrictInt = Field(ge=2)
    predecessor_validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    affected_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=4096)
    comparison_scope_ids: tuple[str, ...] = Field(default=(), max_length=4096)
    feedback_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    unaffected_stable_records_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unaffected_stable_records_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    affected_partition_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    affected_partition_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    matched_identities: tuple[SourceItemRevisionIdentityV1, ...] = Field(default=())
    created_identities: tuple[SourceItemRevisionIdentityV1, ...] = Field(default=())
    tombstoned_identities: tuple[SourceItemRevisionIdentityV1, ...] = Field(default=())
    superseded_identities: tuple[SourceItemRevisionIdentityV1, ...] = Field(default=())
    successor_relations: tuple[SourceItemSuccessorRelationV1, ...] = Field(default=())
    source_display_id_high_watermark_before: StrictInt = Field(ge=1)
    source_display_id_high_watermark_after: StrictInt = Field(ge=1)
    coverage_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    undo_target_artifact_id: str = Field(
        default="", pattern=r"^$|^source-requirement-baseline-[0-9a-f]{32}$"
    )
    undo_target_artifact_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    undo_target_artifact_revision: StrictInt | None = Field(default=None, ge=1)
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> SourceItemScopeRevisionReceiptV1:
        if self.artifact_revision != self.predecessor_artifact_revision + 1:
            raise ValueError("scope-revision receipt revision is not monotonic")
        if (
            self.unaffected_stable_records_before_sha256
            != self.unaffected_stable_records_after_sha256
        ):
            raise ValueError("scope-revision receipt changed unaffected records")
        if (
            self.source_display_id_high_watermark_after
            < self.source_display_id_high_watermark_before
        ):
            raise ValueError("scope-revision SRC high watermark decreased")
        for value in (
            self.affected_scope_ids,
            self.comparison_scope_ids,
            self.feedback_ids,
        ):
            if value != tuple(sorted(set(value))):
                raise ValueError("scope-revision receipt IDs must be unique and sorted")
        if set(self.affected_scope_ids).intersection(self.comparison_scope_ids):
            raise ValueError("scope-revision receipt comparison scope was affected")
        identity_groups = (
            self.matched_identities,
            self.created_identities,
            self.tombstoned_identities,
            self.superseded_identities,
        )
        for identities in identity_groups:
            ids = tuple(item.source_record_id for item in identities)
            if ids != tuple(sorted(set(ids))):
                raise ValueError("scope-revision identity lists must be sorted")
        matched_ids = {item.source_record_id for item in self.matched_identities}
        created_ids = {item.source_record_id for item in self.created_identities}
        tombstoned_ids = {item.source_record_id for item in self.tombstoned_identities}
        superseded_ids = {item.source_record_id for item in self.superseded_identities}
        relation_ids = {
            item.predecessor_source_record_id for item in self.successor_relations
        }
        if tombstoned_ids != superseded_ids or tombstoned_ids != relation_ids:
            raise ValueError("every tombstone requires a canonical successor relation")
        if (
            matched_ids.intersection(created_ids)
            or matched_ids.intersection(tombstoned_ids)
            or created_ids.intersection(tombstoned_ids)
        ):
            raise ValueError("scope-revision identity dispositions must be disjoint")
        affected_scope_ids = set(self.affected_scope_ids)
        if any(
            item.scope_id not in affected_scope_ids
            for identities in identity_groups
            for item in identities
        ):
            raise ValueError("scope-revision identity escaped the affected scopes")
        active_successor_ids = matched_ids.union(created_ids)
        if any(
            successor_id.startswith("source-record-")
            and successor_id not in active_successor_ids
            for relation in self.successor_relations
            for successor_id in relation.successor_ids
        ):
            raise ValueError("scope-revision successor does not resolve")
        relation_order = tuple(
            item.predecessor_source_record_id for item in self.successor_relations
        )
        if relation_order != tuple(sorted(set(relation_order))):
            raise ValueError("scope-revision successor relations must be sorted")
        created_ordinals = tuple(
            sorted(
                _source_display_ordinal(item.source_display_id)
                for item in self.created_identities
            )
        )
        expected_created_ordinals = tuple(
            range(
                self.source_display_id_high_watermark_before + 1,
                self.source_display_id_high_watermark_after + 1,
            )
        )
        if created_ordinals != expected_created_ordinals:
            raise ValueError("scope-revision SRC high watermark is not append-only")
        if self.operation is SourceItemScopeRevisionOperation.REPLACE:
            if (
                self.undo_target_artifact_id
                or self.undo_target_artifact_sha256
                or self.undo_target_artifact_revision is not None
            ):
                raise ValueError("REPLACE receipt forbids an undo target")
        elif (
            not self.undo_target_artifact_id
            or not self.undo_target_artifact_sha256
            or self.undo_target_artifact_revision is None
        ):
            raise ValueError("UNDO receipt requires a target artifact")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("scope-revision receipt SHA drifted")
        return self


class SourceRequirementClassificationCoordinationDecisionV1(_StrictFrozenModel):
    source_record_id: str = Field(pattern=r"^source-record-[0-9a-f]{32}$")
    requirementness: Requirementness | None = None
    requirement_kinds: tuple[RequirementKind, ...] = Field(default=(), max_length=5)
    non_requirement_role: NonRequirementRole | None = None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("requirement_kinds")
    @classmethod
    def validate_kinds(
        cls, value: tuple[RequirementKind, ...]
    ) -> tuple[RequirementKind, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("coordinated requirement kinds must be unique and sorted")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @model_validator(mode="after")
    def validate_abf_target(
        self,
    ) -> SourceRequirementClassificationCoordinationDecisionV1:
        # Missing A preserves the original v1 B-only coordination contract.
        if self.requirementness is None:
            if not self.requirement_kinds or self.non_requirement_role is not None:
                raise ValueError(
                    "B-only coordination requires requirement kinds and forbids F"
                )
        elif self.requirementness is Requirementness.REQUIREMENT:
            if not self.requirement_kinds or self.non_requirement_role is not None:
                raise ValueError("A=REQUIREMENT requires B and forbids F")
        elif self.requirement_kinds or self.non_requirement_role is None:
            raise ValueError("A=NON_REQUIREMENT requires F and forbids B")
        return self


class SourceRequirementClassificationCoordinationV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-classification-coordination/v1"] = (
        SOURCE_REQUIREMENT_CLASSIFICATION_COORDINATION_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coordinator_role: Literal["CODEX_APP_DEVELOPMENT_COORDINATOR", "APP_SERVER_WORKING_AGENT", "PORTABLE_AGENT"] = (
        "CODEX_APP_DEVELOPMENT_COORDINATOR"
    )
    model_id: str = Field(min_length=1, max_length=160)
    method_version: Literal["1.0.0", "1.1.0"] = "1.1.0"
    decisions: tuple[SourceRequirementClassificationCoordinationDecisionV1, ...] = (
        Field(min_length=1, max_length=4096)
    )
    coordination_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_coordination(self) -> SourceRequirementClassificationCoordinationV1:
        record_ids = tuple(item.source_record_id for item in self.decisions)
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("classification coordination record IDs must be unique")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"coordination_sha256"})
        )
        if self.coordination_sha256 != expected:
            raise ValueError("classification coordination SHA drifted")
        return self


class SourceRequirementClassificationCoordinationReceiptV1(_StrictFrozenModel):
    schema_version: Literal[
        "source-requirement-classification-coordination-receipt/v1"
    ] = SOURCE_REQUIREMENT_CLASSIFICATION_COORDINATION_RECEIPT_SCHEMA_VERSION
    coordination_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^source-requirement-baseline-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_count: StrictInt = Field(ge=1)
    preserved_record_count: StrictInt = Field(ge=0)
    preserved_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(
        self,
    ) -> SourceRequirementClassificationCoordinationReceiptV1:
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("classification coordination receipt SHA drifted")
        return self


def compile_document_map_artifact(
    public_reference: dict[str, Any], *, revision: int = 1
) -> DocumentMapArtifactV1:
    if revision < 1:
        raise SourceRequirementBaselineError("DOCUMENT_MAP_REVISION_INVALID")
    if public_reference.get("schema_version") != "native-structure-reference-public/v1":
        raise SourceRequirementBaselineError("DOCUMENT_MAP_REFERENCE_SCHEMA_INVALID")
    source_sha256 = str(public_reference.get("source_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise SourceRequirementBaselineError("DOCUMENT_MAP_SOURCE_INVALID")
    tree = public_reference.get("document_tree")
    if not isinstance(tree, dict):
        raise SourceRequirementBaselineError("DOCUMENT_MAP_TREE_MISSING")
    raw_nodes = tree.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise SourceRequirementBaselineError("DOCUMENT_MAP_NODES_MISSING")
    raw_by_id = {str(item.get("node_id") or ""): item for item in raw_nodes}
    if "" in raw_by_id or len(raw_by_id) != len(raw_nodes):
        raise SourceRequirementBaselineError("DOCUMENT_MAP_NODE_ID_INVALID")

    def ancestor_path(node: dict[str, Any]) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        parent_id = str(node.get("parent_id") or "")
        while parent_id:
            if parent_id in seen or parent_id not in raw_by_id:
                raise SourceRequirementBaselineError(
                    "DOCUMENT_MAP_PARENT_GRAPH_INVALID"
                )
            seen.add(parent_id)
            parent = raw_by_id[parent_id]
            label = str(parent.get("label") or "").strip()
            title = str(parent.get("title") or "").strip()
            result.append(" ".join(item for item in (label, title) if item))
            parent_id = str(parent.get("parent_id") or "")
        return tuple(reversed(result))

    nodes = tuple(
        DocumentMapNodeV1(
            node_id=str(item["node_id"]),
            parent_id=str(item.get("parent_id") or "") or None,
            order=int(item.get("order") or 0),
            kind=str(item.get("kind") or "SECTION"),
            label=str(item.get("label") or ""),
            title=str(item.get("title") or "未命名原文节点"),
            title_authority=str(item.get("title_authority") or "SOURCE_VISIBLE"),
            title_reference_anchors=tuple(DocumentMapSourceAnchorV1.model_validate(value) for value in item.get('title_reference_anchors', [])),
            evidence_anchors=tuple(DocumentMapSourceAnchorV1.model_validate(value) for value in item.get('evidence_anchors', [])),
            ancestor_path=ancestor_path(item),
            pages=tuple(sorted({int(page) for page in item.get("pages", [])})),
            content_blocks=tuple(
                DocumentMapContentBlockV1(
                    kind=str(block.get("kind") or "SOURCE_CONTENT"),
                    logical_table_scope_id=str(
                        block.get("logical_table_scope_id") or ""
                    ),
                )
                for block in item.get("content_blocks", [])
            ),
            source_anchors=tuple(
                DocumentMapSourceAnchorV1(
                    anchor_id=str(anchor.get("anchor_id") or ""),
                    page=int(anchor["page"]) if anchor.get("page") is not None else None,
                    **native_location_fields(anchor),
                    bbox=tuple(anchor["bbox"]) if anchor.get("bbox") is not None else None,
                    source_bounds=tuple(anchor["source_bounds"]) if anchor.get("source_bounds") is not None else None,
                    anchor_kind=str(anchor.get("anchor_kind") or "SOURCE"),
                    precision=str(anchor.get("precision") or "EXACT"),
                    source_handle=str(anchor.get("source_handle") or ""),
                    source_text_sha256=str(anchor.get("source_text_sha256") or ""),
                    logical_table_scope_id=str(
                        anchor.get("logical_table_scope_id") or ""
                    ),
                    table_token=str(anchor.get("table_token") or ""),
                    row_range=(
                        tuple(int(value) for value in anchor["row_range"])
                        if isinstance(anchor.get("row_range"), list)
                        else None
                    ),
                    placement_id=str(anchor.get("placement_id") or ""),
                    asset_id=str(anchor.get("asset_id") or ""),
                    content_sha256=str(anchor.get("content_sha256") or ""),
                    source_text_range=tuple(anchor['source_text_range']) if anchor.get('source_text_range') is not None else None,
                )
                for anchor in item.get("source_anchors", [])
            ),
            source_content=tuple(
                DocumentMapSourceContentV1(
                    content_id=str(content.get("content_id") or ""),
                    page=int(content["page"]) if content.get("page") is not None else None,
                    **native_location_fields(content),
                    bbox=tuple(content["bbox"]) if content.get("bbox") is not None else None,
                    source_bounds=tuple(content["source_bounds"]) if content.get("source_bounds") is not None else None,
                    source_handles=tuple(
                        str(value) for value in content.get("source_handles", [])
                    ),
                    source_text_range=tuple(content['source_text_range']) if content.get('source_text_range') is not None else None,
                    text=str(content.get("text") or ""),
                    text_sha256=str(content.get("text_sha256") or ""),
                    precision=str(content.get("precision") or "EXACT"),
                    is_title=bool(content.get("is_title")),
                )
                for content in item.get("source_content", [])
            ),
            finding_codes=tuple(sorted(set(item.get("finding_codes", [])))),
        )
        for item in sorted(raw_nodes, key=lambda value: int(value.get("order") or 0))
    )
    logical_tables = tuple(
        DocumentMapLogicalTableV1(
            scope_id=str(item.get("scope_id") or ""),
            title=str(item.get("title") or "未命名原文表格"),
            agent_display_label=item.get("agent_display_label"),
            owner_unit_id=str(item.get("owner_unit_id") or "unknown-owner"),
            role=str(item.get("role") or "SOURCE_TABLE"),
            pages=tuple(sorted({int(page) for page in item.get("pages", [])})),
            fragments=tuple(
                DocumentMapTableFragmentV1(
                    table_token=str(fragment.get("table_token") or ""),
                    page=int(fragment["page"]) if fragment.get("page") is not None else None,
                    **native_location_fields(fragment),
                    row_range=tuple(
                        int(value) for value in fragment.get("row_range", ())
                    ),
                )
                for fragment in item.get("fragments", [])
            ),
            source_numbering_anomaly=str(item.get("source_numbering_anomaly") or ""),
            capture_gap=str(item.get("capture_gap") or ""),
        )
        for item in public_reference.get("logical_tables", [])
    )
    decisions = tuple(
        DocumentMapAcceptedDecisionV1(
            proposal_id=str(item.get("proposal_id") or ""),
            node_id=str(item.get("node_id") or ""),
            status=str(item.get("status") or "PROPOSED_CONTINUABLE"),
            recommended_action=str(item.get("recommended_action") or ""),
            decision_id=str((item.get("decision") or {}).get("decision_id") or ""),
        )
        for item in public_reference.get("structure_proposals", [])
    )
    projection_sha256 = canonical_json_sha256(
        {
            "document_tree": tree,
            "logical_tables": public_reference.get("logical_tables", []),
            "structure_proposals": public_reference.get("structure_proposals", []),
        }
    )
    continuation = public_reference.get("continuation")
    continuation = continuation if isinstance(continuation, dict) else {}
    evidence_state = str(continuation.get("evidence_state") or "SUFFICIENT")
    if evidence_state not in {"SUFFICIENT", "CONFLICTING", "INSUFFICIENT"}:
        evidence_state = "INSUFFICIENT"
    values = {
        "revision": revision,
        "source_sha256": source_sha256,
        **({"source_format": public_reference["source_format"]} if public_reference.get("source_format") else {}),
        "native_artifact_id": str(public_reference.get("artifact_id") or ""),
        "native_projection_sha256": projection_sha256,
        "evidence_state": evidence_state,
        "document_summary": (
            f"已接受 {len(nodes)} 个原文结构节点、{len(logical_tables)} 个逻辑表；"
            "标题、直接正文、表格、图片与用户决策保持可追踪。"
        ),
        "root_node_ids": tuple(str(value) for value in tree.get("root_ids", [])),
        "nodes": nodes,
        "logical_tables": logical_tables,
        "accepted_decisions": decisions,
        "predecessor_receipt_refs": tuple(
            sorted(
                {
                    str(
                        public_reference.get("validation", {}).get("receipt_sha256")
                        or ""
                    ),
                    str(public_reference.get("artifact_id") or ""),
                }
                - {""}
            )
        ),
    }
    draft = DocumentMapArtifactV1.model_construct(
        **values,
        artifact_id="document-map-" + "0" * 32,
        artifact_sha256="0" * 64,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"artifact_id", "artifact_sha256"})
    )
    return DocumentMapArtifactV1(
        **values,
        artifact_id=f"document-map-{digest[:32]}",
        artifact_sha256=digest,
    )


def build_source_requirement_scope_request(
    document_map: DocumentMapArtifactV1,
    table_preview: dict[str, Any],
) -> SourceRequirementScopeRequestV1:
    scope_id = str(table_preview.get("logical_table_scope_id") or "")
    table = next(
        (item for item in document_map.logical_tables if item.scope_id == scope_id),
        None,
    )
    if table is None:
        raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_TABLE_NOT_IN_MAP")
    node = next(
        (
            item
            for item in document_map.nodes
            if any(
                block.logical_table_scope_id == scope_id
                for block in item.content_blocks
            )
        ),
        None,
    )
    if node is None:
        raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_TABLE_OWNER_MISSING")
    fragments = tuple(
        SourceTableFragmentV1.model_validate(fragment)
        for fragment in table_preview.get("fragments", [])
    )
    values = {
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "node_id": node.node_id,
        "node_label": node.label,
        "node_title": node.title,
        **({"structural_role": node.kind} if node.kind in FRONT_MATTER_KINDS else {}),
        "ancestor_path": node.ancestor_path,
        "logical_table_scope_id": scope_id,
        "logical_table_title": document_map_table_presentation(table).get("display_title", table.title),
        **({"logical_table_presentation": document_map_table_presentation(table)} if table.agent_display_label is not None else {}),
        "logical_table_role": table.role,
        "source_content": node.source_content,
        "fragments": fragments,
    }
    draft = SourceRequirementScopeRequestV1.model_construct(
        **values,
        request_sha256="0" * 64,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"request_sha256"})
    )
    return SourceRequirementScopeRequestV1(**values, request_sha256=digest)


def build_source_item_text_scope_request(
    document_map: DocumentMapArtifactV1,
    *,
    node_id: str,
    content_ids: tuple[str, ...] | None = None,
) -> SourceItemTextScopeRequestV1:
    node = next((item for item in document_map.nodes if item.node_id == node_id), None)
    if node is None:
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_NODE_NOT_IN_MAP")
    selected_ids = set(content_ids or ())
    source_content = tuple(
        item
        for item in node.source_content
        if not selected_ids or item.content_id in selected_ids
    )
    if not source_content:
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SCOPE_EMPTY")
    if selected_ids and {item.content_id for item in source_content} != selected_ids:
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_CONTENT_NOT_IN_NODE")
    values = {
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "scope_id": f"text:{node.node_id}",
        "node_id": node.node_id,
        "node_label": node.label,
        "node_title": node.title,
        **({"structural_role": node.kind} if node.kind in FRONT_MATTER_KINDS else {}),
        "ancestor_path": node.ancestor_path,
        "source_content": source_content,
    }
    draft = SourceItemTextScopeRequestV1.model_construct(
        **values,
        request_sha256="0" * 64,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"request_sha256"})
    )
    return SourceItemTextScopeRequestV1(**values, request_sha256=digest)


def validate_source_item_text_slice_candidate(
    request: SourceItemTextScopeRequestV1,
    candidate: SourceItemTextSliceCandidateV1,
) -> None:
    if (
        candidate.source_sha256 != request.source_sha256
        or candidate.document_map_artifact_id != request.document_map_artifact_id
        or candidate.document_map_artifact_sha256
        != request.document_map_artifact_sha256
        or candidate.request_sha256 != request.request_sha256
        or candidate.scope_id != request.scope_id
    ):
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_CANDIDATE_LINEAGE_DRIFT")
    if request.structural_role and candidate.items:
        raise SourceRequirementBaselineError("SOURCE_ITEM_FRONT_MATTER_MUST_REMAIN_STRUCTURE: move independent body obligations into their own source-backed Map scope")
    content_by_id = {item.content_id: item for item in request.source_content}
    content_order = {
        item.content_id: index for index, item in enumerate(request.source_content)
    }
    assignments: dict[tuple[str, int], str] = {}

    def unknown_content(content_id: str) -> SourceRequirementBaselineError:
        from difflib import get_close_matches
        nearby = get_close_matches(content_id, tuple(content_by_id), n=3, cutoff=0.35)
        return SourceRequirementBaselineError(
            f"SOURCE_ITEM_TEXT_CONTENT_UNKNOWN: {content_id!r}; scope={request.scope_id}; "
            f"nearby_legal_ids={nearby!r}; use request.source_content[].content_id")

    def assign(span: SourceItemTextSpanV1, owner: str, *, structure_only: bool) -> None:
        content = content_by_id.get(span.content_id)
        if content is None:
            raise unknown_content(span.content_id)
        if span.end_offset > len(content.text):
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_OUT_OF_RANGE")
        selected = content.text[span.start_offset : span.end_offset]
        if not selected.strip():
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_EMPTY")
        if structure_only and not request.structural_role and not _is_structure_only_title(
            request=request,
            content=content,
            selected=selected,
        ):
            error = SourceRequirementBaselineError("SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE")
            error.scope_id = request.scope_id
            error.node_id = request.node_id
            error.content_id = content.content_id
            raise error
        for offset in range(span.start_offset, span.end_offset):
            if content.text[offset].isspace():
                continue
            key = (span.content_id, offset)
            if key in assignments:
                raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_OVERLAP")
            assignments[key] = owner

    for item in candidate.items:
        positions = [
            (content_order.get(span.content_id, 1_000_000), span.start_offset)
            for span in item.text_spans
        ]
        if positions != sorted(positions):
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_ORDER_INVALID")
        for span in item.text_spans:
            content = content_by_id.get(span.content_id)
            if content is None:
                raise unknown_content(span.content_id)
            selected = content.text[span.start_offset : span.end_offset]
            if (
                item.requirementness is Requirementness.NON_REQUIREMENT
                and item.non_requirement_role is NonRequirementRole.HEADING
                and _is_structure_only_title(
                    request=request,
                    content=content,
                    selected=selected,
                )
            ):
                raise SourceRequirementBaselineError(
                    "SOURCE_ITEM_PURE_TITLE_DUPLICATED_AS_ITEM"
                )
            assign(span, f"item:{item.item_key}", structure_only=False)
        if not set(item.shared_source_content_ids).issubset(content_by_id):
            raise SourceRequirementBaselineError("SOURCE_ITEM_SHARED_CONTENT_UNKNOWN")
        owned_content_ids = {span.content_id for span in item.text_spans}
        if owned_content_ids.intersection(item.shared_source_content_ids):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_TEXT_SHARED_CONTENT_OVERLAPS_OWNED_SPAN"
            )
    for index, disposition in enumerate(candidate.structure_only_spans):
        assign(
            disposition.text_span,
            f"structure:{index}",
            structure_only=True,
        )
    expected = {
        (content.content_id, offset)
        for content in request.source_content
        for offset, character in enumerate(content.text)
        if not character.isspace()
    }
    if set(assignments) != expected:
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_COVERAGE_INCOMPLETE")


def _is_structure_only_title(
    *,
    request: SourceItemTextScopeRequestV1,
    content: DocumentMapSourceContentV1,
    selected: str,
) -> bool:
    def normalize(value: str) -> str:
        # The PDF text layer and the recovered title tree may disagree only in
        # compatibility punctuation (for example ``11)`` versus ``11）``) and
        # whitespace. NFKC makes those source-equivalent headings comparable
        # without erasing semantic punctuation from the title itself.
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).strip()

    normalized = normalize(selected)
    candidates = {
        normalize(request.node_title),
        normalize(request.node_label),
        normalize(" ".join(filter(None, (request.node_label, request.node_title)))),
    }
    if request.node_label and request.node_title:
        # Source numbering often carries a presentation separator that the title
        # tree deliberately normalizes away (for example ``1、概述`` -> label
        # ``1`` + title ``概述``).  Accept only separators at that exact boundary;
        # never erase punctuation inside the semantic title text.
        candidates.update(
            normalize(f"{request.node_label}{separator}{request.node_title}")
            for separator in ("、", ".", "．", ")", "）", ":", "：")
        )
    # A visible heading can consist of several native blocks (number, title,
    # subtitle). Accept whole title blocks only if their complete source-ordered
    # concatenation is exactly the declared heading, never an arbitrary substring.
    title_blocks = tuple(part for part in request.source_content if part.is_title)
    if (len(title_blocks) > 1 and content.is_title
            and normalize(content.text) == normalized
            and normalize("".join(part.text for part in title_blocks)) in candidates):
        return True
    # Some document-unit titles are exact standalone source blocks even when the
    # upstream evidence flag did not mark them as titles. The same visible title
    # may also be emitted more than once inside one source block. Accept that
    # repeated-title shape only when every non-empty source line is itself one of
    # the node's visible title identities. Any additional body line makes the
    # proof fail closed.
    content_lines = tuple(
        value
        for value in (normalize(line) for line in content.text.splitlines())
        if value
    )
    exact_standalone_node_title = normalize(content.text) == normalized
    repeated_standalone_node_title = len(content_lines) > 1 and all(
        value == normalized for value in content_lines
    )
    return (
        bool(normalized)
        and normalized in candidates
        and (
            content.is_title
            or exact_standalone_node_title
            or repeated_standalone_node_title
        )
    )


def validate_source_requirement_slice_candidate(
    request: SourceRequirementScopeRequestV1,
    candidate: SourceRequirementSliceCandidateV1,
) -> None:
    if (
        candidate.source_sha256 != request.source_sha256
        or candidate.document_map_artifact_id != request.document_map_artifact_id
        or candidate.document_map_artifact_sha256
        != request.document_map_artifact_sha256
        or candidate.request_sha256 != request.request_sha256
        or candidate.logical_table_scope_id != request.logical_table_scope_id
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_CANDIDATE_LINEAGE_DRIFT"
        )
    validate_source_requirement_row_partition(
        request,
        record_row_groups=tuple(record.row_groups for record in candidate.records),
        context_row_groups=candidate.context_row_groups,
    )
    fragments = {fragment.fragment_id: fragment for fragment in request.fragments}
    for context in candidate.context_row_groups:
        if context.exact_placeholder_values:
            group = context.row_group
            values = {cell.text for cell in _cells_for_rows(fragments[group.fragment_id], group.row_start, group.row_end)}
            if not set(context.exact_placeholder_values) <= values or any(value.strip() and value not in context.exact_placeholder_values for value in values):
                raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_PLACEHOLDER_NOT_EXACT_SOURCE_VALUES: " + group.fragment_id)
    if request.structural_role and candidate.records:
        raise SourceRequirementBaselineError("SOURCE_ITEM_FRONT_MATTER_MUST_REMAIN_STRUCTURE")
    if any(context.role is ContextRowRole.FRONT_MATTER for context in candidate.context_row_groups) and not request.structural_role:
        raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_FRONT_MATTER_REQUIRES_MAP_ROLE")
    if not candidate.records:
        if not candidate.context_row_groups:
            raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_EMPTY_TABLE_CONTEXT_REQUIRED")
        for context in candidate.context_row_groups:
            if context.role not in {ContextRowRole.HEADER, ContextRowRole.FRONT_MATTER}:
                group = context.row_group
                if any(cell.text.strip() and cell.text not in context.exact_placeholder_values for cell in _cells_for_rows(fragments[group.fragment_id], group.row_start, group.row_end)):
                    raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_EMPTY_TABLE_HAS_BODY_TEXT: " + group.fragment_id)
    available_content_ids = {item.content_id for item in request.source_content}
    for record in candidate.records:
        if not set(record.shared_source_content_ids).issubset(available_content_ids):
            raise SourceRequirementBaselineError(
                "SOURCE_REQUIREMENT_SHARED_CONTENT_UNKNOWN"
            )


def _validate_empty_table_revision_headers(previous, candidates):
    if previous is None:
        return
    for candidate in candidates:
        if candidate.records or candidate.logical_table_scope_id not in previous.covered_scope_ids:
            continue
        old = {(item.fragment_id, item.row_start, item.row_end) for item in previous.context_dispositions
               if item.logical_table_scope_id == candidate.logical_table_scope_id and item.role is ContextRowRole.HEADER}
        new = {(item.row_group.fragment_id, item.row_group.row_start, item.row_group.row_end)
               for item in candidate.context_row_groups if item.role is ContextRowRole.HEADER}
        if new != old:
            raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_EMPTY_TABLE_HEADER_BOUNDARY_CHANGED: " + candidate.logical_table_scope_id)


def validate_source_requirement_row_partition(
    request: SourceRequirementScopeRequestV1,
    *,
    record_row_groups: tuple[tuple[SourceRequirementRowGroupV1, ...], ...],
    context_row_groups: tuple[SourceRequirementContextRowGroupV1, ...],
) -> None:
    """Validate exact row ownership without requiring semantic classification."""

    fragment_by_id = {item.fragment_id: item for item in request.fragments}
    assignments: dict[tuple[str, int], str] = {}

    def assign(group: SourceRequirementRowGroupV1, owner: str) -> None:
        fragment = fragment_by_id.get(group.fragment_id)
        if fragment is None:
            raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_FRAGMENT_UNKNOWN")
        if group.row_end > fragment.row_count:
            raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_ROW_OUT_OF_RANGE")
        for row in range(group.row_start, group.row_end):
            key = (group.fragment_id, row)
            if key in assignments:
                raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_ROW_OVERLAP")
            assignments[key] = owner

    for record_index, row_groups in enumerate(record_row_groups):
        for group in row_groups:
            assign(group, f"record:{record_index}")
    for index, context in enumerate(context_row_groups):
        assign(context.row_group, f"context:{index}")
    expected = {
        (fragment.fragment_id, row)
        for fragment in request.fragments
        for row in range(fragment.row_count)
    }
    if set(assignments) != expected:
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_ROW_COVERAGE_INCOMPLETE"
        )


def create_empty_source_requirement_baseline(
    document_map: DocumentMapArtifactV1,
) -> tuple[SourceRequirementBaselineArtifactV1, SourceRequirementBaselineValidationReceiptV1]:
    """An honest starting ledger for a fresh map, never a completed extraction."""
    draft = SourceRequirementBaselineArtifactV1.model_construct(
        artifact_id="source-requirement-baseline-" + "0" * 32,
        artifact_sha256="0" * 64, revision=1, status="EMPTY",
        source_sha256=document_map.source_sha256,
        document_map_artifact_id=document_map.artifact_id,
        document_map_artifact_sha256=document_map.artifact_sha256,
        covered_scope_ids=(), records=(),
    )
    digest = canonical_json_sha256(_baseline_hash_payload(draft))
    artifact = SourceRequirementBaselineArtifactV1.model_validate({
        **draft.model_dump(mode="json"),
        "artifact_id": "source-requirement-baseline-" + digest[:32],
        "artifact_sha256": digest,
    })
    return artifact, _compile_validation_receipt_from_artifact(document_map=document_map, artifact=artifact)


def compile_source_requirement_baseline(
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceRequirementScopeRequestV1, ...],
    candidates: tuple[SourceRequirementSliceCandidateV1, ...],
    text_requests: tuple[SourceItemTextScopeRequestV1, ...] = (),
    text_candidates: tuple[SourceItemTextSliceCandidateV1, ...] = (),
    revision: int | None = None,
    status: Literal["SLICE_COMPLETE", "COMPLETE"] = "SLICE_COMPLETE",
    starting_display_ordinal: int = 1,
    previous_artifact: SourceRequirementBaselineArtifactV1 | None = None,
) -> tuple[
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
]:
    _validate_empty_table_revision_headers(previous_artifact, candidates)
    expected_revision = (
        previous_artifact.revision + 1 if previous_artifact is not None else 1
    )
    resolved_revision = expected_revision if revision is None else revision
    if resolved_revision != expected_revision or starting_display_ordinal < 1:
        raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_REVISION_INVALID")
    if previous_artifact is not None and (
        previous_artifact.source_sha256 != document_map.source_sha256
        or previous_artifact.document_map_artifact_id != document_map.artifact_id
        or previous_artifact.document_map_artifact_sha256
        != document_map.artifact_sha256
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_PREDECESSOR_LINEAGE_DRIFT"
        )
    request_by_scope = {item.logical_table_scope_id: item for item in requests}
    candidate_by_scope = {item.logical_table_scope_id: item for item in candidates}
    text_request_by_scope = {item.scope_id: item for item in text_requests}
    text_candidate_by_scope = {item.scope_id: item for item in text_candidates}
    if (
        len(request_by_scope) != len(requests)
        or len(candidate_by_scope) != len(candidates)
        or set(request_by_scope) != set(candidate_by_scope)
        or len(text_request_by_scope) != len(text_requests)
        or len(text_candidate_by_scope) != len(text_candidates)
        or set(text_request_by_scope) != set(text_candidate_by_scope)
        or not (request_by_scope or text_request_by_scope)
    ):
        raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_SCOPE_SET_INVALID")
    requested_scope_ids = set(request_by_scope).union(text_request_by_scope)
    retained_image_records = tuple(item for item in previous_artifact.records if item.image_bindings) if previous_artifact is not None else ()
    requested_scope_ids.update(_record_scope_id(item) for item in retained_image_records)
    if previous_artifact is not None and not set(
        previous_artifact.covered_scope_ids
    ).issubset(requested_scope_ids):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_PREDECESSOR_SCOPE_REMOVED"
        )
    working_records: list[tuple[tuple[int, float, int, str], Any]] = []
    context_dispositions: list[SourceRequirementContextDispositionV1] = []
    text_context_dispositions: list[SourceItemTextContextDispositionV1] = []
    all_diagnostics: set[str] = set()
    owned_cell_ids: set[str] = set()
    context_cell_ids: set[str] = set()
    shared_cell_ids: set[str] = set()
    owned_text_character_count = 0
    context_text_character_count = 0

    for scope_id in sorted(request_by_scope):
        request = request_by_scope[scope_id]
        candidate = candidate_by_scope[scope_id]
        if (
            request.document_map_artifact_id != document_map.artifact_id
            or request.document_map_artifact_sha256 != document_map.artifact_sha256
            or request.source_sha256 != document_map.source_sha256
        ):
            raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_MAP_LINEAGE_DRIFT")
        expected_role = next(node.kind for node in document_map.nodes if node.node_id == request.node_id)
        if request.structural_role != (expected_role if expected_role in FRONT_MATTER_KINDS else None):
            raise SourceRequirementBaselineError("SOURCE_ITEM_STRUCTURAL_ROLE_MAP_DRIFT")
        validate_source_requirement_slice_candidate(request, candidate)
        fragment_by_id = {item.fragment_id: item for item in request.fragments}
        row_owner: dict[tuple[str, int], str] = {}
        for record in candidate.records:
            for group in record.row_groups:
                for row in range(group.row_start, group.row_end):
                    row_owner[(group.fragment_id, row)] = f"record:{record.record_key}"
        for index, context in enumerate(candidate.context_row_groups):
            group = context.row_group
            for row in range(group.row_start, group.row_end):
                row_owner[(group.fragment_id, row)] = f"context:{index}"
        scope_shared_cell_ids: set[str] = set()
        for fragment in request.fragments:
            for cell in fragment.cells:
                owners = {
                    row_owner[(fragment.fragment_id, row)]
                    for row in range(cell.row, cell.row + cell.row_span)
                }
                if len(owners) <= 1:
                    continue
                if any(owner.startswith("context:") for owner in owners):
                    raise SourceRequirementBaselineError(
                        "SOURCE_REQUIREMENT_CONTEXT_CELL_CROSSES_RECORD"
                    )
                scope_shared_cell_ids.add(cell.cell_id)
        if shared_cell_ids.intersection(scope_shared_cell_ids):
            raise SourceRequirementBaselineError(
                "SOURCE_REQUIREMENT_SHARED_CELL_SCOPE_OVERLAP"
            )
        shared_cell_ids.update(scope_shared_cell_ids)
        header_cells: set[str] = set()
        header_contexts: list[tuple[str, int, int, tuple[str, ...]]] = []
        for context in candidate.context_row_groups:
            fragment = fragment_by_id[context.row_group.fragment_id]
            cells = _cells_for_rows(
                fragment,
                context.row_group.row_start,
                context.row_group.row_end,
            )
            cell_ids = tuple(item.cell_id for item in cells)
            if context.role is ContextRowRole.HEADER:
                header_cells.update(cell_ids)
                header_contexts.append(
                    (
                        fragment.fragment_id,
                        context.row_group.row_start,
                        context.row_group.row_end,
                        cell_ids,
                    )
                )
            if (
                context_cell_ids.intersection(cell_ids)
                or owned_cell_ids.intersection(cell_ids)
                or shared_cell_ids.intersection(cell_ids)
            ):
                raise SourceRequirementBaselineError(
                    "SOURCE_REQUIREMENT_CONTEXT_CELL_OVERLAP"
                )
            context_cell_ids.update(cell_ids)
            context_dispositions.append(
                SourceRequirementContextDispositionV1(
                    logical_table_scope_id=scope_id,
                    role=context.role,
                    fragment_id=fragment.fragment_id,
                    row_start=context.row_group.row_start,
                    row_end=context.row_group.row_end,
                    cell_ids=cell_ids,
                    reason_codes=context.reason_codes,
                    exact_placeholder_values=context.exact_placeholder_values, empty_template_reason=context.empty_template_reason,
                )
            )
        for record in candidate.records:
            bindings: list[SourceRequirementTableFragmentBindingV1] = []
            pages: set[int] = set()
            first_key: tuple[int, float, int, str] | None = None
            record_cell_ids: set[str] = set()
            record_shared_cell_ids: set[str] = set()
            record_header_cell_ids = _applicable_header_cell_ids(
                header_contexts=tuple(header_contexts),
                record_groups=record.row_groups,
            )
            record_fragment_ids = {group.fragment_id for group in record.row_groups}
            if not record_header_cell_ids and not any(
                item[0] in record_fragment_ids for item in header_contexts
            ):
                record_header_cell_ids = tuple(sorted(header_cells))
            for group in record.row_groups:
                fragment = fragment_by_id[group.fragment_id]
                cells = _cells_for_rows(fragment, group.row_start, group.row_end)
                cell_ids = tuple(item.cell_id for item in cells)
                exclusive_cell_ids = tuple(
                    cell_id
                    for cell_id in cell_ids
                    if cell_id not in scope_shared_cell_ids
                )
                record_shared_cell_ids.update(
                    cell_id for cell_id in cell_ids if cell_id in scope_shared_cell_ids
                )
                if (
                    record_cell_ids.intersection(exclusive_cell_ids)
                    or owned_cell_ids.intersection(exclusive_cell_ids)
                    or context_cell_ids.intersection(exclusive_cell_ids)
                ):
                    raise SourceRequirementBaselineError(
                        "SOURCE_REQUIREMENT_RECORD_CELL_OVERLAP"
                    )
                record_cell_ids.update(exclusive_cell_ids)
                if fragment.page is not None:
                    pages.add(fragment.page)
                bbox = _union_bbox(tuple(item.bbox for item in cells)) if original_format(fragment.native_locator) == "pdf" else None
                bindings.append(
                    SourceRequirementTableFragmentBindingV1(
                        fragment_id=fragment.fragment_id,
                        table_token=fragment.table_token,
                        page=fragment.page,
                        row_start=group.row_start,
                        row_end=group.row_end,
                        cell_ids=cell_ids,
                        bbox=bbox,
                        source_bounds=fragment.source_bounds,
                        **native_location_fields(fragment),
                    )
                )
                key = (fragment.page, float(bbox[1]), group.row_start, record.record_key) if original_format(fragment.native_locator) == "pdf" else (*source_position(fragment, row_offset=group.row_start), record.record_key)
                first_key = key if first_key is None else min(first_key, key)
            owned_cell_ids.update(record_cell_ids)
            assert first_key is not None
            working_records.append(
                (
                    first_key,
                    (
                        "TABLE",
                        request,
                        record,
                        tuple(bindings),
                        tuple(sorted(pages)),
                        record_header_cell_ids,
                        tuple(sorted(record_shared_cell_ids)),
                    ),
                )
            )
        all_diagnostics.update(candidate.diagnostic_codes)

    for scope_id in sorted(text_request_by_scope):
        request = text_request_by_scope[scope_id]
        candidate = text_candidate_by_scope[scope_id]
        if (
            request.document_map_artifact_id != document_map.artifact_id
            or request.document_map_artifact_sha256 != document_map.artifact_sha256
            or request.source_sha256 != document_map.source_sha256
        ):
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_MAP_LINEAGE_DRIFT")
        expected_role = next(node.kind for node in document_map.nodes if node.node_id == request.node_id)
        if request.structural_role != (expected_role if expected_role in FRONT_MATTER_KINDS else None):
            raise SourceRequirementBaselineError("SOURCE_ITEM_STRUCTURAL_ROLE_MAP_DRIFT")
        validate_source_item_text_slice_candidate(request, candidate)
        content_by_id = {item.content_id: item for item in request.source_content}
        for disposition in candidate.structure_only_spans:
            span = disposition.text_span
            selected = content_by_id[span.content_id].text[
                span.start_offset : span.end_offset
            ]
            context_text_character_count += sum(
                not character.isspace() for character in selected
            )
            text_context_dispositions.append(
                SourceItemTextContextDispositionV1(
                    scope_id=scope_id,
                    node_id=request.node_id,
                    content_id=span.content_id,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                    reason_codes=disposition.reason_codes,
                )
            )
        for item in candidate.items:
            bindings: list[SourceItemTextBindingV1] = []
            pages: set[int] = set()
            first_key: tuple[int, float, int, str] | None = None
            for span in item.text_spans:
                content = content_by_id[span.content_id]
                selected = content.text[span.start_offset : span.end_offset]
                binding = SourceItemTextBindingV1(
                    content_id=content.content_id,
                    page=content.page,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                    text=selected,
                    text_sha256=canonical_text_sha256(selected),
                    bbox=content.bbox,
                    source_bounds=content.source_bounds,
                    precision=content.precision,
                    **native_location_fields(content),
                )
                bindings.append(binding)
                if content.page is not None:
                    pages.add(content.page)
                owned_text_character_count += sum(
                    not character.isspace() for character in selected
                )
                key = (content.page, float(content.bbox[1]), span.start_offset, item.item_key) if original_format(content.native_locator) == "pdf" else (*source_position(content, offset=span.start_offset), item.item_key)
                first_key = key if first_key is None else min(first_key, key)
            assert first_key is not None
            working_records.append(
                (
                    first_key,
                    (
                        "TEXT",
                        request,
                        item,
                        tuple(bindings),
                        tuple(sorted(pages)),
                        (),
                        (),
                    ),
                )
            )
        all_diagnostics.update(candidate.diagnostic_codes)

    previous_records_by_id = (
        {item.source_record_id: item for item in previous_artifact.records}
        if previous_artifact is not None
        else {}
    )
    next_display_ordinal = (
        max(
            [
                int(item.source_display_id.removeprefix("SRC-"))
                for item in previous_records_by_id.values()
            ]
            + [starting_display_ordinal - 1, _baseline_display_high_watermark(previous_artifact)]
        )
        + 1
    )
    records: list[SourceRequirementRecordV1] = []
    for source_order, (_, values) in enumerate(
        sorted(working_records, key=lambda item: item[0])
    ):
        (
            evidence_kind,
            request,
            candidate_record,
            bindings,
            pages,
            header_cells,
            record_shared_cell_ids,
        ) = values
        identity_sha = canonical_json_sha256(
            {
                "document_map_artifact_sha256": document_map.artifact_sha256,
                "scope_id": (
                    request.logical_table_scope_id
                    if evidence_kind == "TABLE"
                    else request.scope_id
                ),
                "item_key": (
                    candidate_record.record_key
                    if evidence_kind == "TABLE"
                    else candidate_record.item_key
                ),
                "evidence_kind": evidence_kind,
                "bindings": [item.model_dump(mode="json") for item in bindings],
            }
        )
        source_record_id = f"source-record-{identity_sha[:32]}"
        previous_record = previous_records_by_id.get(source_record_id)
        if previous_record is not None:
            source_display_id = previous_record.source_display_id
        else:
            source_display_id = f"SRC-{next_display_ordinal:04d}"
            next_display_ordinal += 1
        requirement_display_id = (
            "REQ-" + source_display_id.removeprefix("SRC-")
            if candidate_record.requirementness is Requirementness.REQUIREMENT
            else None
        )
        records.append(
            SourceRequirementRecordV1(
                source_record_id=source_record_id,
                source_display_id=source_display_id,
                requirement_display_id=requirement_display_id,
                source_order=source_order,
                document_map_artifact_id=document_map.artifact_id,
                document_map_artifact_sha256=document_map.artifact_sha256,
                node_id=request.node_id,
                ancestor_path=request.ancestor_path
                + tuple(
                    value
                    for value in (
                        " ".join(
                            item
                            for item in (request.node_label, request.node_title)
                            if item
                        ),
                    )
                    if value
                ),
                logical_table_scope_id=(
                    request.logical_table_scope_id if evidence_kind == "TABLE" else ""
                ),
                label=candidate_record.label,
                source_form=candidate_record.source_form,
                table_fragments=bindings if evidence_kind == "TABLE" else (),
                text_bindings=bindings if evidence_kind == "TEXT" else (),
                shared_header_cell_ids=header_cells,
                shared_table_cell_ids=record_shared_cell_ids,
                shared_source_content_ids=candidate_record.shared_source_content_ids,
                pages=pages,
                requirementness=candidate_record.requirementness,
                requirement_kinds=candidate_record.requirement_kinds,
                non_requirement_role=candidate_record.non_requirement_role,
                confidence=candidate_record.confidence,
                decision_summary=candidate_record.decision_summary,
                reason_codes=candidate_record.reason_codes,
                evidence_state=(
                    "SUFFICIENT"
                    if candidate_record.confidence >= 0.75
                    else "CONFLICTING"
                    if candidate_record.confidence >= 0.5
                    else "INSUFFICIENT"
                ),
            )
        )
    if retained_image_records:
        records.extend(retained_image_records)
        records = [record.model_copy(update={"source_order": index}) for index, record in enumerate(
            sorted(records, key=lambda item: (_record_source_position(item), item.source_record_id)))]
        all_diagnostics.update(previous_artifact.diagnostic_codes)
    if previous_artifact is not None:
        missing_previous_record_ids = set(previous_records_by_id).difference(
            item.source_record_id for item in records
        )
        if missing_previous_record_ids:
            raise SourceRequirementBaselineError(
                "SOURCE_REQUIREMENT_PREDECESSOR_RECORD_REMOVED"
            )
    artifact_values = {
        "revision": resolved_revision,
        "status": status,
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "predecessor_artifact_id": (
            previous_artifact.artifact_id if previous_artifact is not None else ""
        ),
        "predecessor_artifact_sha256": (
            previous_artifact.artifact_sha256 if previous_artifact is not None else ""
        ),
        "covered_scope_ids": tuple(
            sorted(requested_scope_ids)
        ),
        "records": tuple(records),
        "source_display_high_watermark": _inherit_source_display_high_watermark(previous_artifact, records),
        "table_image_relations": _inherit_table_image_relations(previous_artifact, records),
        "context_dispositions": tuple(
            sorted(
                context_dispositions,
                key=lambda item: (
                    item.logical_table_scope_id,
                    item.fragment_id,
                    item.row_start,
                ),
            )
        ),
        "text_context_dispositions": tuple(
            sorted(
                text_context_dispositions,
                key=lambda item: (
                    item.scope_id,
                    item.content_id,
                    item.start_offset,
                ),
            )
        ),
        "diagnostic_codes": tuple(sorted(all_diagnostics)),
    }
    draft = SourceRequirementBaselineArtifactV1.model_construct(
        **artifact_values,
        artifact_id="source-requirement-baseline-" + "0" * 32,
        artifact_sha256="0" * 64,
    )
    digest = canonical_json_sha256(_baseline_hash_payload(draft))
    artifact = SourceRequirementBaselineArtifactV1(
        **artifact_values,
        artifact_id=f"source-requirement-baseline-{digest[:32]}",
        artifact_sha256=digest,
    )
    coverage_sha256 = canonical_json_sha256(
        {
            "owned_cell_ids": sorted(owned_cell_ids),
            "context_cell_ids": sorted(context_cell_ids),
            "shared_cell_ids": sorted(shared_cell_ids),
            "text_context_dispositions": [
                item.model_dump(mode="json")
                for item in artifact.text_context_dispositions
            ],
            "owned_text_character_count": owned_text_character_count,
            "context_text_character_count": context_text_character_count,
            "covered_scope_ids": artifact.covered_scope_ids,
        }
    )
    receipt_values = {
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "scope_count": len(artifact.covered_scope_ids),
        "record_count": len(records),
        "requirement_count": sum(
            item.requirementness is Requirementness.REQUIREMENT for item in records
        ),
        "non_requirement_count": sum(
            item.requirementness is Requirementness.NON_REQUIREMENT for item in records
        ),
        "owned_cell_count": len(owned_cell_ids),
        "context_cell_count": len(context_cell_ids),
        "shared_cell_count": len(shared_cell_ids),
        "owned_text_character_count": owned_text_character_count,
        "context_text_character_count": context_text_character_count,
        "coverage_sha256": _with_image_coverage(artifact, coverage_sha256),
        "owned_image_count": sum(len(record.image_bindings) for record in artifact.records),
    }
    receipt_draft = SourceRequirementBaselineValidationReceiptV1.model_construct(
        **receipt_values,
        receipt_sha256="0" * 64,
    )
    receipt_sha256 = canonical_json_sha256(_receipt_hash_payload(receipt_draft))
    return artifact, SourceRequirementBaselineValidationReceiptV1(
        **receipt_values,
        receipt_sha256=receipt_sha256,
    )


def append_source_requirement_baseline(
    *,
    document_map: DocumentMapArtifactV1,
    previous_artifact: SourceRequirementBaselineArtifactV1,
    previous_validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    requests: tuple[SourceRequirementScopeRequestV1, ...] = (),
    candidates: tuple[SourceRequirementSliceCandidateV1, ...] = (),
    text_requests: tuple[SourceItemTextScopeRequestV1, ...] = (),
    text_candidates: tuple[SourceItemTextSliceCandidateV1, ...] = (),
) -> tuple[
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceRequirementBaselineAppendReceiptV1,
]:
    """Append later source scopes without recompiling predecessor records."""

    if (
        previous_artifact.source_sha256 != document_map.source_sha256
        or previous_artifact.document_map_artifact_id != document_map.artifact_id
        or previous_artifact.document_map_artifact_sha256
        != document_map.artifact_sha256
        or previous_validation_receipt.artifact_id != previous_artifact.artifact_id
        or previous_validation_receipt.artifact_sha256
        != previous_artifact.artifact_sha256
        or previous_validation_receipt.document_map_artifact_id
        != document_map.artifact_id
        or previous_validation_receipt.document_map_artifact_sha256
        != document_map.artifact_sha256
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_PREDECESSOR_LINEAGE_DRIFT"
        )
    new_scope_ids = {item.logical_table_scope_id for item in requests}.union(
        item.scope_id for item in text_requests
    )
    if (
        not new_scope_ids
        or new_scope_ids.intersection(previous_artifact.covered_scope_ids)
        or len(new_scope_ids) != len(requests) + len(text_requests)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_SCOPE_SET_INVALID"
        )
    next_display_ordinal = _baseline_display_high_watermark(previous_artifact) + 1
    delta_artifact, delta_receipt = compile_source_requirement_baseline(
        document_map=document_map,
        requests=requests,
        candidates=candidates,
        text_requests=text_requests,
        text_candidates=text_candidates,
        status="SLICE_COMPLETE" if previous_artifact.status == "EMPTY" else previous_artifact.status,
        starting_display_ordinal=next_display_ordinal,
    )
    if set(delta_artifact.covered_scope_ids) != new_scope_ids:
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_DELTA_SCOPE_DRIFT"
        )
    if (delta_artifact.records and previous_artifact.records
            and _record_source_position(delta_artifact.records[0]) < _record_source_position(previous_artifact.records[-1])):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_NOT_SOURCE_ORDERED"
        )

    previous_cell_sets = _artifact_cell_ownership_sets(previous_artifact)
    delta_cell_sets = _artifact_cell_ownership_sets(delta_artifact)
    if tuple(len(item) for item in previous_cell_sets) != (
        previous_validation_receipt.owned_cell_count,
        previous_validation_receipt.context_cell_count,
        previous_validation_receipt.shared_cell_count,
    ) or tuple(len(item) for item in delta_cell_sets) != (
        delta_receipt.owned_cell_count,
        delta_receipt.context_cell_count,
        delta_receipt.shared_cell_count,
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_RECEIPT_EVIDENCE_DRIFT"
        )
    if set().union(*previous_cell_sets).intersection(set().union(*delta_cell_sets)):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_CELL_EVIDENCE_OVERLAP"
        )
    if _artifact_text_positions(previous_artifact).intersection(
        _artifact_text_positions(delta_artifact)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_TEXT_EVIDENCE_OVERLAP"
        )

    rebased_delta_records = tuple(
        record.model_copy(
            update={"source_order": len(previous_artifact.records) + index}
        )
        for index, record in enumerate(delta_artifact.records)
    )
    if {item.source_record_id for item in previous_artifact.records}.intersection(
        item.source_record_id for item in rebased_delta_records
    ) or {item.source_display_id for item in previous_artifact.records}.intersection(
        item.source_display_id for item in rebased_delta_records
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_APPEND_RECORD_IDENTITY_COLLISION"
        )
    records = previous_artifact.records + rebased_delta_records
    artifact_values = {
        "revision": previous_artifact.revision + 1,
        "status": "SLICE_COMPLETE" if previous_artifact.status == "EMPTY" else previous_artifact.status,
        "source_sha256": previous_artifact.source_sha256,
        "document_map_artifact_id": previous_artifact.document_map_artifact_id,
        "document_map_artifact_sha256": (
            previous_artifact.document_map_artifact_sha256
        ),
        "predecessor_artifact_id": previous_artifact.artifact_id,
        "predecessor_artifact_sha256": previous_artifact.artifact_sha256,
        "covered_scope_ids": tuple(
            sorted(set(previous_artifact.covered_scope_ids).union(new_scope_ids))
        ),
        "records": records,
        "source_display_high_watermark": _inherit_source_display_high_watermark(previous_artifact, records),
        "table_image_relations": _inherit_table_image_relations(previous_artifact, records),
        "context_dispositions": tuple(
            sorted(
                previous_artifact.context_dispositions
                + delta_artifact.context_dispositions,
                key=lambda item: (
                    item.logical_table_scope_id,
                    item.fragment_id,
                    item.row_start,
                ),
            )
        ),
        "text_context_dispositions": tuple(
            sorted(
                previous_artifact.text_context_dispositions
                + delta_artifact.text_context_dispositions,
                key=lambda item: (
                    item.scope_id,
                    item.content_id,
                    item.start_offset,
                ),
            )
        ),
        "diagnostic_codes": tuple(
            sorted(
                set(previous_artifact.diagnostic_codes).union(
                    delta_artifact.diagnostic_codes
                )
            )
        ),
    }
    artifact_draft = SourceRequirementBaselineArtifactV1.model_construct(
        **artifact_values,
        artifact_id="source-requirement-baseline-" + "0" * 32,
        artifact_sha256="0" * 64,
    )
    artifact_sha256 = canonical_json_sha256(_baseline_hash_payload(artifact_draft))
    artifact = SourceRequirementBaselineArtifactV1(
        **artifact_values,
        artifact_id=f"source-requirement-baseline-{artifact_sha256[:32]}",
        artifact_sha256=artifact_sha256,
    )
    validation_receipt = _compile_appended_validation_receipt(
        artifact=artifact,
        previous_receipt=previous_validation_receipt,
        delta_receipt=delta_receipt,
    )
    preserved_records_sha256 = canonical_json_sha256(
        [item.model_dump(mode="json") for item in previous_artifact.records]
    )
    added_records_sha256 = canonical_json_sha256(
        [item.model_dump(mode="json") for item in rebased_delta_records]
    )
    append_values = {
        "predecessor_artifact_id": previous_artifact.artifact_id,
        "predecessor_artifact_sha256": previous_artifact.artifact_sha256,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "preserved_record_count": len(previous_artifact.records),
        "added_record_count": len(rebased_delta_records),
        "added_scope_ids": tuple(sorted(new_scope_ids)),
        "preserved_records_sha256": preserved_records_sha256,
        "added_records_sha256": added_records_sha256,
        "predecessor_coverage_sha256": previous_validation_receipt.coverage_sha256,
        "combined_coverage_sha256": validation_receipt.coverage_sha256,
    }
    append_draft = SourceRequirementBaselineAppendReceiptV1.model_construct(
        **append_values,
        receipt_sha256="0" * 64,
    )
    append_receipt_sha256 = canonical_json_sha256(
        append_draft.model_dump(mode="json", exclude={"receipt_sha256"})
    )
    append_receipt = SourceRequirementBaselineAppendReceiptV1(
        **append_values,
        receipt_sha256=append_receipt_sha256,
    )
    return artifact, validation_receipt, append_receipt


def grow_source_requirement_baseline(
    *,
    document_map: DocumentMapArtifactV1,
    previous_artifact: SourceRequirementBaselineArtifactV1,
    previous_validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    requests: tuple[SourceRequirementScopeRequestV1, ...] = (),
    candidates: tuple[SourceRequirementSliceCandidateV1, ...] = (),
    text_requests: tuple[SourceItemTextScopeRequestV1, ...] = (),
    text_candidates: tuple[SourceItemTextSliceCandidateV1, ...] = (),
) -> tuple[
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceRequirementBaselineGrowthReceiptV1,
]:
    """Insert new scopes by source position without changing predecessor identity.

    Stable record fields, SRC/REQ display identities, evidence and classifications are
    preserved exactly. Only ``source_order`` may change in the new immutable revision.
    """

    if (
        previous_artifact.source_sha256 != document_map.source_sha256
        or previous_artifact.document_map_artifact_id != document_map.artifact_id
        or previous_artifact.document_map_artifact_sha256
        != document_map.artifact_sha256
        or previous_validation_receipt.artifact_id != previous_artifact.artifact_id
        or previous_validation_receipt.artifact_sha256
        != previous_artifact.artifact_sha256
        or previous_validation_receipt.document_map_artifact_id
        != document_map.artifact_id
        or previous_validation_receipt.document_map_artifact_sha256
        != document_map.artifact_sha256
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_PREDECESSOR_LINEAGE_DRIFT"
        )
    new_scope_ids = {item.logical_table_scope_id for item in requests}.union(
        item.scope_id for item in text_requests
    )
    if (
        not new_scope_ids
        or new_scope_ids.intersection(previous_artifact.covered_scope_ids)
        or len(new_scope_ids) != len(requests) + len(text_requests)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_SCOPE_SET_INVALID"
        )
    next_display_ordinal = _baseline_display_high_watermark(previous_artifact) + 1
    delta_artifact, delta_receipt = compile_source_requirement_baseline(
        document_map=document_map,
        requests=requests,
        candidates=candidates,
        text_requests=text_requests,
        text_candidates=text_candidates,
        status="SLICE_COMPLETE" if previous_artifact.status == "EMPTY" else previous_artifact.status,
        starting_display_ordinal=next_display_ordinal,
    )
    if set(delta_artifact.covered_scope_ids) != new_scope_ids:
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_DELTA_SCOPE_DRIFT"
        )

    previous_cell_sets = _artifact_cell_ownership_sets(previous_artifact)
    delta_cell_sets = _artifact_cell_ownership_sets(delta_artifact)
    if tuple(len(item) for item in previous_cell_sets) != (
        previous_validation_receipt.owned_cell_count,
        previous_validation_receipt.context_cell_count,
        previous_validation_receipt.shared_cell_count,
    ) or tuple(len(item) for item in delta_cell_sets) != (
        delta_receipt.owned_cell_count,
        delta_receipt.context_cell_count,
        delta_receipt.shared_cell_count,
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_RECEIPT_EVIDENCE_DRIFT"
        )
    if set().union(*previous_cell_sets).intersection(set().union(*delta_cell_sets)):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_CELL_EVIDENCE_OVERLAP"
        )
    if _artifact_text_positions(previous_artifact).intersection(
        _artifact_text_positions(delta_artifact)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_TEXT_EVIDENCE_OVERLAP"
        )

    previous_record_ids = {item.source_record_id for item in previous_artifact.records}
    previous_display_ids = {
        item.source_display_id for item in previous_artifact.records
    }
    delta_record_ids = {item.source_record_id for item in delta_artifact.records}
    delta_display_ids = {item.source_display_id for item in delta_artifact.records}
    if previous_record_ids.intersection(delta_record_ids) or (
        previous_display_ids.intersection(delta_display_ids)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_RECORD_IDENTITY_COLLISION"
        )

    combined_records = previous_artifact.records + delta_artifact.records
    ordered_records = tuple(
        item.model_copy(update={"source_order": source_order})
        for source_order, item in enumerate(
            sorted(
                combined_records,
                key=lambda record: (
                    *_record_source_position(record),
                    record.source_record_id,
                ),
            )
        )
    )
    successor_previous_records = tuple(
        item for item in ordered_records if item.source_record_id in previous_record_ids
    )
    predecessor_stable_sha256 = _stable_record_set_sha256(previous_artifact.records)
    successor_stable_sha256 = _stable_record_set_sha256(successor_previous_records)
    if predecessor_stable_sha256 != successor_stable_sha256:
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_PREDECESSOR_RECORD_DRIFT"
        )
    added_records = tuple(
        item for item in ordered_records if item.source_record_id in delta_record_ids
    )
    if len(added_records) != len(delta_artifact.records):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_GROWTH_ADDED_RECORD_DRIFT"
        )

    artifact_values = {
        "revision": previous_artifact.revision + 1,
        "status": "SLICE_COMPLETE" if previous_artifact.status == "EMPTY" else previous_artifact.status,
        "source_sha256": previous_artifact.source_sha256,
        "document_map_artifact_id": previous_artifact.document_map_artifact_id,
        "document_map_artifact_sha256": (
            previous_artifact.document_map_artifact_sha256
        ),
        "predecessor_artifact_id": previous_artifact.artifact_id,
        "predecessor_artifact_sha256": previous_artifact.artifact_sha256,
        "covered_scope_ids": tuple(
            sorted(set(previous_artifact.covered_scope_ids).union(new_scope_ids))
        ),
        "records": ordered_records,
        "source_display_high_watermark": _inherit_source_display_high_watermark(previous_artifact, ordered_records),
        "table_image_relations": _inherit_table_image_relations(previous_artifact, ordered_records),
        "context_dispositions": tuple(
            sorted(
                previous_artifact.context_dispositions
                + delta_artifact.context_dispositions,
                key=lambda item: (
                    item.logical_table_scope_id,
                    item.fragment_id,
                    item.row_start,
                ),
            )
        ),
        "text_context_dispositions": tuple(
            sorted(
                previous_artifact.text_context_dispositions
                + delta_artifact.text_context_dispositions,
                key=lambda item: (
                    item.scope_id,
                    item.content_id,
                    item.start_offset,
                ),
            )
        ),
        "diagnostic_codes": tuple(
            sorted(
                set(previous_artifact.diagnostic_codes).union(
                    delta_artifact.diagnostic_codes
                )
            )
        ),
    }
    artifact_draft = SourceRequirementBaselineArtifactV1.model_construct(
        **artifact_values,
        artifact_id="source-requirement-baseline-" + "0" * 32,
        artifact_sha256="0" * 64,
    )
    artifact_sha256 = canonical_json_sha256(_baseline_hash_payload(artifact_draft))
    artifact = SourceRequirementBaselineArtifactV1(
        **artifact_values,
        artifact_id=f"source-requirement-baseline-{artifact_sha256[:32]}",
        artifact_sha256=artifact_sha256,
    )
    validation_receipt = _compile_appended_validation_receipt(
        artifact=artifact,
        previous_receipt=previous_validation_receipt,
        delta_receipt=delta_receipt,
    )
    growth_values = {
        "predecessor_artifact_id": previous_artifact.artifact_id,
        "predecessor_artifact_sha256": previous_artifact.artifact_sha256,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "preserved_record_count": len(previous_artifact.records),
        "added_record_count": len(added_records),
        "added_scope_ids": tuple(sorted(new_scope_ids)),
        "predecessor_stable_records_sha256": predecessor_stable_sha256,
        "successor_stable_records_sha256": successor_stable_sha256,
        "added_records_sha256": canonical_json_sha256(
            [item.model_dump(mode="json") for item in added_records]
        ),
        "predecessor_coverage_sha256": previous_validation_receipt.coverage_sha256,
        "combined_coverage_sha256": validation_receipt.coverage_sha256,
    }
    growth_draft = SourceRequirementBaselineGrowthReceiptV1.model_construct(
        **growth_values,
        receipt_sha256="0" * 64,
    )
    growth_receipt = SourceRequirementBaselineGrowthReceiptV1(
        **growth_values,
        receipt_sha256=canonical_json_sha256(
            growth_draft.model_dump(mode="json", exclude={"receipt_sha256"})
        ),
    )
    return artifact, validation_receipt, growth_receipt


def _compile_appended_validation_receipt(
    *,
    artifact: SourceRequirementBaselineArtifactV1,
    previous_receipt: SourceRequirementBaselineValidationReceiptV1,
    delta_receipt: SourceRequirementBaselineValidationReceiptV1,
) -> SourceRequirementBaselineValidationReceiptV1:
    owned_cell_ids, context_cell_ids, shared_cell_ids = _artifact_cell_ownership_sets(
        artifact
    )
    owned_text_character_count = (
        previous_receipt.owned_text_character_count
        + delta_receipt.owned_text_character_count
    )
    context_text_character_count = (
        previous_receipt.context_text_character_count
        + delta_receipt.context_text_character_count
    )
    coverage_sha256 = canonical_json_sha256(
        {
            "owned_cell_ids": sorted(owned_cell_ids),
            "context_cell_ids": sorted(context_cell_ids),
            "shared_cell_ids": sorted(shared_cell_ids),
            "text_context_dispositions": [
                item.model_dump(mode="json")
                for item in artifact.text_context_dispositions
            ],
            "owned_text_character_count": owned_text_character_count,
            "context_text_character_count": context_text_character_count,
            "covered_scope_ids": artifact.covered_scope_ids,
        }
    )
    values = {
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "document_map_artifact_id": artifact.document_map_artifact_id,
        "document_map_artifact_sha256": artifact.document_map_artifact_sha256,
        "scope_count": len(artifact.covered_scope_ids),
        "record_count": len(artifact.records),
        "requirement_count": sum(
            item.requirementness is Requirementness.REQUIREMENT
            for item in artifact.records
        ),
        "non_requirement_count": sum(
            item.requirementness is Requirementness.NON_REQUIREMENT
            for item in artifact.records
        ),
        "owned_cell_count": len(owned_cell_ids),
        "context_cell_count": len(context_cell_ids),
        "shared_cell_count": len(shared_cell_ids),
        "owned_text_character_count": owned_text_character_count,
        "context_text_character_count": context_text_character_count,
        "coverage_sha256": _with_image_coverage(artifact, coverage_sha256),
        "owned_image_count": sum(len(record.image_bindings) for record in artifact.records),
    }
    draft = SourceRequirementBaselineValidationReceiptV1.model_construct(
        **values,
        receipt_sha256="0" * 64,
    )
    receipt_sha256 = canonical_json_sha256(_receipt_hash_payload(draft))
    return SourceRequirementBaselineValidationReceiptV1(
        **values,
        receipt_sha256=receipt_sha256,
    )


def _artifact_cell_ownership_sets(
    artifact: SourceRequirementBaselineArtifactV1,
) -> tuple[set[str], set[str], set[str]]:
    shared_cell_ids = {
        cell_id
        for record in artifact.records
        for cell_id in record.shared_table_cell_ids
    }
    bound_cell_ids = {
        cell_id
        for record in artifact.records
        for fragment in record.table_fragments
        for cell_id in fragment.cell_ids
    }
    owned_cell_ids = bound_cell_ids.difference(shared_cell_ids)
    context_cell_ids = {
        cell_id
        for disposition in artifact.context_dispositions
        for cell_id in disposition.cell_ids
    }
    return owned_cell_ids, context_cell_ids, shared_cell_ids


def _artifact_text_positions(
    artifact: SourceRequirementBaselineArtifactV1,
) -> set[tuple[str, int]]:
    positions = {
        (binding.content_id, offset)
        for record in artifact.records
        for binding in record.text_bindings
        for offset in range(binding.start_offset, binding.end_offset)
    }
    positions.update(
        (disposition.content_id, offset)
        for disposition in artifact.text_context_dispositions
        for offset in range(disposition.start_offset, disposition.end_offset)
    )
    return positions


def _stable_record_set_sha256(
    records: tuple[SourceRequirementRecordV1, ...],
) -> str:
    return canonical_json_sha256(
        [
            item.model_dump(mode="json", exclude={"source_order"})
            for item in sorted(records, key=lambda value: value.source_record_id)
        ]
    )


def _record_source_position(
    record: SourceRequirementRecordV1,
) -> tuple[int, float, int]:
    bindings = (*record.table_fragments, *record.image_bindings, *record.text_bindings)
    if bindings and original_format(bindings[0].native_locator) != "pdf":
        return min(source_position(item, offset=getattr(item, "start_offset", 0)) for item in bindings)
    if record.table_fragments:
        return min(
            (
                item.page,
                float(item.bbox[1]),
                item.row_start,
            )
            for item in record.table_fragments
        )
    if record.image_bindings:
        return min((item.page, float(item.bbox[1]), 0) for item in record.image_bindings)
    return min(
        (
            item.page,
            float(item.bbox[1]),
            item.start_offset,
        )
        for item in record.text_bindings
    )


def _assert_direct_scope_revision_undo_target(
    *,
    current_artifact: SourceRequirementBaselineArtifactV1,
    undo_target_artifact: SourceRequirementBaselineArtifactV1,
) -> None:
    """Prove the v1 UNDO target is the current artifact's direct predecessor."""

    if (
        current_artifact.revision <= 1
        or undo_target_artifact.revision != current_artifact.revision - 1
        or undo_target_artifact.artifact_id != current_artifact.predecessor_artifact_id
        or undo_target_artifact.artifact_sha256
        != current_artifact.predecessor_artifact_sha256
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_UNDO_TARGET_NOT_DIRECT_PREDECESSOR"
        )


def build_source_item_scope_revision(
    *,
    document_map: DocumentMapArtifactV1,
    current_artifact: SourceRequirementBaselineArtifactV1,
    current_validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    operation: SourceItemScopeRevisionOperation,
    affected_scope_ids: tuple[str, ...],
    comparison_scope_ids: tuple[str, ...],
    feedback_ids: tuple[str, ...],
    requests: tuple[SourceRequirementScopeRequestV1, ...] = (),
    candidates: tuple[SourceRequirementSliceCandidateV1, ...] = (),
    text_requests: tuple[SourceItemTextScopeRequestV1, ...] = (),
    text_candidates: tuple[SourceItemTextSliceCandidateV1, ...] = (),
    undo_target_artifact: SourceRequirementBaselineArtifactV1 | None = None,
    previous_scope_revision_receipt: SourceItemScopeRevisionReceiptV1 | None = None,
    source_display_high_watermark_floor: int | None = None,
) -> SourceItemScopeRevisionV1:
    """Build the canonical CAS-bound input for an affected-only revision."""

    _assert_validation_receipt_matches_artifact(
        document_map=document_map,
        artifact=current_artifact,
        receipt=current_validation_receipt,
        error_code="SOURCE_ITEM_SCOPE_REVISION_CURRENT_RECEIPT_DRIFT",
    )
    high_watermark = _resolved_source_display_high_watermark(
        artifact=current_artifact,
        previous_scope_revision_receipt=previous_scope_revision_receipt,
        source_display_high_watermark_floor=source_display_high_watermark_floor,
    )
    operation = SourceItemScopeRevisionOperation(operation)
    bindings = (
        _replacement_scope_bindings(
            requests=requests,
            candidates=candidates,
            text_requests=text_requests,
            text_candidates=text_candidates,
        )
        if operation is SourceItemScopeRevisionOperation.REPLACE
        else ()
    )
    if operation is SourceItemScopeRevisionOperation.UNDO:
        if any((requests, candidates, text_requests, text_candidates)):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_UNDO_REPLACEMENT_FORBIDDEN"
            )
        if undo_target_artifact is None:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_UNDO_TARGET_MISSING"
            )
        _assert_direct_scope_revision_undo_target(
            current_artifact=current_artifact,
            undo_target_artifact=undo_target_artifact,
        )
    elif undo_target_artifact is not None:
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_REPLACE_UNDO_TARGET_FORBIDDEN"
        )
    values = {
        "operation": operation,
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "current_artifact_id": current_artifact.artifact_id,
        "current_artifact_sha256": current_artifact.artifact_sha256,
        "current_artifact_revision": current_artifact.revision,
        "current_validation_receipt_sha256": (
            current_validation_receipt.receipt_sha256
        ),
        "affected_scope_ids": tuple(sorted(set(affected_scope_ids))),
        "comparison_scope_ids": tuple(sorted(set(comparison_scope_ids))),
        "feedback_ids": tuple(sorted(set(feedback_ids))),
        "replacement_scope_bindings": bindings,
        "source_display_id_high_watermark": high_watermark,
        "undo_target_artifact_id": (
            undo_target_artifact.artifact_id if undo_target_artifact is not None else ""
        ),
        "undo_target_artifact_sha256": (
            undo_target_artifact.artifact_sha256
            if undo_target_artifact is not None
            else ""
        ),
        "undo_target_artifact_revision": (
            undo_target_artifact.revision if undo_target_artifact is not None else None
        ),
    }
    draft = SourceItemScopeRevisionV1.model_construct(
        **values,
        revision_sha256="0" * 64,
    )
    return SourceItemScopeRevisionV1(
        **values,
        revision_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"revision_sha256"})
        ),
    )


def compile_source_item_scope_revision(
    *,
    document_map: DocumentMapArtifactV1,
    current_artifact: SourceRequirementBaselineArtifactV1,
    current_validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    scope_revision: SourceItemScopeRevisionV1,
    requests: tuple[SourceRequirementScopeRequestV1, ...] = (),
    candidates: tuple[SourceRequirementSliceCandidateV1, ...] = (),
    text_requests: tuple[SourceItemTextScopeRequestV1, ...] = (),
    text_candidates: tuple[SourceItemTextSliceCandidateV1, ...] = (),
    undo_target_artifact: SourceRequirementBaselineArtifactV1 | None = None,
    previous_scope_revision_receipt: SourceItemScopeRevisionReceiptV1 | None = None,
    source_display_high_watermark_floor: int | None = None,
) -> tuple[
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceItemScopeRevisionReceiptV1,
]:
    """Replace only affected SourceItem scopes and append one immutable revision.

    Unaffected records are never sent through the baseline compiler. Their complete
    stable payload is reused, with only ``source_order`` allowed to change after the
    affected replacement is merged back into source order.
    """

    _assert_validation_receipt_matches_artifact(
        document_map=document_map,
        artifact=current_artifact,
        receipt=current_validation_receipt,
        error_code="SOURCE_ITEM_SCOPE_REVISION_CURRENT_RECEIPT_DRIFT",
    )
    if (
        scope_revision.source_sha256 != document_map.source_sha256
        or scope_revision.document_map_artifact_id != document_map.artifact_id
        or scope_revision.document_map_artifact_sha256 != document_map.artifact_sha256
        or scope_revision.current_artifact_id != current_artifact.artifact_id
        or scope_revision.current_artifact_sha256 != current_artifact.artifact_sha256
        or scope_revision.current_artifact_revision != current_artifact.revision
        or scope_revision.current_validation_receipt_sha256
        != current_validation_receipt.receipt_sha256
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_STALE_CURRENT_BASELINE_CAS"
        )
    expected_high_watermark = _resolved_source_display_high_watermark(
        artifact=current_artifact,
        previous_scope_revision_receipt=previous_scope_revision_receipt,
        source_display_high_watermark_floor=source_display_high_watermark_floor,
    )
    if scope_revision.source_display_id_high_watermark != expected_high_watermark:
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_HIGH_WATERMARK_DRIFT"
        )
    covered_scope_ids = set(current_artifact.covered_scope_ids)
    affected_scope_ids = set(scope_revision.affected_scope_ids)
    comparison_scope_ids = set(scope_revision.comparison_scope_ids)
    if not affected_scope_ids.issubset(covered_scope_ids) or not (
        comparison_scope_ids.issubset(covered_scope_ids)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_SCOPE_NOT_IN_CURRENT_BASELINE"
        )

    if scope_revision.operation is SourceItemScopeRevisionOperation.REPLACE:
        if undo_target_artifact is not None:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_REPLACE_UNDO_TARGET_FORBIDDEN"
            )
        _validate_empty_table_revision_headers(current_artifact, candidates)
        bindings = _replacement_scope_bindings(
            requests=requests,
            candidates=candidates,
            text_requests=text_requests,
            text_candidates=text_candidates,
        )
        if bindings != scope_revision.replacement_scope_bindings:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_REPLACEMENT_BINDING_DRIFT"
            )
        (
            desired_records,
            desired_context_dispositions,
            desired_text_context_dispositions,
            replacement_diagnostics,
        ) = _compile_affected_scope_replacement(
            document_map=document_map,
            current_status=current_artifact.status,
            requests=requests,
            candidates=candidates,
            text_requests=text_requests,
            text_candidates=text_candidates,
        )
    else:
        if any((requests, candidates, text_requests, text_candidates)):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_UNDO_REPLACEMENT_FORBIDDEN"
            )
        if undo_target_artifact is None or (
            undo_target_artifact.artifact_id != scope_revision.undo_target_artifact_id
            or undo_target_artifact.artifact_sha256
            != scope_revision.undo_target_artifact_sha256
            or undo_target_artifact.revision
            != scope_revision.undo_target_artifact_revision
        ):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_UNDO_TARGET_CAS_DRIFT"
            )
        _assert_direct_scope_revision_undo_target(
            current_artifact=current_artifact,
            undo_target_artifact=undo_target_artifact,
        )
        if (
            undo_target_artifact.source_sha256 != document_map.source_sha256
            or undo_target_artifact.document_map_artifact_id != document_map.artifact_id
            or undo_target_artifact.document_map_artifact_sha256
            != document_map.artifact_sha256
            or not affected_scope_ids.issubset(undo_target_artifact.covered_scope_ids)
        ):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_UNDO_TARGET_LINEAGE_DRIFT"
            )
        desired_records = tuple(
            item
            for item in undo_target_artifact.records
            if _record_scope_id(item) in affected_scope_ids
        )
        desired_context_dispositions = tuple(
            item
            for item in undo_target_artifact.context_dispositions
            if item.logical_table_scope_id in affected_scope_ids
        )
        desired_text_context_dispositions = tuple(
            item
            for item in undo_target_artifact.text_context_dispositions
            if item.scope_id in affected_scope_ids
        )
        # Diagnostics are not scope-addressable in the v1 baseline. Undo must not
        # reintroduce unrelated historical diagnostics from the target artifact.
        replacement_diagnostics = ()

    current_affected_records = tuple(
        item
        for item in current_artifact.records
        if _record_scope_id(item) in affected_scope_ids
    )
    unaffected_records = tuple(
        item
        for item in current_artifact.records
        if _record_scope_id(item) not in affected_scope_ids
    )
    before_coverage, affected_partition_before_sha256 = _scope_partition_snapshot(
        document_map=document_map,
        records=current_affected_records,
        context_dispositions=tuple(
            item
            for item in current_artifact.context_dispositions
            if item.logical_table_scope_id in affected_scope_ids
        ),
        text_context_dispositions=tuple(
            item
            for item in current_artifact.text_context_dispositions
            if item.scope_id in affected_scope_ids
        ),
        scope_ids=affected_scope_ids,
        subject="CURRENT",
    )
    desired_coverage, affected_partition_after_sha256 = _scope_partition_snapshot(
        document_map=document_map,
        records=desired_records,
        context_dispositions=desired_context_dispositions,
        text_context_dispositions=desired_text_context_dispositions,
        scope_ids=affected_scope_ids,
        subject="REPLACEMENT",
    )
    if before_coverage != desired_coverage:
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_COVERAGE_DRIFT"
        )

    current_by_evidence: dict[str, SourceRequirementRecordV1] = {}
    for record in current_affected_records:
        evidence_sha256 = _record_exact_evidence_sha256(record)
        if evidence_sha256 in current_by_evidence:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_CURRENT_EVIDENCE_DUPLICATED"
            )
        current_by_evidence[evidence_sha256] = record

    high_watermark_after = expected_high_watermark
    matched_records: list[SourceRequirementRecordV1] = []
    created_records: list[SourceRequirementRecordV1] = []
    resolved_affected_records: list[SourceRequirementRecordV1] = []
    if scope_revision.operation is SourceItemScopeRevisionOperation.UNDO:
        current_by_id = {
            item.source_record_id: item for item in current_affected_records
        }
        current_by_display_id = {
            item.source_display_id: item for item in current_affected_records
        }
        for record in sorted(
            desired_records,
            key=lambda item: (*_record_source_position(item), item.source_record_id),
        ):
            if (
                _source_display_ordinal(record.source_display_id)
                > expected_high_watermark
            ):
                raise SourceRequirementBaselineError(
                    "SOURCE_ITEM_SCOPE_REVISION_UNDO_ID_EXCEEDS_HIGH_WATERMARK"
                )
            current_id_match = current_by_id.get(record.source_record_id)
            current_display_match = current_by_display_id.get(record.source_display_id)
            if (
                current_id_match is not None
                and (
                    _record_exact_evidence_sha256(current_id_match)
                    != _record_exact_evidence_sha256(record)
                    or current_id_match.source_display_id != record.source_display_id
                )
            ) or (
                current_display_match is not None
                and current_display_match.source_record_id != record.source_record_id
            ):
                raise SourceRequirementBaselineError(
                    "SOURCE_ITEM_SCOPE_REVISION_UNDO_IDENTITY_COLLISION"
                )
            if current_id_match is not None and current_id_match.decision_origin == "USER_OVERRIDE":
                if _requirement_judgment(current_id_match) != _requirement_judgment(record):
                    raise SourceRequirementBaselineError("SOURCE_ITEM_USER_OVERRIDE_REQUIRES_EXPLICIT_USER_DECISION")
                record = _retain_reviewed_judgment(current_id_match, record)
            resolved_affected_records.append(record)
            matched_records.append(record)
    else:
        for record in sorted(
            desired_records,
            key=lambda item: (
                *_record_source_position(item),
                _record_exact_evidence_sha256(item),
            ),
        ):
            evidence_sha256 = _record_exact_evidence_sha256(record)
            previous_record = current_by_evidence.get(evidence_sha256)
            if previous_record is not None:
                resolved = record.model_copy(
                    update={
                        "source_record_id": previous_record.source_record_id,
                        "source_display_id": previous_record.source_display_id,
                        "requirement_display_id": (
                            previous_record.requirement_display_id or ("REQ-"
                            + previous_record.source_display_id.removeprefix("SRC-"))
                            if record.requirementness is Requirementness.REQUIREMENT
                            else None
                        ),
                    }
                )
                if previous_record.decision_origin == "USER_OVERRIDE" and _requirement_judgment(previous_record) != _requirement_judgment(resolved):
                    raise SourceRequirementBaselineError("SOURCE_ITEM_USER_OVERRIDE_REQUIRES_EXPLICIT_USER_DECISION")
                if _requirement_judgment(previous_record) == _requirement_judgment(resolved):
                    resolved = _retain_reviewed_judgment(previous_record, resolved)
                matched_records.append(resolved)
            else:
                high_watermark_after += 1
                source_display_id = f"SRC-{high_watermark_after:04d}"
                identity_sha256 = canonical_json_sha256(
                    {
                        "schema_version": "source-item-scope-created-identity/v1",
                        "document_map_artifact_sha256": document_map.artifact_sha256,
                        "predecessor_artifact_sha256": current_artifact.artifact_sha256,
                        "exact_evidence_sha256": evidence_sha256,
                    }
                )
                resolved = record.model_copy(
                    update={
                        "source_record_id": f"source-record-{identity_sha256[:32]}",
                        "source_display_id": source_display_id,
                        "requirement_display_id": (
                            "REQ-" + source_display_id.removeprefix("SRC-")
                            if record.requirementness is Requirementness.REQUIREMENT
                            else None
                        ),
                    }
                )
                created_records.append(resolved)
            resolved_affected_records.append(resolved)

    active_ids = {item.source_record_id for item in resolved_affected_records}
    tombstoned_records = tuple(
        item
        for item in current_affected_records
        if item.source_record_id not in active_ids
    )
    if any(record.decision_origin == "USER_OVERRIDE" for record in tombstoned_records):
        raise SourceRequirementBaselineError("SOURCE_ITEM_USER_OVERRIDE_BOUNDARY_REQUIRES_EXPLICIT_USER_DECISION")
    combined_records = unaffected_records + tuple(resolved_affected_records)
    ordered_records = tuple(
        item.model_copy(update={"source_order": source_order})
        for source_order, item in enumerate(
            sorted(
                combined_records,
                key=lambda item: (
                    *_record_source_position(item),
                    item.source_record_id,
                ),
            )
        )
    )
    if len({item.source_record_id for item in ordered_records}) != len(
        ordered_records
    ) or len({item.source_display_id for item in ordered_records}) != len(
        ordered_records
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_IDENTITY_COLLISION"
        )

    unaffected_context_dispositions = tuple(
        item
        for item in current_artifact.context_dispositions
        if item.logical_table_scope_id not in affected_scope_ids
    )
    unaffected_text_context_dispositions = tuple(
        item
        for item in current_artifact.text_context_dispositions
        if item.scope_id not in affected_scope_ids
    )
    artifact_values = {
        "revision": current_artifact.revision + 1,
        "status": current_artifact.status,
        "source_sha256": current_artifact.source_sha256,
        "document_map_artifact_id": current_artifact.document_map_artifact_id,
        "document_map_artifact_sha256": (current_artifact.document_map_artifact_sha256),
        "predecessor_artifact_id": current_artifact.artifact_id,
        "predecessor_artifact_sha256": current_artifact.artifact_sha256,
        "covered_scope_ids": current_artifact.covered_scope_ids,
        "records": ordered_records,
        "source_display_high_watermark": _inherit_source_display_high_watermark(current_artifact, ordered_records, high_watermark_after, floor=source_display_high_watermark_floor),
        "table_image_relations": _inherit_table_image_relations(current_artifact, ordered_records),
        "context_dispositions": tuple(
            sorted(
                unaffected_context_dispositions + desired_context_dispositions,
                key=lambda item: (
                    item.logical_table_scope_id,
                    item.fragment_id,
                    item.row_start,
                ),
            )
        ),
        "text_context_dispositions": tuple(
            sorted(
                unaffected_text_context_dispositions
                + desired_text_context_dispositions,
                key=lambda item: (
                    item.scope_id,
                    item.content_id,
                    item.start_offset,
                ),
            )
        ),
        "diagnostic_codes": tuple(
            sorted(
                set(current_artifact.diagnostic_codes).union(replacement_diagnostics)
            )
        ),
    }
    artifact_draft = SourceRequirementBaselineArtifactV1.model_construct(
        **artifact_values,
        artifact_id="source-requirement-baseline-" + "0" * 32,
        artifact_sha256="0" * 64,
    )
    artifact_sha256 = canonical_json_sha256(_baseline_hash_payload(artifact_draft))
    artifact = SourceRequirementBaselineArtifactV1(
        **artifact_values,
        artifact_id=f"source-requirement-baseline-{artifact_sha256[:32]}",
        artifact_sha256=artifact_sha256,
    )
    validation_receipt = _compile_validation_receipt_from_artifact(
        document_map=document_map,
        artifact=artifact,
    )
    after_coverage, final_partition_sha256 = _scope_partition_snapshot(
        document_map=document_map,
        records=tuple(
            item
            for item in artifact.records
            if _record_scope_id(item) in affected_scope_ids
        ),
        context_dispositions=tuple(
            item
            for item in artifact.context_dispositions
            if item.logical_table_scope_id in affected_scope_ids
        ),
        text_context_dispositions=tuple(
            item
            for item in artifact.text_context_dispositions
            if item.scope_id in affected_scope_ids
        ),
        scope_ids=affected_scope_ids,
        subject="SUCCESSOR",
    )
    if after_coverage != before_coverage or (
        final_partition_sha256 != affected_partition_after_sha256
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_SUCCESSOR_PARTITION_DRIFT"
        )

    successor_relations = _compile_scope_successor_relations(
        document_map=document_map,
        operation=scope_revision.operation,
        tombstoned_records=tombstoned_records,
        successor_records=tuple(resolved_affected_records),
        successor_context_dispositions=desired_context_dispositions,
        successor_text_context_dispositions=desired_text_context_dispositions,
    )
    unaffected_before_sha256 = _stable_record_set_sha256(unaffected_records)
    unaffected_after_records = tuple(
        item
        for item in artifact.records
        if _record_scope_id(item) not in affected_scope_ids
    )
    unaffected_after_sha256 = _stable_record_set_sha256(unaffected_after_records)
    if unaffected_before_sha256 != unaffected_after_sha256:
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_UNAFFECTED_RECORD_DRIFT"
        )

    matched_identities = tuple(
        sorted(
            (_revision_identity(item) for item in matched_records),
            key=lambda item: item.source_record_id,
        )
    )
    created_identities = tuple(
        sorted(
            (_revision_identity(item) for item in created_records),
            key=lambda item: item.source_record_id,
        )
    )
    tombstoned_identities = tuple(
        sorted(
            (_revision_identity(item) for item in tombstoned_records),
            key=lambda item: item.source_record_id,
        )
    )
    receipt_values = {
        "operation": scope_revision.operation,
        "scope_revision_sha256": scope_revision.revision_sha256,
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "predecessor_artifact_id": current_artifact.artifact_id,
        "predecessor_artifact_sha256": current_artifact.artifact_sha256,
        "predecessor_artifact_revision": current_artifact.revision,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "artifact_revision": artifact.revision,
        "predecessor_validation_receipt_sha256": (
            current_validation_receipt.receipt_sha256
        ),
        "validation_receipt_sha256": validation_receipt.receipt_sha256,
        "affected_scope_ids": scope_revision.affected_scope_ids,
        "comparison_scope_ids": scope_revision.comparison_scope_ids,
        "feedback_ids": scope_revision.feedback_ids,
        "unaffected_stable_records_before_sha256": unaffected_before_sha256,
        "unaffected_stable_records_after_sha256": unaffected_after_sha256,
        "affected_partition_before_sha256": affected_partition_before_sha256,
        "affected_partition_after_sha256": affected_partition_after_sha256,
        "matched_identities": matched_identities,
        "created_identities": created_identities,
        "tombstoned_identities": tombstoned_identities,
        "superseded_identities": tombstoned_identities,
        "successor_relations": successor_relations,
        "source_display_id_high_watermark_before": expected_high_watermark,
        "source_display_id_high_watermark_after": high_watermark_after,
        "coverage_before_sha256": current_validation_receipt.coverage_sha256,
        "coverage_after_sha256": validation_receipt.coverage_sha256,
        "undo_target_artifact_id": scope_revision.undo_target_artifact_id,
        "undo_target_artifact_sha256": scope_revision.undo_target_artifact_sha256,
        "undo_target_artifact_revision": scope_revision.undo_target_artifact_revision,
    }
    receipt_draft = SourceItemScopeRevisionReceiptV1.model_construct(
        **receipt_values,
        receipt_sha256="0" * 64,
    )
    receipt = SourceItemScopeRevisionReceiptV1(
        **receipt_values,
        receipt_sha256=canonical_json_sha256(
            receipt_draft.model_dump(mode="json", exclude={"receipt_sha256"})
        ),
    )
    return artifact, validation_receipt, receipt


def _replacement_scope_bindings(
    *,
    requests: tuple[SourceRequirementScopeRequestV1, ...],
    candidates: tuple[SourceRequirementSliceCandidateV1, ...],
    text_requests: tuple[SourceItemTextScopeRequestV1, ...],
    text_candidates: tuple[SourceItemTextSliceCandidateV1, ...],
) -> tuple[SourceItemScopeReplacementBindingV1, ...]:
    request_by_scope = {item.logical_table_scope_id: item for item in requests}
    candidate_by_scope = {item.logical_table_scope_id: item for item in candidates}
    text_request_by_scope = {item.scope_id: item for item in text_requests}
    text_candidate_by_scope = {item.scope_id: item for item in text_candidates}
    if (
        len(request_by_scope) != len(requests)
        or len(candidate_by_scope) != len(candidates)
        or set(request_by_scope) != set(candidate_by_scope)
        or len(text_request_by_scope) != len(text_requests)
        or len(text_candidate_by_scope) != len(text_candidates)
        or set(text_request_by_scope) != set(text_candidate_by_scope)
        or set(request_by_scope).intersection(text_request_by_scope)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_REPLACEMENT_SCOPE_SET_INVALID"
        )
    bindings = [
        SourceItemScopeReplacementBindingV1(
            scope_id=scope_id,
            scope_kind="TABLE",
            request_sha256=request_by_scope[scope_id].request_sha256,
            candidate_sha256=canonical_json_sha256(
                candidate_by_scope[scope_id].model_dump(mode="json")
            ),
        )
        for scope_id in request_by_scope
    ]
    bindings.extend(
        SourceItemScopeReplacementBindingV1(
            scope_id=scope_id,
            scope_kind="TEXT",
            request_sha256=text_request_by_scope[scope_id].request_sha256,
            candidate_sha256=canonical_json_sha256(
                text_candidate_by_scope[scope_id].model_dump(mode="json")
            ),
        )
        for scope_id in text_request_by_scope
    )
    return tuple(sorted(bindings, key=lambda item: item.scope_id))


def _compile_affected_scope_replacement(
    *,
    document_map: DocumentMapArtifactV1,
    current_status: Literal["SLICE_COMPLETE", "COMPLETE"],
    requests: tuple[SourceRequirementScopeRequestV1, ...],
    candidates: tuple[SourceRequirementSliceCandidateV1, ...],
    text_requests: tuple[SourceItemTextScopeRequestV1, ...],
    text_candidates: tuple[SourceItemTextSliceCandidateV1, ...],
) -> tuple[
    tuple[SourceRequirementRecordV1, ...],
    tuple[SourceRequirementContextDispositionV1, ...],
    tuple[SourceItemTextContextDispositionV1, ...],
    tuple[str, ...],
]:
    active_record_count = sum(len(item.records) for item in candidates) + sum(
        len(item.items) for item in text_candidates
    )
    if active_record_count or requests or candidates:
        artifact, _ = compile_source_requirement_baseline(
            document_map=document_map,
            requests=requests,
            candidates=candidates,
            text_requests=text_requests,
            text_candidates=text_candidates,
            status=current_status,
            starting_display_ordinal=1,
        )
        return (
            artifact.records,
            artifact.context_dispositions,
            artifact.text_context_dispositions,
            artifact.diagnostic_codes,
        )
    request_by_scope = {item.scope_id: item for item in text_requests}
    candidate_by_scope = {item.scope_id: item for item in text_candidates}
    if (
        not request_by_scope
        or len(request_by_scope) != len(text_requests)
        or len(candidate_by_scope) != len(text_candidates)
        or set(request_by_scope) != set(candidate_by_scope)
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_REPLACEMENT_SCOPE_SET_INVALID"
        )
    dispositions: list[SourceItemTextContextDispositionV1] = []
    diagnostics: set[str] = set()
    for scope_id in sorted(request_by_scope):
        request = request_by_scope[scope_id]
        candidate = candidate_by_scope[scope_id]
        validate_source_item_text_slice_candidate(request, candidate)
        for disposition in candidate.structure_only_spans:
            span = disposition.text_span
            dispositions.append(
                SourceItemTextContextDispositionV1(
                    scope_id=scope_id,
                    node_id=request.node_id,
                    content_id=span.content_id,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                    reason_codes=disposition.reason_codes,
                )
            )
        diagnostics.update(candidate.diagnostic_codes)
    return (), (), tuple(dispositions), tuple(sorted(diagnostics))


def _record_scope_id(record: SourceRequirementRecordV1) -> str:
    if record.image_bindings:
        return f"image:{record.node_id}"
    return record.logical_table_scope_id or f"text:{record.node_id}"


def _record_exact_evidence_payload(
    record: SourceRequirementRecordV1,
) -> dict[str, Any]:
    payload = {
        "scope_id": _record_scope_id(record),
        "node_id": record.node_id,
        "table_fragments": [
            item.model_dump(mode="json") for item in record.table_fragments
        ],
        "text_bindings": [
            item.model_dump(mode="json") for item in record.text_bindings
        ],
        "shared_header_cell_ids": record.shared_header_cell_ids,
        "shared_table_cell_ids": record.shared_table_cell_ids,
        "shared_source_content_ids": record.shared_source_content_ids,
    }
    if record.image_bindings:
        payload["image_bindings"] = [item.model_dump(mode="json") for item in record.image_bindings]
    return payload


def _record_exact_evidence_sha256(record: SourceRequirementRecordV1) -> str:
    return canonical_json_sha256(_record_exact_evidence_payload(record))


def _revision_identity(
    record: SourceRequirementRecordV1,
) -> SourceItemRevisionIdentityV1:
    return SourceItemRevisionIdentityV1(
        source_record_id=record.source_record_id,
        source_display_id=record.source_display_id,
        requirement_display_id=record.requirement_display_id,
        scope_id=_record_scope_id(record),
        exact_evidence_sha256=_record_exact_evidence_sha256(record),
    )


def _source_display_ordinal(source_display_id: str) -> int:
    return int(source_display_id.removeprefix("SRC-"))


def _baseline_display_high_watermark(artifact: SourceRequirementBaselineArtifactV1 | None) -> int:
    if artifact is None:
        return 0
    return max([artifact.source_display_high_watermark or 0,
                *[_source_display_ordinal(item.source_display_id) for item in artifact.records]])


def _inherit_source_display_high_watermark(previous, records, additional=0, *, floor=None):
    # None deliberately keeps the legacy artifact/hash policy for frozen replay.
    # New callers may supply a floor recovered from their verified source history.
    if floor is not None:
        _validate_source_display_high_watermark_floor(previous, floor)
    if floor is None and (previous is None or previous.source_display_high_watermark is None):
        return None
    return max([_baseline_display_high_watermark(previous), additional, floor or 0,
                *[_source_display_ordinal(item.source_display_id) for item in records]])


def _validate_source_display_high_watermark_floor(previous, floor):
    if type(floor) is not int or floor < _baseline_display_high_watermark(previous):
        raise SourceRequirementBaselineError("SOURCE_ITEM_HISTORY_HIGH_WATERMARK_INVALID")


SOURCE_IDENTITY_ALLOCATION_POLICY = "REVIEW_HISTORY_HIGH_WATERMARK_V1"


def validate_source_identity_allocation(previous, allocation):
    """Check a program history context at a shared-compiler boundary.

    The owning review verifier must also replay the named immutable prefix and
    compare the whole context. A self-consistent digest alone is not ancestry.
    """
    if allocation is None:
        return None
    expected_keys = {"policy", "review_sha256", "history_prefix_sha256", "source_sha256",
        "previous_baseline_artifact_id", "previous_baseline_sha256", "high_watermark", "allocation_sha256"}
    if (not isinstance(allocation, dict) or set(allocation) != expected_keys
            or allocation.get("policy") != SOURCE_IDENTITY_ALLOCATION_POLICY
            or allocation.get("source_sha256") != previous.source_sha256
            or allocation.get("previous_baseline_artifact_id") != previous.artifact_id
            or allocation.get("previous_baseline_sha256") != previous.artifact_sha256
            or any(re.fullmatch(r"[0-9a-f]{64}", str(allocation.get(key, ""))) is None
                   for key in ("review_sha256", "history_prefix_sha256"))
            or allocation.get("allocation_sha256") != canonical_json_sha256(
                {key: value for key, value in allocation.items() if key != "allocation_sha256"})):
        raise SourceRequirementBaselineError("SOURCE_ITEM_HISTORY_ALLOCATION_CONTEXT_DRIFT")
    floor = allocation["high_watermark"]
    _validate_source_display_high_watermark_floor(previous, floor)
    return floor


def _resolved_source_display_high_watermark(
    *,
    artifact: SourceRequirementBaselineArtifactV1,
    previous_scope_revision_receipt: SourceItemScopeRevisionReceiptV1 | None,
    source_display_high_watermark_floor: int | None = None,
) -> int:
    active_high_watermark = _baseline_display_high_watermark(artifact)
    if source_display_high_watermark_floor is not None:
        _validate_source_display_high_watermark_floor(artifact, source_display_high_watermark_floor)
    if previous_scope_revision_receipt is None:
        return max(active_high_watermark, source_display_high_watermark_floor or 0)
    if (
        previous_scope_revision_receipt.artifact_id != artifact.artifact_id
        or previous_scope_revision_receipt.artifact_sha256 != artifact.artifact_sha256
        or previous_scope_revision_receipt.artifact_revision != artifact.revision
        or previous_scope_revision_receipt.source_display_id_high_watermark_after
        < active_high_watermark
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_PREVIOUS_RECEIPT_LINEAGE_DRIFT"
        )
    receipt_floor = previous_scope_revision_receipt.source_display_id_high_watermark_after
    if source_display_high_watermark_floor is not None and source_display_high_watermark_floor < receipt_floor:
        raise SourceRequirementBaselineError("SOURCE_ITEM_HISTORY_HIGH_WATERMARK_BELOW_BOUND_RECEIPT")
    return max(receipt_floor, source_display_high_watermark_floor or 0)


def _document_map_content_catalog(
    document_map: DocumentMapArtifactV1,
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for node in document_map.nodes:
        for content in node.source_content:
            key = (node.node_id, content.content_id)
            if key in result and result[key] != content.text:
                raise SourceRequirementBaselineError(
                    "SOURCE_ITEM_SCOPE_REVISION_CONTENT_ID_CONFLICT"
                )
            result[key] = content.text
    return result


def _record_coverage_atoms(
    record: SourceRequirementRecordV1,
) -> set[tuple[str, str, str, int]]:
    scope_id = _record_scope_id(record)
    atoms: set[tuple[str, str, str, int]] = set()
    for fragment in record.table_fragments:
        atoms.update(
            ("TABLE", scope_id, fragment.fragment_id, row)
            for row in range(fragment.row_start, fragment.row_end)
        )
    for binding in record.text_bindings:
        if len(binding.text) != binding.end_offset - binding.start_offset:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_TEXT_BINDING_LENGTH_DRIFT"
            )
        atoms.update(
            ("TEXT", scope_id, binding.content_id, binding.start_offset + index)
            for index, character in enumerate(binding.text)
            if not character.isspace()
        )
    atoms.update(("IMAGE", scope_id, binding.anchor_id, 0) for binding in record.image_bindings)
    return atoms


def _scope_partition_snapshot(
    *,
    document_map: DocumentMapArtifactV1,
    records: tuple[SourceRequirementRecordV1, ...],
    context_dispositions: tuple[SourceRequirementContextDispositionV1, ...],
    text_context_dispositions: tuple[SourceItemTextContextDispositionV1, ...],
    scope_ids: set[str],
    subject: str,
    table_image_relations: tuple[SourceItemTableImageRelationV1, ...] = (),
) -> tuple[set[tuple[str, str, str, int]], str]:
    content_catalog = _document_map_content_catalog(document_map)
    assignments: dict[tuple[str, str, str, int], str] = {}
    record_payloads: list[dict[str, Any]] = []
    context_payloads: list[dict[str, Any]] = []

    def assign(atoms: set[tuple[str, str, str, int]], owner: str) -> None:
        for atom in atoms:
            if atom in assignments:
                raise SourceRequirementBaselineError(
                    f"SOURCE_ITEM_SCOPE_REVISION_{subject}_PARTITION_OVERLAP"
                )
            assignments[atom] = owner

    for record in sorted(
        records,
        key=lambda item: (_record_scope_id(item), _record_source_position(item)),
    ):
        scope_id = _record_scope_id(record)
        if scope_id not in scope_ids:
            raise SourceRequirementBaselineError(
                f"SOURCE_ITEM_SCOPE_REVISION_{subject}_SCOPE_LEAK"
            )
        validate_source_image_record_against_map(document_map, record)
        evidence_payload = _record_exact_evidence_payload(record)
        evidence_sha256 = canonical_json_sha256(evidence_payload)
        assign(_record_coverage_atoms(record), f"record:{evidence_sha256}")
        record_payloads.append(evidence_payload)
    _validate_table_image_relations(document_map, records, table_image_relations)
    for relation in table_image_relations:
        scope_id = f"image:{relation.node_id}"
        if scope_id not in scope_ids:
            raise SourceRequirementBaselineError("SOURCE_TABLE_IMAGE_SCOPE_LEAK")
        payload = {"kind": "TABLE_OWNED_IMAGE", **relation.model_dump(mode="json")}
        assign({("IMAGE", scope_id, relation.image_binding.anchor_id, 0)},
               f"table-image:{canonical_json_sha256(payload)}")
        context_payloads.append(payload)
    for scope_id in scope_ids:
        if scope_id.startswith("image:"):
            node = next((n for n in document_map.nodes if n.node_id == scope_id.removeprefix("image:")), None)
            expected = {a.anchor_id for a in node.source_anchors if a.anchor_kind in {"IMAGE", "FIGURE"}} if node else set()
            actual = {atom[2] for atom in assignments if atom[0] == "IMAGE" and atom[1] == scope_id}
            if not expected or actual != expected:
                raise SourceRequirementBaselineError("SOURCE_IMAGE_SCOPE_COVERAGE_INCOMPLETE")
    for disposition in sorted(
        context_dispositions,
        key=lambda item: (
            item.logical_table_scope_id,
            item.fragment_id,
            item.row_start,
        ),
    ):
        if disposition.logical_table_scope_id not in scope_ids:
            raise SourceRequirementBaselineError(
                f"SOURCE_ITEM_SCOPE_REVISION_{subject}_SCOPE_LEAK"
            )
        atoms = {
            (
                "TABLE",
                disposition.logical_table_scope_id,
                disposition.fragment_id,
                row,
            )
            for row in range(disposition.row_start, disposition.row_end)
        }
        payload = {
            "scope_id": disposition.logical_table_scope_id,
            "kind": "TABLE_CONTEXT",
            "role": disposition.role,
            "fragment_id": disposition.fragment_id,
            "row_start": disposition.row_start,
            "row_end": disposition.row_end,
            "cell_ids": disposition.cell_ids,
        }
        assign(atoms, f"context:{canonical_json_sha256(payload)}")
        context_payloads.append(payload)
    for disposition in sorted(
        text_context_dispositions,
        key=lambda item: (item.scope_id, item.content_id, item.start_offset),
    ):
        if disposition.scope_id not in scope_ids:
            raise SourceRequirementBaselineError(
                f"SOURCE_ITEM_SCOPE_REVISION_{subject}_SCOPE_LEAK"
            )
        text = content_catalog.get((disposition.node_id, disposition.content_id))
        if text is None or disposition.end_offset > len(text):
            raise SourceRequirementBaselineError(
                f"SOURCE_ITEM_SCOPE_REVISION_{subject}_TEXT_CONTEXT_DRIFT"
            )
        atoms = {
            ("TEXT", disposition.scope_id, disposition.content_id, offset)
            for offset in range(disposition.start_offset, disposition.end_offset)
            if not text[offset].isspace()
        }
        payload = {
            "scope_id": disposition.scope_id,
            "kind": "STRUCTURE_ONLY",
            "node_id": disposition.node_id,
            "content_id": disposition.content_id,
            "start_offset": disposition.start_offset,
            "end_offset": disposition.end_offset,
        }
        assign(atoms, f"context:{canonical_json_sha256(payload)}")
        context_payloads.append(payload)
    payload = {
        "scope_ids": sorted(scope_ids),
        "records": sorted(
            record_payloads, key=lambda item: canonical_json_sha256(item)
        ),
        "contexts": sorted(
            context_payloads, key=lambda item: canonical_json_sha256(item)
        ),
    }
    return set(assignments), canonical_json_sha256(payload)


def _compile_validation_receipt_from_artifact(
    *,
    document_map: DocumentMapArtifactV1,
    artifact: SourceRequirementBaselineArtifactV1,
) -> SourceRequirementBaselineValidationReceiptV1:
    _scope_partition_snapshot(
        document_map=document_map,
        records=artifact.records,
        context_dispositions=artifact.context_dispositions,
        text_context_dispositions=artifact.text_context_dispositions,
        scope_ids=set(artifact.covered_scope_ids),
        subject="ARTIFACT",
        table_image_relations=artifact.table_image_relations,
    )
    owned_cell_ids, context_cell_ids, shared_cell_ids = _artifact_cell_ownership_sets(
        artifact
    )
    owned_text_character_count = sum(
        not character.isspace()
        for record in artifact.records
        for binding in record.text_bindings
        for character in binding.text
    )
    content_catalog = _document_map_content_catalog(document_map)
    context_text_character_count = 0
    for disposition in artifact.text_context_dispositions:
        text = content_catalog.get((disposition.node_id, disposition.content_id))
        if text is None or disposition.end_offset > len(text):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_ARTIFACT_TEXT_CONTEXT_DRIFT"
            )
        context_text_character_count += sum(
            not text[offset].isspace()
            for offset in range(disposition.start_offset, disposition.end_offset)
        )
    coverage_sha256 = canonical_json_sha256(
        {
            "owned_cell_ids": sorted(owned_cell_ids),
            "context_cell_ids": sorted(context_cell_ids),
            "shared_cell_ids": sorted(shared_cell_ids),
            "text_context_dispositions": [
                item.model_dump(mode="json")
                for item in artifact.text_context_dispositions
            ],
            "owned_text_character_count": owned_text_character_count,
            "context_text_character_count": context_text_character_count,
            "covered_scope_ids": artifact.covered_scope_ids,
        }
    )
    values = {
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "document_map_artifact_id": artifact.document_map_artifact_id,
        "document_map_artifact_sha256": artifact.document_map_artifact_sha256,
        "scope_count": len(artifact.covered_scope_ids),
        "record_count": len(artifact.records),
        "requirement_count": sum(
            item.requirementness is Requirementness.REQUIREMENT
            for item in artifact.records
        ),
        "non_requirement_count": sum(
            item.requirementness is Requirementness.NON_REQUIREMENT
            for item in artifact.records
        ),
        "owned_cell_count": len(owned_cell_ids),
        "context_cell_count": len(context_cell_ids),
        "shared_cell_count": len(shared_cell_ids),
        "owned_text_character_count": owned_text_character_count,
        "context_text_character_count": context_text_character_count,
        "coverage_sha256": _with_image_coverage(artifact, coverage_sha256),
        "owned_image_count": sum(len(record.image_bindings) for record in artifact.records),
    }
    draft = SourceRequirementBaselineValidationReceiptV1.model_construct(
        **values,
        receipt_sha256="0" * 64,
    )
    return SourceRequirementBaselineValidationReceiptV1(
        **values,
        receipt_sha256=canonical_json_sha256(_receipt_hash_payload(draft)),
    )


def _assert_validation_receipt_matches_artifact(
    *,
    document_map: DocumentMapArtifactV1,
    artifact: SourceRequirementBaselineArtifactV1,
    receipt: SourceRequirementBaselineValidationReceiptV1,
    error_code: str,
) -> None:
    if (
        artifact.source_sha256 != document_map.source_sha256
        or artifact.document_map_artifact_id != document_map.artifact_id
        or artifact.document_map_artifact_sha256 != document_map.artifact_sha256
    ):
        raise SourceRequirementBaselineError(error_code)
    expected = _compile_validation_receipt_from_artifact(
        document_map=document_map,
        artifact=artifact,
    )
    if expected != receipt:
        raise SourceRequirementBaselineError(error_code)


def _scope_owner_node_id(
    document_map: DocumentMapArtifactV1,
    scope_id: str,
) -> str:
    if scope_id.startswith("text:"):
        return scope_id.removeprefix("text:")
    if scope_id.startswith("image:"):
        return scope_id.removeprefix("image:")
    owners = tuple(
        node.node_id
        for node in document_map.nodes
        if any(
            block.logical_table_scope_id == scope_id for block in node.content_blocks
        )
    )
    if len(owners) != 1:
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_SCOPE_REVISION_STRUCTURE_SUCCESSOR_AMBIGUOUS"
        )
    return owners[0]


def _compile_scope_successor_relations(
    *,
    document_map: DocumentMapArtifactV1,
    operation: SourceItemScopeRevisionOperation,
    tombstoned_records: tuple[SourceRequirementRecordV1, ...],
    successor_records: tuple[SourceRequirementRecordV1, ...],
    successor_context_dispositions: tuple[SourceRequirementContextDispositionV1, ...],
    successor_text_context_dispositions: tuple[SourceItemTextContextDispositionV1, ...],
) -> tuple[SourceItemSuccessorRelationV1, ...]:
    if not tombstoned_records:
        return ()
    successor_record_atoms = {
        item.source_record_id: _record_coverage_atoms(item)
        for item in successor_records
    }
    context_atoms_by_successor: dict[str, set[tuple[str, str, str, int]]] = {}
    for disposition in successor_context_dispositions:
        successor_id = "STRUCTURE_NODE:" + _scope_owner_node_id(
            document_map,
            disposition.logical_table_scope_id,
        )
        context_atoms_by_successor.setdefault(successor_id, set()).update(
            (
                "TABLE",
                disposition.logical_table_scope_id,
                disposition.fragment_id,
                row,
            )
            for row in range(disposition.row_start, disposition.row_end)
        )
    content_catalog = _document_map_content_catalog(document_map)
    for disposition in successor_text_context_dispositions:
        successor_id = f"STRUCTURE_NODE:{disposition.node_id}"
        text = content_catalog.get((disposition.node_id, disposition.content_id))
        if text is None or disposition.end_offset > len(text):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_SUCCESSOR_TEXT_CONTEXT_DRIFT"
            )
        context_atoms_by_successor.setdefault(successor_id, set()).update(
            ("TEXT", disposition.scope_id, disposition.content_id, offset)
            for offset in range(disposition.start_offset, disposition.end_offset)
            if not text[offset].isspace()
        )

    raw_successors: dict[str, tuple[str, ...]] = {}
    for record in tombstoned_records:
        atoms = _record_coverage_atoms(record)
        successor_ids = tuple(
            sorted(
                [
                    record_id
                    for record_id, successor_atoms in successor_record_atoms.items()
                    if atoms.intersection(successor_atoms)
                ]
                + [
                    successor_id
                    for successor_id, successor_atoms in context_atoms_by_successor.items()
                    if atoms.intersection(successor_atoms)
                ]
            )
        )
        if not successor_ids:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_SCOPE_REVISION_TOMBSTONE_SUCCESSOR_MISSING"
            )
        raw_successors[record.source_record_id] = successor_ids
    predecessor_count_by_successor: dict[str, int] = {}
    for successor_ids in raw_successors.values():
        for successor_id in successor_ids:
            if successor_id.startswith("source-record-"):
                predecessor_count_by_successor[successor_id] = (
                    predecessor_count_by_successor.get(successor_id, 0) + 1
                )

    relations: list[SourceItemSuccessorRelationV1] = []
    for record in sorted(tombstoned_records, key=lambda item: item.source_record_id):
        successor_ids = raw_successors[record.source_record_id]
        if all(item.startswith("STRUCTURE_NODE:") for item in successor_ids):
            relation = "STRUCTURE_ONLY"
        elif len(successor_ids) > 1:
            relation = "SPLIT"
        elif predecessor_count_by_successor.get(successor_ids[0], 0) > 1:
            relation = "MERGED"
        elif operation is SourceItemScopeRevisionOperation.UNDO:
            relation = "UNDO"
        else:
            relation = "REPLACED"
        relations.append(
            SourceItemSuccessorRelationV1(
                predecessor_source_record_id=record.source_record_id,
                successor_ids=successor_ids,
                relation=relation,
            )
        )
    return tuple(relations)


def _requirement_judgment(record):
    return (record.requirementness, tuple(record.requirement_kinds), record.non_requirement_role)


def _retain_reviewed_judgment(previous, proposed):
    """A representation-only rewrite cannot erase a person's existing judgment."""
    if previous.decision_origin != "USER_OVERRIDE" and previous.review_state != "REVIEWED":
        return proposed
    return proposed.model_copy(update={key: getattr(previous, key) for key in (
        "requirementness", "requirement_kinds", "non_requirement_role", "requirement_display_id",
        "confidence", "decision_summary", "reason_codes", "decision_origin", "review_state")})


def compile_source_requirement_classification_coordination(
    *,
    previous_artifact: SourceRequirementBaselineArtifactV1,
    previous_validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    decisions: tuple[SourceRequirementClassificationCoordinationDecisionV1, ...],
    model_id: str,
    coordinator_role: Literal["CODEX_APP_DEVELOPMENT_COORDINATOR", "APP_SERVER_WORKING_AGENT", "PORTABLE_AGENT"] = "CODEX_APP_DEVELOPMENT_COORDINATOR",
    source_display_high_watermark_floor: int | None = None,
) -> tuple[
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceRequirementClassificationCoordinationV1,
    SourceRequirementClassificationCoordinationReceiptV1,
]:
    if (
        previous_validation_receipt.artifact_id != previous_artifact.artifact_id
        or previous_validation_receipt.artifact_sha256
        != previous_artifact.artifact_sha256
        or previous_validation_receipt.document_map_artifact_id
        != previous_artifact.document_map_artifact_id
        or previous_validation_receipt.document_map_artifact_sha256
        != previous_artifact.document_map_artifact_sha256
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_COORDINATION_RECEIPT_LINEAGE_DRIFT"
        )
    if not decisions:
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_COORDINATION_DECISIONS_EMPTY"
        )
    records_by_id = {
        record.source_record_id: record for record in previous_artifact.records
    }
    decision_by_id = {decision.source_record_id: decision for decision in decisions}
    if len(decision_by_id) != len(decisions) or not set(decision_by_id).issubset(
        records_by_id
    ):
        raise SourceRequirementBaselineError(
            "SOURCE_REQUIREMENT_COORDINATION_RECORD_SET_INVALID"
        )
    for record_id, decision in decision_by_id.items():
        if (
            decision.requirementness is None
            and records_by_id[record_id].requirementness
            is not Requirementness.REQUIREMENT
        ):
            raise SourceRequirementBaselineError(
                "SOURCE_REQUIREMENT_COORDINATION_NON_REQUIREMENT_TARGET"
            )

    coordination_values = {
        "source_sha256": previous_artifact.source_sha256,
        "document_map_artifact_id": previous_artifact.document_map_artifact_id,
        "document_map_artifact_sha256": (
            previous_artifact.document_map_artifact_sha256
        ),
        "predecessor_artifact_id": previous_artifact.artifact_id,
        "predecessor_artifact_sha256": previous_artifact.artifact_sha256,
        "model_id": model_id,
        "coordinator_role": coordinator_role,
        "decisions": decisions,
    }
    coordination_draft = SourceRequirementClassificationCoordinationV1.model_construct(
        **coordination_values,
        coordination_sha256="0" * 64,
    )
    coordination_sha256 = canonical_json_sha256(
        coordination_draft.model_dump(mode="json", exclude={"coordination_sha256"})
    )
    coordination = SourceRequirementClassificationCoordinationV1(
        **coordination_values,
        coordination_sha256=coordination_sha256,
    )

    def coordinate_record(
        record: SourceRequirementRecordV1,
    ) -> SourceRequirementRecordV1:
        decision = decision_by_id.get(record.source_record_id)
        if decision is None:
            return record
        target_requirementness = decision.requirementness or record.requirementness
        if target_requirementness is Requirementness.REQUIREMENT:
            target_kinds = decision.requirement_kinds
            target_role = None
            target_requirement_id = record.requirement_display_id or (
                "REQ-" + record.source_display_id.removeprefix("SRC-")
            )
        else:
            target_kinds = ()
            target_role = decision.non_requirement_role
            target_requirement_id = None
        if record.decision_origin == "USER_OVERRIDE":
            if (target_requirementness, tuple(target_kinds), target_role) != _requirement_judgment(record):
                raise SourceRequirementBaselineError("SOURCE_ITEM_USER_OVERRIDE_REQUIRES_EXPLICIT_USER_DECISION")
            return record
        changed = ((target_requirementness, tuple(target_kinds), target_role) != _requirement_judgment(record)
            or decision.confidence != record.confidence or decision.decision_summary != record.decision_summary
            or decision.reason_codes != record.reason_codes)
        return record.model_copy(
            update={
                "requirementness": target_requirementness,
                "requirement_kinds": target_kinds,
                "non_requirement_role": target_role,
                "requirement_display_id": target_requirement_id,
                "confidence": decision.confidence,
                "decision_summary": decision.decision_summary,
                "reason_codes": decision.reason_codes,
                "decision_origin": "AGENT_DEFAULT" if changed else record.decision_origin,
                "review_state": "UNREVIEWED" if changed else record.review_state,
            }
        )

    coordinated_records = tuple(
        coordinate_record(record) for record in previous_artifact.records
    )
    artifact_values = {
        "revision": previous_artifact.revision + 1,
        "status": "SLICE_COMPLETE" if previous_artifact.status == "EMPTY" else previous_artifact.status,
        "source_sha256": previous_artifact.source_sha256,
        "document_map_artifact_id": previous_artifact.document_map_artifact_id,
        "document_map_artifact_sha256": (
            previous_artifact.document_map_artifact_sha256
        ),
        "predecessor_artifact_id": previous_artifact.artifact_id,
        "predecessor_artifact_sha256": previous_artifact.artifact_sha256,
        "covered_scope_ids": previous_artifact.covered_scope_ids,
        "records": coordinated_records,
        "source_display_high_watermark": _inherit_source_display_high_watermark(previous_artifact, coordinated_records, floor=source_display_high_watermark_floor),
        "table_image_relations": _inherit_table_image_relations(previous_artifact, coordinated_records),
        "context_dispositions": previous_artifact.context_dispositions,
        "text_context_dispositions": previous_artifact.text_context_dispositions,
        "diagnostic_codes": previous_artifact.diagnostic_codes,
    }
    artifact_draft = SourceRequirementBaselineArtifactV1.model_construct(
        **artifact_values,
        artifact_id="source-requirement-baseline-" + "0" * 32,
        artifact_sha256="0" * 64,
    )
    artifact_sha256 = canonical_json_sha256(_baseline_hash_payload(artifact_draft))
    artifact = SourceRequirementBaselineArtifactV1(
        **artifact_values,
        artifact_id=f"source-requirement-baseline-{artifact_sha256[:32]}",
        artifact_sha256=artifact_sha256,
    )
    validation_values = {
        **previous_validation_receipt.model_dump(
            mode="python",
            exclude={
                "artifact_id",
                "artifact_sha256",
                "requirement_count",
                "non_requirement_count",
                "receipt_sha256",
            },
        ),
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "requirement_count": sum(
            item.requirementness is Requirementness.REQUIREMENT
            for item in coordinated_records
        ),
        "non_requirement_count": sum(
            item.requirementness is Requirementness.NON_REQUIREMENT
            for item in coordinated_records
        ),
    }
    validation_draft = SourceRequirementBaselineValidationReceiptV1.model_construct(
        **validation_values,
        receipt_sha256="0" * 64,
    )
    validation_receipt_sha256 = canonical_json_sha256(
        _receipt_hash_payload(validation_draft)
    )
    validation_receipt = SourceRequirementBaselineValidationReceiptV1(
        **validation_values,
        receipt_sha256=validation_receipt_sha256,
    )
    coordination_receipt_values = {
        "coordination_sha256": coordination.coordination_sha256,
        "predecessor_artifact_id": previous_artifact.artifact_id,
        "predecessor_artifact_sha256": previous_artifact.artifact_sha256,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "decision_count": len(decisions),
        "preserved_record_count": len(previous_artifact.records),
        "preserved_evidence_sha256": previous_validation_receipt.coverage_sha256,
    }
    coordination_receipt_draft = (
        SourceRequirementClassificationCoordinationReceiptV1.model_construct(
            **coordination_receipt_values,
            receipt_sha256="0" * 64,
        )
    )
    coordination_receipt_sha256 = canonical_json_sha256(
        coordination_receipt_draft.model_dump(mode="json", exclude={"receipt_sha256"})
    )
    coordination_receipt = SourceRequirementClassificationCoordinationReceiptV1(
        **coordination_receipt_values,
        receipt_sha256=coordination_receipt_sha256,
    )
    return artifact, validation_receipt, coordination, coordination_receipt


def source_requirement_slice_candidate_output_schema() -> dict[str, Any]:
    return compile_strict_runner_output_schema(SourceRequirementSliceCandidateV1)


def source_item_text_slice_candidate_output_schema() -> dict[str, Any]:
    return compile_strict_runner_output_schema(SourceItemTextSliceCandidateV1)


def _inherit_table_image_relations(previous, records):
    if previous is None or not previous.table_image_relations:
        return ()
    by_id = {record.source_record_id: record for record in records}
    for relation in previous.table_image_relations:
        old = next(record for record in previous.records if record.source_record_id == relation.table_source_record_id)
        current = by_id.get(relation.table_source_record_id)
        if current is None or current.table_fragments != old.table_fragments:
            raise SourceRequirementBaselineError("TABLE_IMAGE_OWNERSHIP_REQUIRES_RECOMPILATION_AFTER_TABLE_CHANGE")
    return previous.table_image_relations


def _validate_table_image_relations(document_map, records, relations):
    by_id = {record.source_record_id: record for record in records}
    nodes = {node.node_id: node for node in document_map.nodes}
    claimed = set()
    for relation in relations:
        owner = by_id.get(relation.table_source_record_id)
        binding = relation.image_binding
        anchor_key = (relation.node_id, binding.anchor_id)
        node = nodes.get(relation.node_id)
        anchor = next((a for a in node.source_anchors if a.anchor_id == binding.anchor_id and a.anchor_kind in {"IMAGE", "FIGURE"}), None) if node else None
        if owner is None or not owner.table_fragments or owner.node_id != relation.node_id or owner.logical_table_scope_id != relation.logical_table_scope_id:
            raise SourceRequirementBaselineError("SOURCE_TABLE_IMAGE_OWNER_DRIFT")
        fragment = next((f for f in owner.table_fragments if f.fragment_id == relation.fragment_id), None)
        if (fragment is None or not set(relation.cell_ids) <= set(fragment.cell_ids)
                or len(set(relation.cell_ids)) != len(relation.cell_ids) or fragment.page != binding.page):
            raise SourceRequirementBaselineError("SOURCE_TABLE_IMAGE_CELL_OWNERSHIP_DRIFT")
        if anchor is None or binding.source_sha256 != document_map.source_sha256:
            raise SourceRequirementBaselineError("SOURCE_TABLE_IMAGE_ANCHOR_DRIFT")
        expected = SourceImageBindingV1(source_sha256=document_map.source_sha256,
            **{key: getattr(anchor, key) for key in ("anchor_id", "source_handle", "asset_id", "page", "bbox", "source_bounds", "precision")},
            **native_location_fields(anchor), **({"content_sha256": anchor.content_sha256} if anchor.content_sha256 else {}))
        if binding != expected or anchor_key in claimed:
            raise SourceRequirementBaselineError("SOURCE_TABLE_IMAGE_BINDING_DRIFT")
        claimed.add(anchor_key)


def _baseline_hash_payload(value: Any) -> dict[str, Any]:
    payload = value.model_dump(
        mode="json",
        exclude={"artifact_id", "artifact_sha256"},
    )
    if not payload.get("predecessor_artifact_id"):
        payload.pop("predecessor_artifact_id", None)
    if not payload.get("predecessor_artifact_sha256"):
        payload.pop("predecessor_artifact_sha256", None)
    if not payload.get("text_context_dispositions"):
        payload.pop("text_context_dispositions", None)
    if not payload.get("table_image_relations"):
        payload.pop("table_image_relations", None)
    if payload.get("source_display_high_watermark") is None:
        payload.pop("source_display_high_watermark", None)
    for record in payload.get("records", []):
        if not record.get("text_bindings"):
            record.pop("text_bindings", None)
        if not record.get("image_bindings"):
            record.pop("image_bindings", None)
    return payload


def _receipt_hash_payload(value: Any) -> dict[str, Any]:
    payload = value.model_dump(mode="json", exclude={"receipt_sha256"})
    if not payload.get("owned_text_character_count"):
        payload.pop("owned_text_character_count", None)
    if not payload.get("context_text_character_count"):
        payload.pop("context_text_character_count", None)
    if not payload.get("owned_image_count"):
        payload.pop("owned_image_count", None)
    return payload


def validate_source_image_record_against_map(document_map: DocumentMapArtifactV1, record: SourceRequirementRecordV1) -> None:
    if not record.image_bindings:
        return
    if record.document_map_artifact_sha256 != document_map.artifact_sha256:
        raise SourceRequirementBaselineError("SOURCE_IMAGE_MAP_LINEAGE_DRIFT")
    node = next((node for node in document_map.nodes if node.node_id == record.node_id), None)
    anchors = {a.anchor_id: a for a in node.source_anchors if a.anchor_kind in {"IMAGE", "FIGURE"}} if node else {}
    for binding in record.image_bindings:
        anchor = anchors.get(binding.anchor_id)
        if anchor is None:
            raise SourceRequirementBaselineError("SOURCE_IMAGE_ANCHOR_NOT_IN_MAP")
        expected = SourceImageBindingV1(source_sha256=document_map.source_sha256, anchor_id=anchor.anchor_id,
            source_handle=anchor.source_handle, asset_id=anchor.asset_id, page=anchor.page,
            bbox=anchor.bbox, source_bounds=anchor.source_bounds, precision=anchor.precision,
            **native_location_fields(anchor), **({"content_sha256": anchor.content_sha256} if anchor.content_sha256 else {}))
        if binding != expected:
            raise SourceRequirementBaselineError("SOURCE_IMAGE_BINDING_DRIFT")
    if record.pages != tuple(sorted({binding.page for binding in record.image_bindings if binding.page is not None})):
        raise SourceRequirementBaselineError("SOURCE_IMAGE_PAGE_BINDING_DRIFT")


def _with_image_coverage(artifact: SourceRequirementBaselineArtifactV1, original_sha256: str) -> str:
    images = [{"source_item_id": record.source_record_id, "node_id": record.node_id,
               "binding": binding.model_dump(mode="json")}
              for record in artifact.records for binding in record.image_bindings]
    if not images and not artifact.table_image_relations:
        return original_sha256
    payload = {"text_and_table_coverage_sha256": original_sha256, "images": images}
    if artifact.table_image_relations:
        payload["table_image_relations"] = [r.model_dump(mode="json") for r in artifact.table_image_relations]
    return canonical_json_sha256(payload)


def canonical_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_reason_codes(
    value: tuple[str, ...], *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not allow_empty and not value:
        raise ValueError("reason codes cannot be empty")
    if value != tuple(sorted(set(value))) or any(
        re.fullmatch(r"[a-z][a-z0-9_]{0,79}", item) is None for item in value
    ):
        raise ValueError("reason codes must be sorted lowercase machine codes")
    return value


def _require_bbox(value: tuple[float, ...], *, subject: str) -> None:
    if (
        len(value) != 4
        or not all(math.isfinite(item) for item in value)
        or value[2] <= value[0]
        or value[3] <= value[1]
    ):
        raise ValueError(f"{subject} bbox is invalid")


def _cells_for_rows(
    fragment: SourceTableFragmentV1, row_start: int, row_end: int
) -> tuple[SourceTableCellV1, ...]:
    cells = tuple(
        sorted(
            (
                item
                for item in fragment.cells
                if item.row < row_end and item.row + item.row_span > row_start
            ),
            key=lambda item: (item.row, item.column, item.cell_id),
        )
    )
    if not cells:
        raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_ROW_GROUP_EMPTY")
    return cells


def _applicable_header_cell_ids(
    *,
    header_contexts: tuple[tuple[str, int, int, tuple[str, ...]], ...],
    record_groups: tuple[SourceRequirementRowGroupV1, ...],
) -> tuple[str, ...]:
    """Return the nearest contiguous header band for each record fragment."""

    selected: set[str] = set()
    for group in record_groups:
        candidates = sorted(
            (
                item
                for item in header_contexts
                if item[0] == group.fragment_id and item[2] <= group.row_start
            ),
            key=lambda item: (item[2], item[1], item[3]),
        )
        if not candidates:
            continue
        current = candidates[-1]
        band = [current]
        cursor = current[1]
        for item in reversed(candidates[:-1]):
            if item[2] == cursor:
                band.append(item)
                cursor = item[1]
                continue
            if item[2] < cursor:
                break
        for item in band:
            selected.update(item[3])
    return tuple(sorted(selected))


def _union_bbox(
    values: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    if not values:
        raise SourceRequirementBaselineError("SOURCE_REQUIREMENT_BBOX_MISSING")
    return (
        min(item[0] for item in values),
        min(item[1] for item in values),
        max(item[2] for item in values),
        max(item[3] for item in values),
    )


__all__ = [
    "ContextRowRole",
    "DocumentMapArtifactV1",
    "NonRequirementRole",
    "RequirementKind",
    "Requirementness",
    "SourceItemStructureOnlySpanV1",
    "SourceItemRevisionIdentityV1",
    "SourceItemScopeReplacementBindingV1",
    "SourceItemScopeRevisionOperation",
    "SourceItemScopeRevisionReceiptV1",
    "SourceItemScopeRevisionV1",
    "SourceItemSuccessorRelationV1",
    "SourceItemTextBindingV1",
    "SourceItemTextBoundaryCandidateV1",
    "SourceItemTextScopeRequestV1",
    "SourceItemTextSliceCandidateV1",
    "SourceItemTextSpanV1",
    "SourceRecordForm",
    "SourceRequirementBaselineArtifactV1",
    "SourceRequirementBaselineAppendReceiptV1",
    "SourceRequirementBaselineError",
    "SourceRequirementBaselineGrowthReceiptV1",
    "SourceRequirementBaselineValidationReceiptV1",
    "SourceRequirementBoundaryCandidateV1",
    "SourceRequirementContextRowGroupV1",
    "SourceRequirementRowGroupV1",
    "SourceRequirementScopeRequestV1",
    "SourceRequirementSliceCandidateV1",
    "build_source_item_text_scope_request",
    "build_source_requirement_scope_request",
    "append_source_requirement_baseline",
    "build_source_item_scope_revision",
    "canonical_json_sha256",
    "compile_document_map_artifact",
    "compile_source_item_scope_revision",
    "compile_source_requirement_baseline",
    "grow_source_requirement_baseline",
    "source_item_text_slice_candidate_output_schema",
    "source_requirement_slice_candidate_output_schema",
    "validate_source_item_text_slice_candidate",
    "validate_source_requirement_row_partition",
    "validate_source_requirement_slice_candidate",
]
