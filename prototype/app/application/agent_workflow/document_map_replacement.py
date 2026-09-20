from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.adapters.agents.reference_workspace import verify_reference_workspace
from app.application.agent_workflow.document_map_scope import (
    DocumentMapHeadingNodeV1,
    DocumentMapHeadingScopeCompilationV1,
    DocumentMapHeadingScopeFindingV1,
    DocumentMapScopeRequestV1,
)


DOCUMENT_MAP_SCOPE_REPLACEMENT_REQUEST_SCHEMA_VERSION = (
    "document-map-scope-replacement-request/v1"
)
DOCUMENT_MAP_SCOPE_REPLACEMENT_CANDIDATE_SCHEMA_VERSION = (
    "document-map-scope-replacement-candidate/v1"
)
DOCUMENT_MAP_SCOPE_REPLACEMENT_WIRE_SCHEMA_VERSION = (
    "document-map-scope-replacement-candidate/v2"
)
DOCUMENT_MAP_SCOPE_REPLACEMENT_COMPILATION_SCHEMA_VERSION = (
    "document-map-scope-replacement-compilation/v1"
)
DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE = "DOCUMENT_MAP_SCOPE_REPLACEMENT"

_SOURCE_NODE_ID = r"^node-[0-9a-f]{32}$"
_TABLE_ID = r"^table-[0-9a-f]{32}$"
_ASSET_ID = r"^asset-[0-9a-f]{32}$"
_HEADING_NODE_ID = r"^document-map-node-[0-9a-f]{32}$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class DocumentMapScopeReplacementAction(StrEnum):
    ACCEPT_SCOPE = "ACCEPT_SCOPE"
    COORDINATE_WITH_PREVIOUS = "COORDINATE_WITH_PREVIOUS"
    COORDINATE_WITH_NEXT = "COORDINATE_WITH_NEXT"


class DocumentMapTextOwnershipPolicy(StrEnum):
    NEAREST_PRECEDING_ACCEPTED_HEADING = "NEAREST_PRECEDING_ACCEPTED_HEADING"


class DocumentMapContentDisposition(StrEnum):
    OWNED_BY_HEADING = "OWNED_BY_HEADING"
    NON_BUSINESS = "NON_BUSINESS"
    UNRESOLVED = "UNRESOLVED"


class DocumentMapTableTitleAuthority(StrEnum):
    SOURCE_VISIBLE = "SOURCE_VISIBLE"
    AGENT_LABEL = "AGENT_LABEL"


class DocumentMapAssetRole(StrEnum):
    FIGURE = "FIGURE"
    INLINE_IMAGE = "INLINE_IMAGE"
    EMBEDDED_ATTACHMENT = "EMBEDDED_ATTACHMENT"
    OTHER_ASSET = "OTHER_ASSET"


class DocumentMapScopeReplacementRequestV1(_StrictFrozenModel):
    schema_version: Literal["document-map-scope-replacement-request/v1"] = (
        DOCUMENT_MAP_SCOPE_REPLACEMENT_REQUEST_SCHEMA_VERSION
    )
    private_stage_input_only: Literal[True] = True
    scope_request: DocumentMapScopeRequestV1
    heading_compilation: DocumentMapHeadingScopeCompilationV1
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_lineage(self) -> DocumentMapScopeReplacementRequestV1:
        scope = self.scope_request
        headings = self.heading_compilation
        if (
            headings.source_sha256 != scope.source_sha256
            or headings.survey_compilation_sha256 != scope.survey_compilation_sha256
            or headings.scope_id != scope.scope_id
            or headings.request_sha256 != scope.request_sha256
            or headings.accepted_source_span != scope.suggested_source_span
        ):
            raise ValueError("replacement request heading lineage drifted")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"request_sha256"})
        )
        if self.request_sha256 != expected:
            raise ValueError("replacement request SHA drifted")
        return self

    @property
    def run_id(self) -> str:
        return self.scope_request.run_id

    @property
    def source_sha256(self) -> str:
        return self.scope_request.source_sha256


def build_document_map_scope_replacement_request(
    *,
    scope_request: DocumentMapScopeRequestV1,
    heading_compilation: DocumentMapHeadingScopeCompilationV1,
) -> DocumentMapScopeReplacementRequestV1:
    values = {
        "scope_request": scope_request,
        "heading_compilation": heading_compilation,
    }
    draft = DocumentMapScopeReplacementRequestV1.model_construct(
        **values,
        request_sha256="0" * 64,
    )
    return DocumentMapScopeReplacementRequestV1(
        **values,
        request_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"request_sha256"})
        ),
    )


class DocumentMapTextOwnershipOverrideV1(_StrictFrozenModel):
    source_node_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    disposition: DocumentMapContentDisposition
    display_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    public_summary: str = Field(min_length=1, max_length=600)

    @field_validator("source_node_ids")
    @classmethod
    def validate_source_nodes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(_SOURCE_NODE_ID, item) is None for item in value
        ):
            raise ValueError("text ownership source nodes must be unique")
        return value


class DocumentMapTableFragmentDecisionV1(_StrictFrozenModel):
    physical_table_id: str = Field(pattern=_TABLE_ID)
    start_row: int = Field(ge=0)
    end_row: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> DocumentMapTableFragmentDecisionV1:
        if self.end_row <= self.start_row:
            raise ValueError("table fragment row range is empty")
        return self


class DocumentMapLogicalTableDecisionV1(_StrictFrozenModel):
    # This is an Agent-local identifier, never a path or a public source ID.
    logical_table_key: str = Field(min_length=1, max_length=120)
    owner_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    title: str = Field(min_length=1, max_length=300)
    title_authority: DocumentMapTableTitleAuthority
    fragments: tuple[DocumentMapTableFragmentDecisionV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    public_summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_fragments(self) -> DocumentMapLogicalTableDecisionV1:
        keys = tuple(
            (item.physical_table_id, item.start_row, item.end_row)
            for item in self.fragments
        )
        if len(keys) != len(set(keys)):
            raise ValueError("logical table repeats a physical fragment")
        return self


class DocumentMapTableRemainderDecisionV1(_StrictFrozenModel):
    fragment: DocumentMapTableFragmentDecisionV1
    disposition: Literal[
        DocumentMapContentDisposition.NON_BUSINESS,
        DocumentMapContentDisposition.UNRESOLVED,
    ]
    display_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    public_summary: str = Field(min_length=1, max_length=600)


class DocumentMapAssetOwnershipDecisionV1(_StrictFrozenModel):
    placement_source_node_id: str = Field(pattern=_SOURCE_NODE_ID)
    display_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    disposition: DocumentMapContentDisposition
    role: DocumentMapAssetRole
    public_summary: str = Field(min_length=1, max_length=600)


class _DocumentMapScopeReplacementDecisions(_StrictFrozenModel):
    action: DocumentMapScopeReplacementAction
    text_ownership_policy: DocumentMapTextOwnershipPolicy | None = None
    text_ownership_overrides: tuple[DocumentMapTextOwnershipOverrideV1, ...] = Field(
        default=(), max_length=4096
    )
    logical_tables: tuple[DocumentMapLogicalTableDecisionV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    table_remainders: tuple[DocumentMapTableRemainderDecisionV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    asset_ownership: tuple[DocumentMapAssetOwnershipDecisionV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    findings: tuple[DocumentMapHeadingScopeFindingV1, ...] = Field(
        default=(),
        max_length=256,
    )
    public_summary: str = Field(min_length=1, max_length=1500)

    @model_validator(mode="after")
    def validate_action_shape(self) -> _DocumentMapScopeReplacementDecisions:
        decisions_present = bool(
            self.text_ownership_overrides
            or self.logical_tables
            or self.table_remainders
            or self.asset_ownership
        )
        if self.action is DocumentMapScopeReplacementAction.ACCEPT_SCOPE:
            if self.text_ownership_policy is None:
                raise ValueError("accepted replacement requires text ownership policy")
        elif self.text_ownership_policy is not None or decisions_present:
            raise ValueError("coordination replacement cannot publish ownership")
        table_keys = tuple(item.logical_table_key for item in self.logical_tables)
        if len(table_keys) != len(set(table_keys)):
            raise ValueError("logical table keys must be unique")
        override_ids = tuple(
            source_id
            for item in self.text_ownership_overrides
            for source_id in item.source_node_ids
        )
        if len(override_ids) != len(set(override_ids)):
            raise ValueError("text ownership overrides overlap")
        placement_ids = tuple(
            item.placement_source_node_id for item in self.asset_ownership
        )
        if len(placement_ids) != len(set(placement_ids)):
            raise ValueError("asset placement decisions must be unique")
        return self


class DocumentMapScopeReplacementCandidateV2(_DocumentMapScopeReplacementDecisions):
    """Agent wire decisions; invocation identity is owned by the Host."""

    schema_version: Literal["document-map-scope-replacement-candidate/v2"] = (
        DOCUMENT_MAP_SCOPE_REPLACEMENT_WIRE_SCHEMA_VERSION
    )


class DocumentMapScopeReplacementCandidateV1(_DocumentMapScopeReplacementDecisions):
    """Private source-bound candidate after the Host binds the exact invocation."""

    schema_version: Literal["document-map-scope-replacement-candidate/v1"] = (
        DOCUMENT_MAP_SCOPE_REPLACEMENT_CANDIDATE_SCHEMA_VERSION
    )
    request_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentMapCompiledTextOwnershipV1(_StrictFrozenModel):
    source_node_id: str = Field(pattern=_SOURCE_NODE_ID)
    source_order: int = Field(ge=0)
    display_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    disposition: DocumentMapContentDisposition
    table_evidence_scope_id: str | None = Field(
        default=None,
        pattern=r"^document-map-table-[0-9a-f]{32}$",
    )


class DocumentMapCompiledTableFragmentV1(_StrictFrozenModel):
    physical_table_id: str = Field(pattern=_TABLE_ID)
    table_token: str = Field(min_length=1, max_length=80)
    page_number: int = Field(ge=1, le=250)
    start_row: int = Field(ge=0)
    end_row: int = Field(gt=0)
    row_count: int = Field(gt=0)
    column_count: int = Field(gt=0)
    table_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentMapCompiledLogicalTableV1(_StrictFrozenModel):
    logical_table_scope_id: str = Field(pattern=r"^document-map-table-[0-9a-f]{32}$")
    logical_table_key: str = Field(min_length=1, max_length=120)
    owner_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    title: str = Field(min_length=1, max_length=300)
    title_authority: DocumentMapTableTitleAuthority
    fragments: tuple[DocumentMapCompiledTableFragmentV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    public_summary: str = Field(min_length=1, max_length=1000)


class DocumentMapCompiledTableRemainderV1(_StrictFrozenModel):
    fragment: DocumentMapCompiledTableFragmentV1
    disposition: Literal[
        DocumentMapContentDisposition.NON_BUSINESS,
        DocumentMapContentDisposition.UNRESOLVED,
    ]
    display_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    public_summary: str = Field(min_length=1, max_length=600)


class DocumentMapCompiledAssetOwnershipV1(_StrictFrozenModel):
    placement_source_node_id: str = Field(pattern=_SOURCE_NODE_ID)
    source_order: int = Field(ge=0)
    asset_id: str = Field(pattern=_ASSET_ID)
    display_heading_node_id: str = Field(pattern=_HEADING_NODE_ID)
    disposition: DocumentMapContentDisposition
    role: DocumentMapAssetRole
    public_summary: str = Field(min_length=1, max_length=600)


class DocumentMapScopeReplacementCompilationV1(_StrictFrozenModel):
    schema_version: Literal["document-map-scope-replacement-compilation/v1"] = (
        DOCUMENT_MAP_SCOPE_REPLACEMENT_COMPILATION_SCHEMA_VERSION
    )
    private_checkpoint_only: Literal[True] = True
    document_map_complete: Literal[False] = False
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    survey_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^document-map-detail-scope-[0-9a-f]{32}$")
    replacement_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_nodes: tuple[DocumentMapHeadingNodeV1, ...] = Field(min_length=1)
    text_ownership: tuple[DocumentMapCompiledTextOwnershipV1, ...]
    logical_tables: tuple[DocumentMapCompiledLogicalTableV1, ...]
    table_remainders: tuple[DocumentMapCompiledTableRemainderV1, ...]
    asset_ownership: tuple[DocumentMapCompiledAssetOwnershipV1, ...]
    findings: tuple[DocumentMapHeadingScopeFindingV1, ...]
    opened_high_resolution_page_numbers: tuple[int, ...] = Field(min_length=1)
    visual_receipt_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["COMPLETE", "PARTIAL"]
    public_summary: str = Field(min_length=1, max_length=1500)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_compilation(self) -> DocumentMapScopeReplacementCompilationV1:
        if self.opened_high_resolution_page_numbers != tuple(
            sorted(set(self.opened_high_resolution_page_numbers))
        ):
            raise ValueError("replacement visual pages must be unique and sorted")
        has_unresolved = any(
            item.disposition is DocumentMapContentDisposition.UNRESOLVED
            for item in (
                *self.text_ownership,
                *self.table_remainders,
                *self.asset_ownership,
            )
        )
        if (self.status == "PARTIAL") != has_unresolved:
            raise ValueError("replacement status drifted from unresolved evidence")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"compilation_sha256"})
        )
        if self.compilation_sha256 != expected:
            raise ValueError("replacement compilation SHA drifted")
        return self


@dataclass(frozen=True, slots=True)
class DocumentMapScopeEvidenceRecord:
    source_node_id: str
    source_order: int
    source_kind: str
    page_number: int
    bbox: tuple[float, float, float, float]
    page_bounds: tuple[float, float, float, float]
    raw_text: str
    source_text_sha256: str
    table_id: str | None
    asset_id: str | None


@dataclass(frozen=True, slots=True)
class DocumentMapScopeEvidenceIndex:
    root: Path
    source_sha256: str
    source_ir_canonical_sha256: str
    scope_id: str
    records: tuple[DocumentMapScopeEvidenceRecord, ...]
    tables_by_id: Mapping[str, Mapping[str, Any]]
    table_index_by_id: Mapping[str, Mapping[str, Any]]
    assets_by_id: Mapping[str, Mapping[str, Any]]


def load_document_map_scope_evidence(
    *,
    root: Path,
    request: DocumentMapScopeReplacementRequestV1 | DocumentMapScopeRequestV1,
) -> DocumentMapScopeEvidenceIndex:
    scope = (
        request.scope_request
        if isinstance(request, DocumentMapScopeReplacementRequestV1)
        else request
    )
    verify_reference_workspace(
        root,
        expected_source_sha256=scope.source_sha256,
        expected_bundle_sha256=scope.evidence_workspace_bundle_sha256,
    )
    workspace = root.resolve(strict=True)
    manifest = _json(workspace / "manifest.json")
    capture = manifest.get("capture")
    if (
        not isinstance(capture, Mapping)
        or capture.get("source_ir_id") != scope.source_ir_id
        or capture.get("source_ir_canonical_sha256") != scope.source_ir_canonical_sha256
    ):
        raise ValueError("replacement Evidence Workspace lineage drifted")
    page_index = _jsonl(workspace / "pages/index.jsonl")
    bounds_by_page = {
        int(item["page_number"]): _bbox(item["bounds"]) for item in page_index
    }
    source_map = {
        str(item["source_handle"]): item
        for item in _jsonl(workspace / "pages/source-map.jsonl")
    }
    text_map = {
        str(item["source_handle"]): item
        for item in _jsonl(workspace / "pages/text-index.jsonl")
    }
    ordinals = _jsonl(workspace / "pages/source-ordinal-ledger.jsonl")
    records: list[DocumentMapScopeEvidenceRecord] = []
    expected_ids = set(scope.source_node_ids)
    for item in ordinals:
        source_node_id = str(item.get("source_node_id") or "")
        if source_node_id not in expected_ids:
            continue
        fact = source_map.get(source_node_id)
        if fact is None:
            raise ValueError("replacement source fact is missing")
        page_number = int(item.get("page_number") or 0)
        text = text_map.get(source_node_id)
        raw_text = "" if text is None else str(text.get("text") or "")
        text_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        if (
            page_number not in bounds_by_page
            or fact.get("source_order") != item.get("source_order")
            or fact.get("kind") != item.get("source_kind")
            or fact.get("table_id") != item.get("table_id")
            or fact.get("asset_id") != item.get("asset_ref")
            or (
                text is not None
                and (
                    text.get("text_sha256") != text_sha256
                    or item.get("source_text_sha256") != text_sha256
                )
            )
        ):
            raise ValueError("replacement source evidence drifted")
        records.append(
            DocumentMapScopeEvidenceRecord(
                source_node_id=source_node_id,
                source_order=int(item["source_order"]),
                source_kind=str(item["source_kind"]),
                page_number=page_number,
                bbox=_bbox(fact["bbox"]),
                page_bounds=bounds_by_page[page_number],
                raw_text=raw_text,
                source_text_sha256=text_sha256,
                table_id=(
                    str(item["table_id"]) if item.get("table_id") is not None else None
                ),
                asset_id=(
                    str(item["asset_ref"])
                    if item.get("asset_ref") is not None
                    else None
                ),
            )
        )
    records.sort(key=lambda item: item.source_order)
    if tuple(item.source_node_id for item in records) != tuple(scope.source_node_ids):
        raise ValueError("replacement scope source coverage drifted")

    table_index_payload = _json(workspace / "tables/index.json")
    table_index = {
        str(item["table_id"]): item for item in table_index_payload.get("tables", [])
    }
    tables: dict[str, Mapping[str, Any]] = {}
    for table_id in scope.physical_table_ids:
        index = table_index.get(table_id)
        if index is None:
            raise ValueError("replacement physical table is missing")
        payload = _json(workspace / str(index["json_path"]))
        if payload.get("table_id") != table_id or _canonical_sha256(
            payload
        ) != index.get("table_sha256"):
            raise ValueError("replacement physical table digest drifted")
        tables[table_id] = payload
    asset_index_payload = _json(workspace / "assets/index.json")
    assets = {
        str(item["asset_id"]): item
        for item in asset_index_payload.get("unique_assets", [])
    }
    if not set(scope.asset_ids).issubset(assets):
        raise ValueError("replacement asset evidence is missing")
    return DocumentMapScopeEvidenceIndex(
        root=workspace,
        source_sha256=scope.source_sha256,
        source_ir_canonical_sha256=scope.source_ir_canonical_sha256,
        scope_id=scope.scope_id,
        records=tuple(records),
        tables_by_id=tables,
        table_index_by_id=table_index,
        assets_by_id=assets,
    )


def compile_document_map_scope_replacement(
    *,
    request: DocumentMapScopeReplacementRequestV1,
    candidate: DocumentMapScopeReplacementCandidateV1,
    evidence: DocumentMapScopeEvidenceIndex,
    opened_high_resolution_page_numbers: tuple[int, ...],
    visual_receipt_root_sha256: str,
) -> DocumentMapScopeReplacementCompilationV1:
    request = DocumentMapScopeReplacementRequestV1.model_validate(
        request.model_dump(mode="json")
    )
    candidate = DocumentMapScopeReplacementCandidateV1.model_validate(
        candidate.model_dump(mode="json")
    )
    scope = request.scope_request
    headings = request.heading_compilation
    if (
        candidate.action is not DocumentMapScopeReplacementAction.ACCEPT_SCOPE
        or candidate.request_id != scope.scope_id
        or candidate.request_sha256 != request.request_sha256
        or candidate.heading_compilation_sha256 != headings.compilation_sha256
        or evidence.root.is_symlink()
        or tuple(sorted(set(opened_high_resolution_page_numbers)))
        != opened_high_resolution_page_numbers
        or not set(scope.page_numbers).issubset(opened_high_resolution_page_numbers)
        or re.fullmatch(r"[0-9a-f]{64}", visual_receipt_root_sha256) is None
    ):
        raise ValueError("replacement candidate or visual lineage drifted")

    heading_by_id = {item.stable_node_id: item for item in headings.heading_nodes}
    heading_by_source = {item.source_node_id: item for item in headings.heading_nodes}
    if len(heading_by_id) != len(headings.heading_nodes):
        raise ValueError("replacement heading identities are not unique")
    ordinary_records = tuple(
        item
        for item in evidence.records
        if item.source_node_id not in heading_by_source
        and item.table_id is None
        and item.asset_id is None
    )
    default_owners = _default_text_owners(
        records=evidence.records,
        heading_by_source=heading_by_source,
    )
    overrides: dict[str, DocumentMapTextOwnershipOverrideV1] = {}
    ordinary_ids = {item.source_node_id for item in ordinary_records}
    for override in candidate.text_ownership_overrides:
        if override.display_heading_node_id not in heading_by_id or not set(
            override.source_node_ids
        ).issubset(ordinary_ids):
            raise ValueError("replacement text ownership override is invalid")
        for source_node_id in override.source_node_ids:
            if source_node_id in overrides:
                raise ValueError("replacement text ownership overrides overlap")
            overrides[source_node_id] = override
    text_ownership = tuple(
        DocumentMapCompiledTextOwnershipV1(
            source_node_id=item.source_node_id,
            source_order=item.source_order,
            display_heading_node_id=(
                overrides[item.source_node_id].display_heading_node_id
                if item.source_node_id in overrides
                else default_owners[item.source_node_id]
            ),
            disposition=(
                overrides[item.source_node_id].disposition
                if item.source_node_id in overrides
                else DocumentMapContentDisposition.OWNED_BY_HEADING
            ),
        )
        for item in ordinary_records
    )

    logical_tables: list[DocumentMapCompiledLogicalTableV1] = []
    table_coverage: dict[str, list[int]] = {
        table_id: [0] * int(table["row_count"])
        for table_id, table in evidence.tables_by_id.items()
    }
    for decision in candidate.logical_tables:
        if decision.owner_heading_node_id not in heading_by_id:
            raise ValueError("logical table owner heading is invalid")
        fragments = tuple(
            _compile_table_fragment(
                item,
                evidence=evidence,
                coverage=table_coverage,
            )
            for item in decision.fragments
        )
        table_orders = {
            item.table_id: item.source_order
            for item in evidence.records
            if item.table_id is not None
        }
        fragment_order = tuple(
            (table_orders[item.physical_table_id], item.start_row) for item in fragments
        )
        if fragment_order != tuple(sorted(fragment_order)):
            raise ValueError("logical table fragments must preserve source order")
        if decision.title_authority is DocumentMapTableTitleAuthority.SOURCE_VISIBLE:
            visible_values = [heading_by_id[decision.owner_heading_node_id].title]
            visible_values.extend(
                str(cell.get("raw_text") or cell.get("display_value") or "")
                for fragment in fragments
                for cell in evidence.tables_by_id[fragment.physical_table_id]["cells"]
            )
            normalized_title = re.sub(r"\s+", "", decision.title)
            if not any(
                normalized_title in re.sub(r"\s+", "", value)
                for value in visible_values
            ):
                raise ValueError("logical table title is not source-visible")
        scope_id = (
            "document-map-table-"
            + _canonical_sha256(
                {
                    "source_sha256": scope.source_sha256,
                    "fragments": [
                        {
                            "physical_table_id": item.physical_table_id,
                            "start_row": item.start_row,
                            "end_row": item.end_row,
                        }
                        for item in fragments
                    ],
                }
            )[:32]
        )
        logical_tables.append(
            DocumentMapCompiledLogicalTableV1(
                logical_table_scope_id=scope_id,
                logical_table_key=decision.logical_table_key,
                owner_heading_node_id=decision.owner_heading_node_id,
                title=decision.title,
                title_authority=decision.title_authority,
                fragments=fragments,
                public_summary=decision.public_summary,
            )
        )
    table_remainders = tuple(
        DocumentMapCompiledTableRemainderV1(
            fragment=_compile_table_fragment(
                item.fragment,
                evidence=evidence,
                coverage=table_coverage,
            ),
            disposition=item.disposition,
            display_heading_node_id=item.display_heading_node_id,
            public_summary=item.public_summary,
        )
        for item in candidate.table_remainders
    )
    if any(
        item.display_heading_node_id not in heading_by_id for item in table_remainders
    ):
        raise ValueError("table remainder owner heading is invalid")
    if any(any(count != 1 for count in rows) for rows in table_coverage.values()):
        raise ValueError("replacement table rows require exact one-owner coverage")
    text_ownership = _bind_table_text_aliases(
        text_ownership,
        ordinary_records=ordinary_records,
        logical_tables=tuple(logical_tables),
        evidence=evidence,
    )

    placement_records = {
        item.source_node_id: item
        for item in evidence.records
        if item.asset_id is not None
    }
    asset_ownership = tuple(
        DocumentMapCompiledAssetOwnershipV1(
            placement_source_node_id=item.placement_source_node_id,
            source_order=placement_records[item.placement_source_node_id].source_order,
            asset_id=placement_records[item.placement_source_node_id].asset_id or "",
            display_heading_node_id=item.display_heading_node_id,
            disposition=item.disposition,
            role=item.role,
            public_summary=item.public_summary,
        )
        for item in candidate.asset_ownership
        if item.placement_source_node_id in placement_records
    )
    if (
        len(asset_ownership) != len(candidate.asset_ownership)
        or {item.placement_source_node_id for item in asset_ownership}
        != set(placement_records)
        or any(
            item.display_heading_node_id not in heading_by_id
            for item in asset_ownership
        )
    ):
        raise ValueError("replacement asset placements require exact coverage")
    _validate_finding_refs(candidate, request=request)
    has_unresolved = any(
        item.disposition is DocumentMapContentDisposition.UNRESOLVED
        for item in (*text_ownership, *table_remainders, *asset_ownership)
    )
    values = {
        "source_sha256": scope.source_sha256,
        "source_ir_id": scope.source_ir_id,
        "source_ir_canonical_sha256": scope.source_ir_canonical_sha256,
        "survey_compilation_sha256": scope.survey_compilation_sha256,
        "scope_id": scope.scope_id,
        "replacement_request_sha256": request.request_sha256,
        "heading_compilation_sha256": headings.compilation_sha256,
        "heading_nodes": headings.heading_nodes,
        "text_ownership": text_ownership,
        "logical_tables": tuple(logical_tables),
        "table_remainders": table_remainders,
        "asset_ownership": asset_ownership,
        "findings": tuple(candidate.findings),
        "opened_high_resolution_page_numbers": opened_high_resolution_page_numbers,
        "visual_receipt_root_sha256": visual_receipt_root_sha256,
        "status": "PARTIAL" if has_unresolved else "COMPLETE",
        "public_summary": candidate.public_summary,
        "candidate_sha256": _canonical_sha256(candidate.model_dump(mode="json")),
    }
    draft = DocumentMapScopeReplacementCompilationV1.model_construct(
        **values,
        compilation_sha256="0" * 64,
    )
    return DocumentMapScopeReplacementCompilationV1(
        **values,
        compilation_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"compilation_sha256"})
        ),
    )


def _default_text_owners(
    *,
    records: tuple[DocumentMapScopeEvidenceRecord, ...],
    heading_by_source: Mapping[str, DocumentMapHeadingNodeV1],
) -> dict[str, str]:
    current: str | None = None
    result: dict[str, str] = {}
    for item in records:
        heading = heading_by_source.get(item.source_node_id)
        if heading is not None:
            current = heading.stable_node_id
            continue
        if item.table_id is not None or item.asset_id is not None:
            continue
        if current is None:
            raise ValueError("replacement text precedes its first accepted heading")
        result[item.source_node_id] = current
    return result


def _compile_table_fragment(
    item: DocumentMapTableFragmentDecisionV1,
    *,
    evidence: DocumentMapScopeEvidenceIndex,
    coverage: dict[str, list[int]],
) -> DocumentMapCompiledTableFragmentV1:
    table = evidence.tables_by_id.get(item.physical_table_id)
    index = evidence.table_index_by_id.get(item.physical_table_id)
    if table is None or index is None:
        raise ValueError("replacement table fragment leaves its scope")
    row_count = int(table["row_count"])
    if item.end_row > row_count:
        raise ValueError("replacement table fragment exceeds row count")
    if any(
        int(cell["row"]) < boundary < int(cell["row"]) + int(cell["row_span"])
        for cell in table["cells"]
        for boundary in (item.start_row, item.end_row)
    ):
        raise ValueError("replacement table fragment cuts a merged cell")
    for row in range(item.start_row, item.end_row):
        coverage[item.physical_table_id][row] += 1
    return DocumentMapCompiledTableFragmentV1(
        physical_table_id=item.physical_table_id,
        table_token=str(table["table_token"]),
        page_number=int(table["page_number"]),
        start_row=item.start_row,
        end_row=item.end_row,
        row_count=row_count,
        column_count=int(table["column_count"]),
        table_sha256=str(index["table_sha256"]),
    )


def _bind_table_text_aliases(
    ownerships: tuple[DocumentMapCompiledTextOwnershipV1, ...],
    *,
    ordinary_records: tuple[DocumentMapScopeEvidenceRecord, ...],
    logical_tables: tuple[DocumentMapCompiledLogicalTableV1, ...],
    evidence: DocumentMapScopeEvidenceIndex,
) -> tuple[DocumentMapCompiledTextOwnershipV1, ...]:
    records = {item.source_node_id: item for item in ordinary_records}
    result: list[DocumentMapCompiledTextOwnershipV1] = []
    for ownership in ownerships:
        record = records[ownership.source_node_id]
        source_text = re.sub(r"\s+", "", record.raw_text)
        matches: list[DocumentMapCompiledLogicalTableV1] = []
        if (
            source_text
            and ownership.disposition is DocumentMapContentDisposition.OWNED_BY_HEADING
        ):
            for logical in logical_tables:
                for fragment in logical.fragments:
                    if fragment.page_number != record.page_number:
                        continue
                    physical = evidence.tables_by_id[fragment.physical_table_id]
                    cells = [
                        cell
                        for cell in physical["cells"]
                        if int(cell["row"]) < fragment.end_row
                        and int(cell["row"]) + int(cell["row_span"])
                        > fragment.start_row
                    ]
                    table_text = "".join(
                        re.sub(
                            r"\s+",
                            "",
                            str(
                                cell.get("raw_text") or cell.get("display_value") or ""
                            ),
                        )
                        for cell in sorted(
                            cells,
                            key=lambda cell: (int(cell["row"]), int(cell["column"])),
                        )
                    )
                    if source_text not in table_text:
                        continue
                    cell_boxes = [
                        _bbox(cell["native_locator"]["bbox"])
                        for cell in cells
                        if isinstance(cell.get("native_locator"), Mapping)
                        and cell["native_locator"].get("bbox") is not None
                    ]
                    if not cell_boxes:
                        continue
                    table_bbox = (
                        min(box[0] for box in cell_boxes),
                        min(box[1] for box in cell_boxes),
                        max(box[2] for box in cell_boxes),
                        max(box[3] for box in cell_boxes),
                    )
                    if _bbox_coverage(record.bbox, table_bbox) >= 0.95:
                        matches.append(logical)
                        break
        unique = {item.logical_table_scope_id: item for item in matches}
        if len(unique) == 1:
            logical = next(iter(unique.values()))
            result.append(
                ownership.model_copy(
                    update={
                        "display_heading_node_id": logical.owner_heading_node_id,
                        "table_evidence_scope_id": logical.logical_table_scope_id,
                    }
                )
            )
        else:
            result.append(ownership)
    return tuple(result)


def _bbox_coverage(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> float:
    area = max(0.0, inner[2] - inner[0]) * max(0.0, inner[3] - inner[1])
    if area <= 0.0:
        return 0.0
    overlap = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0])) * max(
        0.0,
        min(inner[3], outer[3]) - max(inner[1], outer[1]),
    )
    return overlap / area


def _validate_finding_refs(
    candidate: DocumentMapScopeReplacementCandidateV1,
    *,
    request: DocumentMapScopeReplacementRequestV1,
) -> None:
    scope = request.scope_request
    allowed = {
        *(f"page:{item}" for item in scope.page_numbers),
        *scope.source_node_ids,
        *scope.physical_table_ids,
        *scope.asset_ids,
    }
    if any(
        any(ref not in allowed for ref in finding.evidence_refs)
        for finding in candidate.findings
    ):
        raise ValueError("replacement finding leaves its scope evidence")


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("replacement evidence JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("replacement evidence JSON is invalid")
    return value


def _jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        values = tuple(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("replacement evidence JSONL is invalid") from exc
    if any(not isinstance(item, dict) for item in values):
        raise ValueError("replacement evidence JSONL is invalid")
    return values


def _bbox(value: object) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in value
        )
    ):
        raise ValueError("replacement evidence bbox is invalid")
    result = tuple(float(item) for item in value)
    if (
        not all(math.isfinite(item) for item in result)
        or result[2] <= result[0]
        or result[3] <= result[1]
    ):
        raise ValueError("replacement evidence bbox is invalid")
    return result  # type: ignore[return-value]


def _canonical_sha256(value: object) -> str:
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
    "DOCUMENT_MAP_SCOPE_REPLACEMENT_CANDIDATE_SCHEMA_VERSION",
    "DOCUMENT_MAP_SCOPE_REPLACEMENT_COMPILATION_SCHEMA_VERSION",
    "DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE",
    "DOCUMENT_MAP_SCOPE_REPLACEMENT_REQUEST_SCHEMA_VERSION",
    "DocumentMapAssetOwnershipDecisionV1",
    "DocumentMapAssetRole",
    "DocumentMapCompiledAssetOwnershipV1",
    "DocumentMapCompiledLogicalTableV1",
    "DocumentMapCompiledTableFragmentV1",
    "DocumentMapCompiledTableRemainderV1",
    "DocumentMapCompiledTextOwnershipV1",
    "DocumentMapContentDisposition",
    "DocumentMapLogicalTableDecisionV1",
    "DocumentMapScopeEvidenceIndex",
    "DocumentMapScopeEvidenceRecord",
    "DocumentMapScopeReplacementAction",
    "DocumentMapScopeReplacementCandidateV1",
    "DocumentMapScopeReplacementCompilationV1",
    "DocumentMapScopeReplacementRequestV1",
    "DocumentMapTableFragmentDecisionV1",
    "DocumentMapTableRemainderDecisionV1",
    "DocumentMapTableTitleAuthority",
    "DocumentMapTextOwnershipOverrideV1",
    "DocumentMapTextOwnershipPolicy",
    "build_document_map_scope_replacement_request",
    "compile_document_map_scope_replacement",
    "load_document_map_scope_evidence",
]
