from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from reqpilotx_server.application.agent_workflow.d2_logical_structure import (
    D2SourceHandleKind,
    D2SourceSegmentV1,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    AgentLogicalTablePhysicalPartV1,
    AgentLogicalTableScopeV1,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    DocumentTableCellFactV1,
)


D2_LOGICAL_TABLE_CANDIDATE_INVENTORY_SCHEMA_VERSION = (
    "d2-logical-table-candidate-inventory/v1"
)
D2_LOGICAL_TABLE_EVIDENCE_SCHEMA_VERSION = "d2-logical-table-evidence/v1"
D2_RESOLVED_SOURCE_SEGMENT_BATCH_SCHEMA_VERSION = "d2-resolved-source-segment-batch/v1"
D2_RESOLVER_SUGGESTION_BATCH_SCHEMA_VERSION = "d2-resolver-suggestion-batch/v1"
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D2EvidenceToolContextV1(_StrictFrozenModel):
    source_handle: str = Field(pattern=r"^source-handle-[0-9a-f]{32}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_d1_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    expected_d1_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    expected_d1_table_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class D2LogicalTableCandidatesInputV1(_StrictFrozenModel):
    context: D2EvidenceToolContextV1
    context_nodes_per_side: StrictInt = Field(default=3, ge=0, le=8)


class D2LogicalTableEvidenceInputV1(_StrictFrozenModel):
    context: D2EvidenceToolContextV1
    logical_table_scope: AgentLogicalTableScopeV1
    context_nodes_per_side: StrictInt = Field(default=3, ge=0, le=8)


class D2SourceHandleSelectionV1(_StrictFrozenModel):
    handle_kind: D2SourceHandleKind
    source_handle: str = Field(min_length=1, max_length=160)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)

    @field_validator("source_handle")
    @classmethod
    def reject_blank_handle(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 source handle must not be blank")
        return value

    @model_validator(mode="after")
    def validate_offsets(self) -> D2SourceHandleSelectionV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("D2 source-handle offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("D2 source-handle range must be non-empty")
        return self


class D2ResolveSourceHandlesInputV1(_StrictFrozenModel):
    context: D2EvidenceToolContextV1
    selections: tuple[D2SourceHandleSelectionV1, ...] = Field(
        min_length=1,
        max_length=512,
    )

    @field_validator("selections")
    @classmethod
    def reject_duplicate_selections(
        cls,
        value: tuple[D2SourceHandleSelectionV1, ...],
    ) -> tuple[D2SourceHandleSelectionV1, ...]:
        keys = tuple(
            (
                item.handle_kind,
                item.source_handle,
                item.start_offset,
                item.end_offset,
            )
            for item in value
        )
        if len(keys) != len(set(keys)):
            raise ValueError("D2 source-handle selections must be unique")
        return value


class D2ResolverSuggestionsInputV1(_StrictFrozenModel):
    context: D2EvidenceToolContextV1
    physical_table_ids: tuple[str, ...] = Field(default=(), max_length=512)

    @field_validator("physical_table_ids")
    @classmethod
    def validate_table_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("D2 resolver table IDs must be unique and non-empty")
        return value


class D2SourceContextEvidenceV1(_StrictFrozenModel):
    source_handle: str = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...]
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    text: str = Field(min_length=1, max_length=100_000)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_text_and_pages(self) -> D2SourceContextEvidenceV1:
        if self.page_numbers != tuple(sorted(set(self.page_numbers))) or any(
            page < 1 for page in self.page_numbers
        ):
            raise ValueError("D2 context pages must be positive and sorted")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("D2 context text SHA does not bind its exact text")
        return self


class D2PhysicalTableEvidenceV1(_StrictFrozenModel):
    physical_part: AgentLogicalTablePhysicalPartV1
    parent_source_node_id: str | None = Field(default=None, max_length=160)
    bbox: tuple[float, float, float, float] | None = None
    header_regions: tuple[tuple[StrictInt, StrictInt, StrictInt, StrictInt], ...]
    merged_regions: tuple[tuple[StrictInt, StrictInt, StrictInt, StrictInt], ...]
    notes: tuple[str, ...]
    cells: tuple[DocumentTableCellFactV1, ...]
    leading_context: tuple[D2SourceContextEvidenceV1, ...] = ()
    trailing_context: tuple[D2SourceContextEvidenceV1, ...] = ()
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_table_evidence(self) -> D2PhysicalTableEvidenceV1:
        cell_keys = tuple((item.row, item.column, item.cell_id) for item in self.cells)
        if cell_keys != tuple(sorted(cell_keys)) or len(cell_keys) != len(
            set(cell_keys)
        ):
            raise ValueError("D2 table cells must be unique and canonically ordered")
        if tuple(item.cell_id for item in self.cells) != self.physical_part.cell_ids:
            raise ValueError("D2 table cells drifted from the physical-part binding")
        context_handles = tuple(
            item.source_handle
            for item in (*self.leading_context, *self.trailing_context)
        )
        if len(context_handles) != len(set(context_handles)):
            raise ValueError("D2 table context handles must be unique")
        expected_sha = canonical_d2_tool_sha256(
            self.model_dump(mode="json", exclude={"evidence_sha256"})
        )
        if self.evidence_sha256 != expected_sha:
            raise ValueError("D2 physical-table evidence SHA is invalid")
        return self


class D2TableAdjacencyCandidateV1(_StrictFrozenModel):
    left_table_id: str = Field(min_length=1, max_length=160)
    right_table_id: str = Field(min_length=1, max_length=160)
    left_page_numbers: tuple[StrictInt, ...]
    right_page_numbers: tuple[StrictInt, ...]
    source_order_gap: StrictInt = Field(ge=1)
    page_gap: StrictInt = Field(ge=0)
    same_column_count: StrictBool
    same_header_contract: StrictBool
    evidence_refs: tuple[str, ...] = Field(min_length=2)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D2 candidate evidence refs must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_candidate(self) -> D2TableAdjacencyCandidateV1:
        if self.left_table_id == self.right_table_id:
            raise ValueError("D2 adjacency endpoints must differ")
        expected_sha = canonical_d2_tool_sha256(
            self.model_dump(mode="json", exclude={"candidate_sha256"})
        )
        if self.candidate_sha256 != expected_sha:
            raise ValueError("D2 adjacency candidate SHA is invalid")
        return self


class D2LogicalTableCandidateInventoryV1(_StrictFrozenModel):
    schema_version: Literal["d2-logical-table-candidate-inventory/v1"] = (
        D2_LOGICAL_TABLE_CANDIDATE_INVENTORY_SCHEMA_VERSION
    )
    authority: Literal["SOURCE_EVIDENCE_WITH_ADVISORY_ADJACENCY"] = (
        "SOURCE_EVIDENCE_WITH_ADVISORY_ADJACENCY"
    )
    content_trust: Literal["UNTRUSTED_SOURCE_DATA"] = "UNTRUSTED_SOURCE_DATA"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    d1_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_tables: tuple[D2PhysicalTableEvidenceV1, ...]
    adjacency_candidates: tuple[D2TableAdjacencyCandidateV1, ...]
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_inventory(self) -> D2LogicalTableCandidateInventoryV1:
        table_order = tuple(
            (item.physical_part.source_order, item.physical_part.table_id)
            for item in self.physical_tables
        )
        if table_order != tuple(sorted(table_order)):
            raise ValueError("D2 table inventory must follow source order")
        if len({item[1] for item in table_order}) != len(table_order):
            raise ValueError("D2 table inventory contains duplicate tables")
        expected_sha = canonical_d2_tool_sha256(
            self.model_dump(mode="json", exclude={"inventory_sha256"})
        )
        if self.inventory_sha256 != expected_sha:
            raise ValueError("D2 table inventory SHA is invalid")
        return self


class D2LogicalTableEvidenceV1(_StrictFrozenModel):
    schema_version: Literal["d2-logical-table-evidence/v1"] = (
        D2_LOGICAL_TABLE_EVIDENCE_SCHEMA_VERSION
    )
    authority: Literal["HOST_VALIDATED_STAGE_EVIDENCE"] = (
        "HOST_VALIDATED_STAGE_EVIDENCE"
    )
    content_trust: Literal["UNTRUSTED_SOURCE_DATA"] = "UNTRUSTED_SOURCE_DATA"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope: AgentLogicalTableScopeV1
    physical_tables: tuple[D2PhysicalTableEvidenceV1, ...] = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_logical_table(self) -> D2LogicalTableEvidenceV1:
        if (
            tuple(item.physical_part.table_id for item in self.physical_tables)
            != self.logical_table_scope.physical_table_ids
        ):
            raise ValueError("D2 logical-table evidence parts drifted from its scope")
        expected_sha = canonical_d2_tool_sha256(
            self.model_dump(mode="json", exclude={"evidence_sha256"})
        )
        if self.evidence_sha256 != expected_sha:
            raise ValueError("D2 logical-table evidence SHA is invalid")
        return self


class D2ResolvedSourceSegmentV1(_StrictFrozenModel):
    segment: D2SourceSegmentV1
    exact_text: str = Field(max_length=1_000_000)
    exact_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_exact_text(self) -> D2ResolvedSourceSegmentV1:
        if hashlib.sha256(self.exact_text.encode("utf-8")).hexdigest() != (
            self.exact_text_sha256
        ):
            raise ValueError("D2 resolved exact-text SHA is invalid")
        if self.segment.start_offset is not None:
            assert self.segment.end_offset is not None
            if len(self.exact_text) != (
                self.segment.end_offset - self.segment.start_offset
            ):
                raise ValueError("D2 resolved text length differs from its exact range")
        elif self.exact_text:
            raise ValueError("unranged D2 source evidence cannot expose text")
        return self


class D2ResolvedSourceSegmentBatchV1(_StrictFrozenModel):
    schema_version: Literal["d2-resolved-source-segment-batch/v1"] = (
        D2_RESOLVED_SOURCE_SEGMENT_BATCH_SCHEMA_VERSION
    )
    authority: Literal["SOURCE_GROUNDING"] = "SOURCE_GROUNDING"
    content_trust: Literal["UNTRUSTED_SOURCE_DATA"] = "UNTRUSTED_SOURCE_DATA"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    segments: tuple[D2ResolvedSourceSegmentV1, ...] = Field(min_length=1)
    batch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_batch(self) -> D2ResolvedSourceSegmentBatchV1:
        ids = tuple(item.segment.segment_id for item in self.segments)
        if len(ids) != len(set(ids)):
            raise ValueError("D2 resolved segment IDs must be unique")
        expected_sha = canonical_d2_tool_sha256(
            self.model_dump(mode="json", exclude={"batch_sha256"})
        )
        if self.batch_sha256 != expected_sha:
            raise ValueError("D2 resolved segment batch SHA is invalid")
        return self


class D2ResolverSuggestionKind(StrEnum):
    CONTINUES = "CONTINUES"
    NESTED_VALUE = "NESTED_VALUE"
    SEPARATE_LOGICAL_OBJECT = "SEPARATE_LOGICAL_OBJECT"
    OTHER = "OTHER"


class D2ResolverSuggestionV1(_StrictFrozenModel):
    suggestion_id: str = Field(pattern=r"^d2-resolver-suggestion-[0-9a-f]{32}$")
    kind: D2ResolverSuggestionKind
    left_table_id: str = Field(min_length=1, max_length=160)
    right_table_id: str = Field(min_length=1, max_length=160)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    evidence_refs: tuple[str, ...] = Field(min_length=2, max_length=64)
    suggestion_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("D2 resolver reason codes must be lowercase snake_case")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D2 resolver evidence refs must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_suggestion(self) -> D2ResolverSuggestionV1:
        if self.left_table_id == self.right_table_id:
            raise ValueError("D2 resolver suggestion endpoints must differ")
        identity = canonical_d2_tool_sha256(
            {
                "kind": self.kind.value,
                "left_table_id": self.left_table_id,
                "right_table_id": self.right_table_id,
                "reason_codes": list(self.reason_codes),
                "evidence_refs": list(self.evidence_refs),
            }
        )
        if self.suggestion_id != f"d2-resolver-suggestion-{identity[:32]}":
            raise ValueError("D2 resolver suggestion ID is invalid")
        expected_sha = canonical_d2_tool_sha256(
            self.model_dump(mode="json", exclude={"suggestion_sha256"})
        )
        if self.suggestion_sha256 != expected_sha:
            raise ValueError("D2 resolver suggestion SHA is invalid")
        return self


class D2ResolverSuggestionProviderStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class D2ResolverSuggestionBatchV1(_StrictFrozenModel):
    schema_version: Literal["d2-resolver-suggestion-batch/v1"] = (
        D2_RESOLVER_SUGGESTION_BATCH_SCHEMA_VERSION
    )
    authority: Literal["ADVISORY_EVIDENCE"] = "ADVISORY_EVIDENCE"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_status: D2ResolverSuggestionProviderStatus
    suggestions: tuple[D2ResolverSuggestionV1, ...]
    batch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_batch(self) -> D2ResolverSuggestionBatchV1:
        if (
            self.provider_status is D2ResolverSuggestionProviderStatus.NOT_CONFIGURED
            and self.suggestions
        ):
            raise ValueError("an unconfigured resolver cannot return suggestions")
        ids = tuple(item.suggestion_id for item in self.suggestions)
        if len(ids) != len(set(ids)):
            raise ValueError("D2 resolver suggestion IDs must be unique")
        expected_sha = canonical_d2_tool_sha256(
            self.model_dump(mode="json", exclude={"batch_sha256"})
        )
        if self.batch_sha256 != expected_sha:
            raise ValueError("D2 resolver suggestion batch SHA is invalid")
        return self


def canonical_d2_tool_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "D2EvidenceToolContextV1",
    "D2LogicalTableCandidateInventoryV1",
    "D2LogicalTableCandidatesInputV1",
    "D2LogicalTableEvidenceInputV1",
    "D2LogicalTableEvidenceV1",
    "D2PhysicalTableEvidenceV1",
    "D2ResolveSourceHandlesInputV1",
    "D2ResolvedSourceSegmentBatchV1",
    "D2ResolvedSourceSegmentV1",
    "D2ResolverSuggestionBatchV1",
    "D2ResolverSuggestionKind",
    "D2ResolverSuggestionProviderStatus",
    "D2ResolverSuggestionV1",
    "D2ResolverSuggestionsInputV1",
    "D2SourceContextEvidenceV1",
    "D2SourceHandleSelectionV1",
    "D2TableAdjacencyCandidateV1",
    "canonical_d2_tool_sha256",
]
