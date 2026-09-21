from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


DOCUMENT_RUN_MANIFEST_SCHEMA_VERSION = "document-run-manifest/v2"
DOCUMENT_PAGE_LEDGER_SCHEMA_VERSION = "document-page-ledger/v2"
DOCUMENT_PAGE_FACTS_SCHEMA_VERSION = "document-page-facts/v1"
DOCUMENT_PAGE_TEXT_SCHEMA_VERSION = "document-page-text/v1"
DOCUMENT_ASSET_LEDGER_SCHEMA_VERSION = "document-asset-ledger/v1"
DOCUMENT_TABLE_MAP_SCHEMA_VERSION = "document-table-map/v1"
DOCUMENT_RENDER_RECEIPT_SCHEMA_VERSION = "document-render-receipt/v1"
DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION = (
    "document-understanding-proposal/v2"
)
DOCUMENT_UNDERSTANDING_VALIDATION_SCHEMA_VERSION = (
    "document-understanding-validation-receipt/v2"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class DocumentEvidenceRequirement(StrEnum):
    NATIVE = "NATIVE"
    VISUAL_REQUIRED = "VISUAL_REQUIRED"
    NATIVE_AND_VISUAL = "NATIVE_AND_VISUAL"
    INTENTIONAL_BLANK = "INTENTIONAL_BLANK"
    UNRESOLVED = "UNRESOLVED"


class DocumentEvidenceAuthority(StrEnum):
    NATIVE = "NATIVE"
    VISUAL = "VISUAL"
    NATIVE_AND_VISUAL = "NATIVE_AND_VISUAL"
    INTENTIONAL_BLANK = "INTENTIONAL_BLANK"
    UNRESOLVED = "UNRESOLVED"


class DocumentUnderstandingStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class DocumentRenderDetail(StrEnum):
    THUMBNAIL = "THUMBNAIL"
    HIGH = "HIGH"
    ORIGINAL = "ORIGINAL"
    REGION = "REGION"


class DocumentVisualObservationKind(StrEnum):
    HEADING = "HEADING"
    TEXT = "TEXT"
    TABLE = "TABLE"
    FIGURE = "FIGURE"
    MARK = "MARK"
    OTHER = "OTHER"


class DocumentSourceHandleInputV1(_StrictFrozenModel):
    source_handle: str | None = Field(
        default=None,
        pattern=r"^source-handle-[0-9a-f]{32}$",
    )


class DocumentRunManifestInputV2(DocumentSourceHandleInputV1):
    stage: Literal["D1_DOCUMENT_UNDERSTANDING"] | None = None
    stage_context_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    kit_bundle_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class DocumentPageInputV1(DocumentSourceHandleInputV1):
    page_number: StrictInt = Field(ge=1)


class DocumentPageWindowInputV1(DocumentPageInputV1):
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=128, ge=1, le=1024)


class DocumentPageTextWindowInputV1(DocumentPageInputV1):
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=64, ge=1, le=512)


class DocumentRenderPageInputV1(DocumentPageInputV1):
    detail: Literal["THUMBNAIL", "HIGH", "ORIGINAL"]


class DocumentRenderRegionInputV1(DocumentPageInputV1):
    bbox: tuple[float, float, float, float]
    scale: float = Field(default=3.0, gt=0.0, le=4.0)


class DocumentTableMapInputV1(DocumentSourceHandleInputV1):
    table_id: str = Field(min_length=1, max_length=160)
    include_cells: bool = False
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=100, ge=1, le=200)


class DocumentRunManifestV2(_StrictFrozenModel):
    schema_version: Literal["document-run-manifest/v2"] = (
        DOCUMENT_RUN_MANIFEST_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    opaque_source_handle: str = Field(pattern=r"^source-handle-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    document_role: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    source_adapter_id: str = Field(min_length=1, max_length=160)
    source_adapter_version: str = Field(min_length=1, max_length=160)
    page_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    intentional_blank_pages: tuple[StrictInt, ...] = ()

    @field_validator("intentional_blank_pages")
    @classmethod
    def validate_blank_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("intentional blank pages must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_page_scope(self) -> DocumentRunManifestV2:
        if any(page > self.page_count for page in self.intentional_blank_pages):
            raise ValueError("intentional blank page exceeds the source document")
        return self


class DocumentPageLedgerEntryV2(_StrictFrozenModel):
    page_number: StrictInt = Field(ge=1)
    bounds: tuple[float, float, float, float]
    rotation_cw_degrees: Literal[0, 90, 180, 270]
    evidence_requirement: DocumentEvidenceRequirement
    native_node_count: StrictInt = Field(ge=0)
    native_text_char_count: StrictInt = Field(ge=0)
    table_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    diagnostic_codes: tuple[str, ...] = ()
    visual_review_reasons: tuple[str, ...] = ()
    page_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "table_ids",
        "asset_ids",
        "diagnostic_codes",
        "visual_review_reasons",
    )
    @classmethod
    def validate_sorted_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("page ledger values must be unique and sorted")
        return value


class DocumentPageLedgerV2(_StrictFrozenModel):
    schema_version: Literal["document-page-ledger/v2"] = (
        DOCUMENT_PAGE_LEDGER_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    pages: tuple[DocumentPageLedgerEntryV2, ...]
    ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_ledger(self) -> DocumentPageLedgerV2:
        if tuple(page.page_number for page in self.pages) != tuple(
            range(1, self.page_count + 1)
        ):
            raise ValueError("page ledger must cover every page exactly once")
        expected = document_page_ledger_sha256(
            source_sha256=self.source_sha256,
            page_count=self.page_count,
            pages=self.pages,
        )
        if self.ledger_sha256 != expected:
            raise ValueError("page ledger SHA must bind every page entry")
        return self


class DocumentPageFactV1(_StrictFrozenModel):
    source_handle: str = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    bbox: tuple[float, float, float, float] | None = None
    parent_source_handle: str | None = Field(default=None, max_length=160)
    table_id: str | None = Field(default=None, max_length=160)
    asset_id: str | None = Field(default=None, max_length=160)
    raw_text_length: StrictInt = Field(ge=0)
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostic_codes: tuple[str, ...] = ()


class DocumentPageFactsChunkV1(_StrictFrozenModel):
    schema_version: Literal["document-page-facts/v1"] = (
        DOCUMENT_PAGE_FACTS_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: StrictInt = Field(ge=1)
    page_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    offset: StrictInt = Field(ge=0)
    items: tuple[DocumentPageFactV1, ...]
    next_offset: StrictInt | None = Field(default=None, ge=0)


class DocumentPageTextItemV1(_StrictFrozenModel):
    source_handle: str = Field(min_length=1, max_length=160)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    text: str
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_text_sha(self) -> DocumentPageTextItemV1:
        if self.text_sha256 != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("page text SHA must bind the exact text")
        return self


class DocumentPageTextChunkV1(_StrictFrozenModel):
    schema_version: Literal["document-page-text/v1"] = DOCUMENT_PAGE_TEXT_SCHEMA_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: StrictInt = Field(ge=1)
    page_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    offset: StrictInt = Field(ge=0)
    items: tuple[DocumentPageTextItemV1, ...]
    next_offset: StrictInt | None = Field(default=None, ge=0)


class DocumentAssetFactV1(_StrictFrozenModel):
    asset_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=160)
    page_number: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None


class DocumentAssetLedgerV1(_StrictFrozenModel):
    schema_version: Literal["document-asset-ledger/v1"] = (
        DOCUMENT_ASSET_LEDGER_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: StrictInt = Field(ge=1)
    assets: tuple[DocumentAssetFactV1, ...]
    assets_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentTableCellFactV1(_StrictFrozenModel):
    cell_id: str = Field(min_length=1, max_length=160)
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(ge=1)
    column_span: StrictInt = Field(ge=1)
    text: str
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_text_sha(self) -> DocumentTableCellFactV1:
        if self.text_sha256 != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("table cell SHA must bind the exact text")
        return self


class DocumentTableMapChunkV1(_StrictFrozenModel):
    schema_version: Literal["document-table-map/v1"] = DOCUMENT_TABLE_MAP_SCHEMA_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    table_id: str = Field(min_length=1, max_length=160)
    page_number: StrictInt = Field(ge=1)
    bbox: tuple[float, float, float, float] | None = None
    row_count: StrictInt = Field(ge=1)
    column_count: StrictInt = Field(ge=1)
    header_regions: tuple[tuple[StrictInt, StrictInt, StrictInt, StrictInt], ...]
    merged_regions: tuple[tuple[StrictInt, StrictInt, StrictInt, StrictInt], ...]
    notes: tuple[str, ...]
    table_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cells_included: bool
    offset: StrictInt = Field(ge=0)
    cells: tuple[DocumentTableCellFactV1, ...]
    next_offset: StrictInt | None = Field(default=None, ge=0)


class DocumentRenderReceiptV1(_StrictFrozenModel):
    schema_version: Literal["document-render-receipt/v1"] = (
        DOCUMENT_RENDER_RECEIPT_SCHEMA_VERSION
    )
    render_receipt_id: str = Field(pattern=r"^document-render-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: StrictInt = Field(ge=1)
    detail: DocumentRenderDetail
    bbox: tuple[float, float, float, float]
    scale: float = Field(gt=0.0, le=4.0)
    width_px: StrictInt = Field(ge=1)
    height_px: StrictInt = Field(ge=1)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["image/png"] = "image/png"


class DocumentVisualObservationV2(_StrictFrozenModel):
    kind: DocumentVisualObservationKind
    visible_text: str = Field(default="", max_length=4000)
    bbox: tuple[float, float, float, float] | None = None
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_handles: tuple[str, ...] = ()

    @field_validator("source_handles")
    @classmethod
    def validate_source_handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("visual source handles must be unique and sorted")
        return value


class DocumentUnderstandingPageV2(_StrictFrozenModel):
    page_number: StrictInt = Field(ge=1)
    authority: DocumentEvidenceAuthority
    page_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    table_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    visual_observations: tuple[DocumentVisualObservationV2, ...] = ()
    unresolved_codes: tuple[str, ...] = ()

    @field_validator("table_ids", "asset_ids", "unresolved_codes")
    @classmethod
    def validate_sorted_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("understanding page values must be unique and sorted")
        return value


class DocumentUnderstandingProposalV2(_StrictFrozenModel):
    schema_version: Literal["document-understanding-proposal/v2"] = (
        DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DocumentUnderstandingStatus
    pages: tuple[DocumentUnderstandingPageV2, ...]

    @model_validator(mode="after")
    def validate_pages(self) -> DocumentUnderstandingProposalV2:
        if tuple(page.page_number for page in self.pages) != tuple(
            range(1, self.page_count + 1)
        ):
            raise ValueError("understanding proposal must cover every page exactly once")
        unresolved = any(page.unresolved_codes for page in self.pages)
        if (self.status is DocumentUnderstandingStatus.COMPLETE) == unresolved:
            raise ValueError("understanding status must disclose unresolved page scopes")
        return self


class DocumentValidateUnderstandingInputV1(_StrictFrozenModel):
    proposal: DocumentUnderstandingProposalV2


class DocumentUnderstandingValidationReceiptV2(_StrictFrozenModel):
    schema_version: Literal["document-understanding-validation-receipt/v2"] = (
        DOCUMENT_UNDERSTANDING_VALIDATION_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DocumentUnderstandingStatus
    page_count: StrictInt = Field(ge=1)
    unresolved_pages: tuple[StrictInt, ...]
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_id: Literal["reqpilotx-document-understanding-validator"] = (
        "reqpilotx-document-understanding-validator"
    )
    validator_version: Literal["1.1.0"] = "1.1.0"

    @model_validator(mode="after")
    def validate_receipt_sha(self) -> DocumentUnderstandingValidationReceiptV2:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != canonical_json_sha256(payload):
            raise ValueError("validation receipt SHA must bind the receipt payload")
        return self


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def document_page_ledger_sha256(
    *,
    source_sha256: str,
    page_count: int,
    pages: tuple[DocumentPageLedgerEntryV2, ...],
) -> str:
    return canonical_json_sha256(
        {
            "schema_version": DOCUMENT_PAGE_LEDGER_SCHEMA_VERSION,
            "source_sha256": source_sha256,
            "page_count": page_count,
            "pages": [page.model_dump(mode="json") for page in pages],
        }
    )


__all__ = [
    "DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION",
    "DocumentAssetFactV1",
    "DocumentAssetLedgerV1",
    "DocumentEvidenceAuthority",
    "DocumentEvidenceRequirement",
    "DocumentPageFactV1",
    "DocumentPageFactsChunkV1",
    "DocumentPageInputV1",
    "DocumentPageLedgerEntryV2",
    "DocumentPageLedgerV2",
    "DocumentPageTextWindowInputV1",
    "DocumentPageTextChunkV1",
    "DocumentPageTextItemV1",
    "DocumentPageWindowInputV1",
    "DocumentRenderDetail",
    "DocumentRenderPageInputV1",
    "DocumentRenderReceiptV1",
    "DocumentRenderRegionInputV1",
    "DocumentRunManifestV2",
    "DocumentRunManifestInputV2",
    "DocumentSourceHandleInputV1",
    "DocumentTableCellFactV1",
    "DocumentTableMapInputV1",
    "DocumentTableMapChunkV1",
    "DocumentUnderstandingPageV2",
    "DocumentUnderstandingProposalV2",
    "DocumentUnderstandingStatus",
    "DocumentUnderstandingValidationReceiptV2",
    "DocumentValidateUnderstandingInputV1",
    "DocumentVisualObservationKind",
    "DocumentVisualObservationV2",
    "canonical_json_sha256",
    "document_page_ledger_sha256",
]
