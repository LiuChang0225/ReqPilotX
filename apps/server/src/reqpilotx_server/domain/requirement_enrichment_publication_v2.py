from __future__ import annotations

import hashlib
import json
import re
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx_server.domain.extraction.llm_requirement_review import LlmReviewSourceSpan
from reqpilotx_server.domain.extraction.requirement_view import RequirementDecisionMode


REQUIREMENT_ENRICHMENT_PUBLICATION_PROJECTION_V2_SCHEMA_VERSION = (
    "requirement-enrichment-publication-projection/v2"
)
REQUIREMENT_ENRICHMENT_PUBLICATION_BINDING_PREVIEW_V2_SCHEMA_VERSION = (
    "requirement-enrichment-publication-binding-preview/v2"
)
REQUIREMENT_ENRICHMENT_PUBLICATION_ROW_BINDING_V2_SCHEMA_VERSION = (
    "requirement-enrichment-publication-row-binding/v2"
)
REQUIREMENT_ENRICHMENT_PUBLICATION_START_V2_SCHEMA_VERSION = (
    "requirement-enrichment-publication-start/v2"
)
REQUIREMENT_ENRICHMENT_PUBLICATION_BINDING_V2_SCHEMA_VERSION = (
    "requirement-enrichment-publication-binding/v2"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ATOMIZATION_RUN_ID_RE = re.compile(r"^atomization-run-[0-9a-f]{32}$")
_ATOMIZATION_NODE_ID_RE = re.compile(r"^atomization-node-[0-9a-f]{32}$")
_ATOMIC_REQUIREMENT_ID_RE = re.compile(r"^atomic-obligation-[0-9a-f]{32}$")
_ATOMIZATION_RECEIPT_ID_RE = re.compile(r"^atomization-scope-receipt-[0-9a-f]{32}$")
_PUBLICATION_PREVIEW_ID_RE = re.compile(
    r"^requirement-publication-preview-[0-9a-f]{32}$"
)
_BINDING_PREVIEW_ID_RE = re.compile(
    r"^requirement-enrichment-binding-preview-[0-9a-f]{32}$"
)
_BINDING_ID_RE = re.compile(r"^requirement-enrichment-binding-[0-9a-f]{32}$")
_ENTRY_CLASSIFICATIONS = frozenset({"TECHNICAL", "NON_TECHNICAL", "INFORMATION"})
_BUILDER_BINDING_ROLES = frozenset(
    {
        "STRUCTURE_PROXY",
        "DIRECT_CONTENT",
        "WHOLE_TABLE_REVIEW",
        "TABLE_RECORD",
        "REQUIREMENT",
    }
)


class RequirementEnrichmentAtomizationStateV2(str, Enum):
    NOT_RUN = "NOT_RUN"
    APPLIED = "APPLIED"


class RequirementEnrichmentPublicationEntryKindV2(str, Enum):
    HEADING = "HEADING"
    REQUIREMENT = "REQUIREMENT"
    REFERENCE = "REFERENCE"


class RequirementEnrichmentAnalysisDispositionV2(str, Enum):
    ANALYZE = "ANALYZE"
    RETAIN = "RETAIN"


class RequirementEnrichmentPublicationBuilderKindV2(str, Enum):
    REQUIREMENT = "REQUIREMENT"
    REQUIREMENT_CONTAINER = "REQUIREMENT_CONTAINER"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RequirementEnrichmentPublicationRowV2(_FrozenModel):
    """One logical database/tree/evidence row in the W4 publication plan.

    ``requirement_tree_node_id`` is the final immutable tree identity.  A
    multi-atom family keeps the accepted W3 identity for its evidence parent;
    each additional child uses the immutable atomization node identity.
    """

    requirement_tree_node_id: str = Field(min_length=1, max_length=260)
    source_working_tree_node_id: str = Field(min_length=1, max_length=260)
    parent_requirement_tree_node_id: str | None = Field(
        default=None,
        max_length=260,
    )
    workflow_node_id: str = Field(min_length=1, max_length=260)
    atomization_node_id: str | None = Field(default=None, max_length=80)
    atomic_requirement_id: str | None = Field(default=None, max_length=80)
    entry_kind: RequirementEnrichmentPublicationEntryKindV2
    builder_kind: RequirementEnrichmentPublicationBuilderKindV2
    decision_mode: RequirementDecisionMode
    classification: str = Field(min_length=1, max_length=120)
    builder_binding_role: str = Field(min_length=1, max_length=80)
    final_projection_role: str = Field(min_length=1, max_length=80)
    intrinsic_analysis_eligible: bool
    default_disposition: RequirementEnrichmentAnalysisDispositionV2
    source_order: int = Field(ge=0)
    source_offset: int = Field(default=0, ge=0)
    label: str = Field(default="", max_length=500_000)
    original_statement: str = Field(default="", max_length=500_000)
    published_statement: str = Field(min_length=1, max_length=500_000)
    structure_node_id: str | None = Field(default=None, max_length=260)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    source_spans: tuple[LlmReviewSourceSpan, ...] = ()
    source_refs: tuple[dict[str, Any], ...] = ()
    status: str = Field(default="PENDING", max_length=80)
    table_record_id: str | None = Field(default=None, max_length=260)
    table_evidence_binding_id: str | None = Field(default=None, max_length=260)
    deterministic_rule_evidence: tuple[dict[str, Any], ...] = ()
    include_in_view: bool = True
    content_blocks: tuple[dict[str, Any], ...] = ()
    editable_structure_node_ids: tuple[str, ...] = ()
    table_refs: tuple[dict[str, Any], ...] = ()
    figure_refs: tuple[dict[str, Any], ...] = ()

    @field_validator("atomization_node_id")
    @classmethod
    def validate_atomization_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _ATOMIZATION_NODE_ID_RE.fullmatch(value):
            raise ValueError("publication row atomization node identity is invalid")
        return value

    @field_validator("atomic_requirement_id")
    @classmethod
    def validate_atomic_requirement_id(cls, value: str | None) -> str | None:
        if value is not None and not _ATOMIC_REQUIREMENT_ID_RE.fullmatch(value):
            raise ValueError("publication row atomic requirement identity is invalid")
        return value

    @field_validator("classification")
    @classmethod
    def validate_classification(cls, value: str) -> str:
        if value not in _ENTRY_CLASSIFICATIONS:
            raise ValueError("publication rows require accepted three-class truth")
        return value

    @field_validator("builder_binding_role", "final_projection_role")
    @classmethod
    def validate_projection_role(cls, value: str) -> str:
        if value not in _BUILDER_BINDING_ROLES:
            raise ValueError("publication row projection role is unsupported")
        return value

    @model_validator(mode="after")
    def validate_row_shape(self) -> RequirementEnrichmentPublicationRowV2:
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("publication row page_end cannot precede page_start")
        if len(self.editable_structure_node_ids) != len(
            set(self.editable_structure_node_ids)
        ):
            raise ValueError("publication editable structure identities must be unique")
        content_structure_ids = {
            str(block.get("structure_node_id") or "")
            for block in self.content_blocks
            if str(block.get("structure_node_id") or "")
        }
        if not set(self.editable_structure_node_ids).issubset(content_structure_ids):
            raise ValueError(
                "publication editable structure identities must come from content"
            )
        is_anchor = self.requirement_tree_node_id == self.source_working_tree_node_id
        if self.atomic_requirement_id is not None and self.atomization_node_id is None:
            raise ValueError(
                "atomic requirement identity requires atomization evidence"
            )
        if self.atomization_node_id is not None and not self.source_spans:
            raise ValueError("atomization output requires exact source spans")
        if not is_anchor:
            if self.parent_requirement_tree_node_id != self.source_working_tree_node_id:
                raise ValueError("atom child parent must be its accepted W3 anchor")
            if self.requirement_tree_node_id != self.atomization_node_id:
                raise ValueError(
                    "atom child tree identity must use atomization_node_id"
                )
            if self.atomic_requirement_id is None:
                raise ValueError("atom child requires an atomic requirement identity")
            if (
                self.entry_kind
                is not RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
            ):
                raise ValueError("atom child must be a requirement row")
            if (
                self.builder_kind
                is not RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT
            ):
                raise ValueError("atom child must use the requirement builder shape")
            if self.decision_mode is not RequirementDecisionMode.DIRECT:
                raise ValueError("atom child must be a direct decision")
            if not self.source_spans:
                raise ValueError("atom child requires exact source spans")
        elif self.parent_requirement_tree_node_id is not None:
            raise ValueError("accepted W3 anchor cannot have a logical row parent")
        if self.entry_kind is RequirementEnrichmentPublicationEntryKindV2.HEADING:
            if (
                self.builder_kind
                is not RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT_CONTAINER
                or self.decision_mode is not RequirementDecisionMode.ROLLUP
                or self.atomic_requirement_id is not None
                or self.atomization_node_id is None
                or self.classification != "TECHNICAL"
                or self.final_projection_role != "REQUIREMENT"
                or self.intrinsic_analysis_eligible
                or self.default_disposition
                is not RequirementEnrichmentAnalysisDispositionV2.RETAIN
            ):
                raise ValueError(
                    "evidence parent must use HEADING/REQUIREMENT_CONTAINER/ROLLUP"
                )
        elif (
            self.builder_kind
            is RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT_CONTAINER
        ):
            raise ValueError("only evidence parents use the container builder shape")
        elif (
            self.entry_kind is RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
            and self.decision_mode is not RequirementDecisionMode.DIRECT
        ):
            raise ValueError("requirement publication rows must be direct decisions")
        if self.entry_kind is RequirementEnrichmentPublicationEntryKindV2.REFERENCE:
            if (
                self.decision_mode is not RequirementDecisionMode.NONE
                or self.intrinsic_analysis_eligible
                or self.default_disposition
                is not RequirementEnrichmentAnalysisDispositionV2.RETAIN
            ):
                raise ValueError("reference context cannot be an independent decision")
        if self.classification == "INFORMATION" and (
            self.entry_kind is not RequirementEnrichmentPublicationEntryKindV2.REFERENCE
        ):
            raise ValueError("information rows must remain reference context")
        if (
            self.classification == "TECHNICAL"
            and self.entry_kind
            is not RequirementEnrichmentPublicationEntryKindV2.HEADING
        ):
            if (
                self.entry_kind
                is not RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
                or not self.intrinsic_analysis_eligible
                or self.decision_mode is not RequirementDecisionMode.DIRECT
                or self.final_projection_role != "REQUIREMENT"
                or self.default_disposition
                is not RequirementEnrichmentAnalysisDispositionV2.ANALYZE
            ):
                raise ValueError(
                    "technical business rows must publish as analyzable requirements"
                )
        if self.classification == "NON_TECHNICAL":
            if (
                self.entry_kind
                is not RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
                or not self.intrinsic_analysis_eligible
                or self.decision_mode is not RequirementDecisionMode.DIRECT
                or self.final_projection_role != "DIRECT_CONTENT"
                or self.default_disposition
                is not RequirementEnrichmentAnalysisDispositionV2.RETAIN
            ):
                raise ValueError(
                    "non-technical business rows must remain eligible requirements"
                )
        if (
            self.default_disposition
            is RequirementEnrichmentAnalysisDispositionV2.ANALYZE
        ):
            if (
                self.classification != "TECHNICAL"
                or not self.intrinsic_analysis_eligible
                or self.entry_kind
                is not RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
                or self.final_projection_role != "REQUIREMENT"
                or self.decision_mode is not RequirementDecisionMode.DIRECT
            ):
                raise ValueError("default analysis requires a direct technical row")
        return self


class RequirementEnrichmentPublicationProjectionV2(_FrozenModel):
    schema_version: Literal["requirement-enrichment-publication-projection/v2"] = (
        REQUIREMENT_ENRICHMENT_PUBLICATION_PROJECTION_V2_SCHEMA_VERSION
    )
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    effective_structure_id: str = Field(min_length=1, max_length=200)
    accepted_working_tree_id: str = Field(min_length=1, max_length=200)
    accepted_working_tree_sha256: str
    entry_classification_snapshot_id: str = Field(min_length=1, max_length=200)
    entry_classification_revision: int = Field(ge=0)
    entry_classification_sha256: str
    entry_classification_content_sha256: str
    atomization_state: RequirementEnrichmentAtomizationStateV2
    atomization_scope_receipt_id: str = Field(default="", max_length=80)
    atomization_scope_receipt_sha256: str = Field(default="", max_length=64)
    atomization_scope_revision: int = Field(default=0, ge=0)
    atomization_active_revision: int = Field(default=0, ge=0)
    atomization_classification_content_sha256: str = Field(
        default="",
        max_length=64,
    )
    atomization_run_id: str = Field(default="", max_length=80)
    atomization_revision: int = Field(default=0, ge=0)
    atomization_run_sha256: str = Field(default="", max_length=64)
    publication_preview_id: str = Field(default="", max_length=80)
    publication_preview_sha256: str = Field(default="", max_length=64)
    publication_key: str = Field(default="", max_length=64)
    publication_lineage_sha256: str = Field(default="", max_length=64)
    applied_review_node_ids: tuple[str, ...] = ()
    source_business_tree_node_ids: tuple[str, ...]
    rows: tuple[RequirementEnrichmentPublicationRowV2, ...]
    projection_sha256: str

    @field_validator(
        "source_sha256",
        "accepted_working_tree_sha256",
        "entry_classification_sha256",
        "entry_classification_content_sha256",
        "projection_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("publication projection hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_projection(self) -> RequirementEnrichmentPublicationProjectionV2:
        if not self.source_business_tree_node_ids or not self.rows:
            raise ValueError(
                "publication projection requires the full business skeleton"
            )
        if len(self.source_business_tree_node_ids) != len(
            set(self.source_business_tree_node_ids)
        ):
            raise ValueError("business skeleton node identities must be unique")
        if len(self.applied_review_node_ids) != len(set(self.applied_review_node_ids)):
            raise ValueError("applied atomization targets must be unique")
        row_ids = tuple(row.requirement_tree_node_id for row in self.rows)
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("publication logical row identities must be unique")
        row_by_id = {row.requirement_tree_node_id: row for row in self.rows}
        if any(
            row.parent_requirement_tree_node_id is not None
            and row.parent_requirement_tree_node_id not in row_by_id
            for row in self.rows
        ):
            raise ValueError("publication logical row parent is missing")
        _reject_parent_cycles(row_by_id)
        anchor_ids = tuple(
            row.requirement_tree_node_id
            for row in self.rows
            if row.requirement_tree_node_id == row.source_working_tree_node_id
        )
        if anchor_ids != self.source_business_tree_node_ids:
            raise ValueError(
                "publication rows must exactly cover the accepted W3 business skeleton"
            )
        business_ids = set(self.source_business_tree_node_ids)
        if any(
            row.source_working_tree_node_id not in business_ids for row in self.rows
        ):
            raise ValueError("publication row references an extra W3 business node")
        children_by_anchor: dict[str, list[str]] = {
            anchor_id: [] for anchor_id in self.source_business_tree_node_ids
        }
        for row in self.rows:
            if row.requirement_tree_node_id != row.source_working_tree_node_id:
                children_by_anchor[row.source_working_tree_node_id].append(
                    row.requirement_tree_node_id
                )
        expected_order: list[str] = []
        for anchor_id in self.source_business_tree_node_ids:
            expected_order.append(anchor_id)
            expected_order.extend(children_by_anchor[anchor_id])
        if tuple(expected_order) != row_ids:
            raise ValueError("publication rows must retain canonical W3 family order")
        if self.atomization_state is RequirementEnrichmentAtomizationStateV2.NOT_RUN:
            if any(
                (
                    self.atomization_scope_receipt_id,
                    self.atomization_scope_receipt_sha256,
                    self.atomization_scope_revision,
                    self.atomization_active_revision,
                    self.atomization_classification_content_sha256,
                    self.atomization_run_id,
                    self.atomization_revision,
                    self.atomization_run_sha256,
                    self.publication_preview_id,
                    self.publication_preview_sha256,
                    self.publication_key,
                    self.publication_lineage_sha256,
                    self.applied_review_node_ids,
                )
            ) or any(row.atomization_node_id is not None for row in self.rows):
                raise ValueError("NOT_RUN publication cannot carry atomization output")
        else:
            if not _ATOMIZATION_RECEIPT_ID_RE.fullmatch(
                self.atomization_scope_receipt_id
            ):
                raise ValueError("APPLIED publication requires a scope receipt ID")
            if not _ATOMIZATION_RUN_ID_RE.fullmatch(self.atomization_run_id):
                raise ValueError("APPLIED publication requires an atomization run")
            if not _PUBLICATION_PREVIEW_ID_RE.fullmatch(self.publication_preview_id):
                raise ValueError("APPLIED publication requires a preview ID")
            if self.atomization_revision < 1:
                raise ValueError("APPLIED publication requires a positive revision")
            if (
                not self.atomization_scope_receipt_id
                or not _SHA256_RE.fullmatch(self.atomization_scope_receipt_sha256)
                or self.atomization_scope_revision != self.atomization_revision
                or self.atomization_active_revision < 1
                or not _SHA256_RE.fullmatch(
                    self.atomization_classification_content_sha256
                )
                or not _SHA256_RE.fullmatch(self.atomization_run_sha256)
                or not self.publication_preview_id
                or not _SHA256_RE.fullmatch(self.publication_preview_sha256)
                or not _SHA256_RE.fullmatch(self.publication_key)
                or not _SHA256_RE.fullmatch(self.publication_lineage_sha256)
            ):
                raise ValueError(
                    "APPLIED publication requires exact receipt and preview identities"
                )
        if self.projection_sha256 != canonical_publication_projection_v2_sha256(self):
            raise ValueError("publication projection hash does not match its contents")
        return self


def canonical_publication_projection_v2_sha256(
    projection: RequirementEnrichmentPublicationProjectionV2,
) -> str:
    payload = projection.model_dump(mode="json", exclude={"projection_sha256"})
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class RequirementEnrichmentPublicationBindingPreviewV2(_FrozenModel):
    """Persisted command receipt for one exact W4 projection.

    This sidecar deliberately owns the large projection payload.  The eventual
    Requirement Set receives only the paired final binding identity and hash.
    """

    schema_version: Literal[
        "requirement-enrichment-publication-binding-preview/v2"
    ] = REQUIREMENT_ENRICHMENT_PUBLICATION_BINDING_PREVIEW_V2_SCHEMA_VERSION
    binding_preview_id: str = Field(min_length=1, max_length=96)
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    review_revision: int = Field(ge=0)
    base_active_requirement_set_id: str = Field(default="", max_length=160)
    projection: RequirementEnrichmentPublicationProjectionV2
    payload_sha256: str

    @field_validator("binding_preview_id")
    @classmethod
    def validate_binding_preview_id(cls, value: str) -> str:
        if not _BINDING_PREVIEW_ID_RE.fullmatch(value):
            raise ValueError("publication binding preview identity is invalid")
        return value

    @field_validator("payload_sha256")
    @classmethod
    def validate_payload_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("publication binding preview hash is invalid")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> RequirementEnrichmentPublicationBindingPreviewV2:
        if (
            self.project_id != self.projection.project_id
            or self.document_id != self.projection.document_id
        ):
            raise ValueError("publication binding preview does not match its projection")
        expected_id = stable_publication_binding_preview_v2_id(
            session_id=self.session_id,
            review_revision=self.review_revision,
            base_active_requirement_set_id=self.base_active_requirement_set_id,
            projection_sha256=self.projection.projection_sha256,
        )
        if self.binding_preview_id != expected_id:
            raise ValueError("publication binding preview identity is not canonical")
        if self.payload_sha256 != canonical_publication_binding_preview_v2_sha256(
            self
        ):
            raise ValueError("publication binding preview hash does not match contents")
        return self

    @property
    def atomization_state(self) -> RequirementEnrichmentAtomizationStateV2:
        return self.projection.atomization_state


class RequirementEnrichmentPublicationRowBindingV2(_FrozenModel):
    schema_version: Literal[
        "requirement-enrichment-publication-row-binding/v2"
    ] = REQUIREMENT_ENRICHMENT_PUBLICATION_ROW_BINDING_V2_SCHEMA_VERSION
    binding_id: str = Field(min_length=1, max_length=96)
    publication_order: int = Field(ge=0)
    requirement_tree_node_id: str = Field(min_length=1, max_length=260)
    requirement_entry_id: str = Field(min_length=1, max_length=160)
    projection_row_sha256: str
    decision_trace_sha256: str
    projection_row: RequirementEnrichmentPublicationRowV2
    payload_sha256: str

    @field_validator("binding_id")
    @classmethod
    def validate_binding_id(cls, value: str) -> str:
        if not _BINDING_ID_RE.fullmatch(value):
            raise ValueError("publication binding identity is invalid")
        return value

    @field_validator(
        "projection_row_sha256", "decision_trace_sha256", "payload_sha256"
    )
    @classmethod
    def validate_row_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("publication row binding hash is invalid")
        return value

    @model_validator(mode="after")
    def validate_row_binding(self) -> RequirementEnrichmentPublicationRowBindingV2:
        if self.requirement_tree_node_id != self.projection_row.requirement_tree_node_id:
            raise ValueError("publication row binding tree identity is inconsistent")
        if self.projection_row_sha256 != _canonical_json_sha256(
            self.projection_row.model_dump(mode="json")
        ):
            raise ValueError("publication row projection hash does not match contents")
        if self.decision_trace_sha256 != publication_row_decision_trace_v2_sha256(
            binding_id=self.binding_id,
            publication_order=self.publication_order,
            requirement_entry_id=self.requirement_entry_id,
            projection_row_sha256=self.projection_row_sha256,
        ):
            raise ValueError("publication row trace hash does not match contents")
        if self.payload_sha256 != canonical_publication_row_binding_v2_sha256(self):
            raise ValueError("publication row binding hash does not match contents")
        return self


class RequirementEnrichmentPublicationStartV2(_FrozenModel):
    """The exact atomically-created PUBLISHING database materialization."""

    schema_version: Literal[
        "requirement-enrichment-publication-start/v2"
    ] = REQUIREMENT_ENRICHMENT_PUBLICATION_START_V2_SCHEMA_VERSION
    binding_id: str = Field(min_length=1, max_length=96)
    binding_preview_id: str = Field(min_length=1, max_length=96)
    binding_preview_sha256: str
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    review_revision: int = Field(ge=0)
    base_active_requirement_set_id: str = Field(default="", max_length=160)
    projection_sha256: str
    requirement_set_id: str = Field(min_length=1, max_length=160)
    requirement_set_version: int = Field(ge=1)
    row_bindings: tuple[RequirementEnrichmentPublicationRowBindingV2, ...]
    created: bool = True

    @field_validator("binding_id")
    @classmethod
    def validate_start_binding_id(cls, value: str) -> str:
        if not _BINDING_ID_RE.fullmatch(value):
            raise ValueError("publication binding identity is invalid")
        return value

    @field_validator("binding_preview_id")
    @classmethod
    def validate_start_preview_id(cls, value: str) -> str:
        if not _BINDING_PREVIEW_ID_RE.fullmatch(value):
            raise ValueError("publication binding preview identity is invalid")
        return value

    @field_validator("binding_preview_sha256", "projection_sha256")
    @classmethod
    def validate_start_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("publication start hash is invalid")
        return value

    @model_validator(mode="after")
    def validate_start(self) -> RequirementEnrichmentPublicationStartV2:
        if not self.row_bindings:
            raise ValueError("publication start requires materialized rows")
        if tuple(item.publication_order for item in self.row_bindings) != tuple(
            range(len(self.row_bindings))
        ):
            raise ValueError("publication rows must retain exact canonical order")
        if any(item.binding_id != self.binding_id for item in self.row_bindings):
            raise ValueError("publication rows do not match their binding")
        if self.binding_id != stable_publication_binding_v2_id(
            self.binding_preview_id
        ):
            raise ValueError("publication binding identity is not canonical")
        return self


class RequirementEnrichmentPublicationBindingV2(_FrozenModel):
    schema_version: Literal[
        "requirement-enrichment-publication-binding/v2"
    ] = REQUIREMENT_ENRICHMENT_PUBLICATION_BINDING_V2_SCHEMA_VERSION
    binding_id: str = Field(min_length=1, max_length=96)
    binding_preview_id: str = Field(min_length=1, max_length=96)
    binding_preview_sha256: str
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    review_revision: int = Field(ge=0)
    base_active_requirement_set_id: str = Field(default="", max_length=160)
    projection_sha256: str
    requirement_set_id: str = Field(min_length=1, max_length=160)
    requirement_set_version: int = Field(ge=1)
    requirement_tree_object_key: str = Field(min_length=1, max_length=1000)
    requirement_tree_sha256: str
    requirement_tree_schema_version: str = Field(min_length=1, max_length=160)
    row_bindings: tuple[RequirementEnrichmentPublicationRowBindingV2, ...]
    payload_sha256: str

    @field_validator("binding_id")
    @classmethod
    def validate_final_binding_id(cls, value: str) -> str:
        if not _BINDING_ID_RE.fullmatch(value):
            raise ValueError("publication binding identity is invalid")
        return value

    @field_validator("binding_preview_id")
    @classmethod
    def validate_final_preview_id(cls, value: str) -> str:
        if not _BINDING_PREVIEW_ID_RE.fullmatch(value):
            raise ValueError("publication binding preview identity is invalid")
        return value

    @field_validator(
        "binding_preview_sha256",
        "projection_sha256",
        "requirement_tree_sha256",
        "payload_sha256",
    )
    @classmethod
    def validate_final_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("publication binding hash is invalid")
        return value

    @model_validator(mode="after")
    def validate_final_binding(self) -> RequirementEnrichmentPublicationBindingV2:
        if self.binding_id != stable_publication_binding_v2_id(
            self.binding_preview_id
        ):
            raise ValueError("publication binding identity is not canonical")
        if not self.row_bindings or any(
            item.binding_id != self.binding_id for item in self.row_bindings
        ):
            raise ValueError("publication final binding rows are incomplete")
        if tuple(item.publication_order for item in self.row_bindings) != tuple(
            range(len(self.row_bindings))
        ):
            raise ValueError("publication final binding row order is invalid")
        if self.payload_sha256 != canonical_publication_binding_v2_sha256(self):
            raise ValueError("publication binding hash does not match contents")
        return self


def stable_publication_binding_preview_v2_id(
    *,
    session_id: str,
    review_revision: int,
    base_active_requirement_set_id: str,
    projection_sha256: str,
) -> str:
    seed = _canonical_json_sha256(
        {
            "schema_version": (
                REQUIREMENT_ENRICHMENT_PUBLICATION_BINDING_PREVIEW_V2_SCHEMA_VERSION
            ),
            "session_id": session_id,
            "review_revision": review_revision,
            "base_active_requirement_set_id": base_active_requirement_set_id,
            "projection_sha256": projection_sha256,
        }
    )
    return "requirement-enrichment-binding-preview-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"reqpilotx:w4:binding-preview:{seed}",
    ).hex


def stable_publication_binding_v2_id(binding_preview_id: str) -> str:
    return "requirement-enrichment-binding-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"reqpilotx:w4:binding:{binding_preview_id}",
    ).hex


def build_requirement_enrichment_publication_binding_preview_v2(
    *,
    session_id: str,
    review_revision: int,
    base_active_requirement_set_id: str,
    projection: RequirementEnrichmentPublicationProjectionV2,
) -> RequirementEnrichmentPublicationBindingPreviewV2:
    binding_preview_id = stable_publication_binding_preview_v2_id(
        session_id=session_id,
        review_revision=review_revision,
        base_active_requirement_set_id=base_active_requirement_set_id,
        projection_sha256=projection.projection_sha256,
    )
    fields: dict[str, Any] = {
        "binding_preview_id": binding_preview_id,
        "project_id": projection.project_id,
        "document_id": projection.document_id,
        "session_id": session_id,
        "review_revision": review_revision,
        "base_active_requirement_set_id": base_active_requirement_set_id,
        "projection": projection,
        "payload_sha256": "0" * 64,
    }
    draft = RequirementEnrichmentPublicationBindingPreviewV2.model_construct(**fields)
    fields["payload_sha256"] = canonical_publication_binding_preview_v2_sha256(
        draft
    )
    return RequirementEnrichmentPublicationBindingPreviewV2.model_validate(fields)


def build_requirement_enrichment_publication_row_bindings_v2(
    *,
    binding_id: str,
    projection: RequirementEnrichmentPublicationProjectionV2,
) -> tuple[RequirementEnrichmentPublicationRowBindingV2, ...]:
    result: list[RequirementEnrichmentPublicationRowBindingV2] = []
    for publication_order, row in enumerate(projection.rows):
        requirement_entry_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"reqpilotx:w4:{binding_id}:{row.requirement_tree_node_id}",
            )
        )
        row_sha256 = _canonical_json_sha256(row.model_dump(mode="json"))
        trace_sha256 = publication_row_decision_trace_v2_sha256(
            binding_id=binding_id,
            publication_order=publication_order,
            requirement_entry_id=requirement_entry_id,
            projection_row_sha256=row_sha256,
        )
        fields: dict[str, Any] = {
            "binding_id": binding_id,
            "publication_order": publication_order,
            "requirement_tree_node_id": row.requirement_tree_node_id,
            "requirement_entry_id": requirement_entry_id,
            "projection_row_sha256": row_sha256,
            "decision_trace_sha256": trace_sha256,
            "projection_row": row,
            "payload_sha256": "0" * 64,
        }
        draft = RequirementEnrichmentPublicationRowBindingV2.model_construct(**fields)
        fields["payload_sha256"] = canonical_publication_row_binding_v2_sha256(
            draft
        )
        result.append(RequirementEnrichmentPublicationRowBindingV2.model_validate(fields))
    return tuple(result)


def build_requirement_enrichment_publication_binding_v2(
    *,
    started: RequirementEnrichmentPublicationStartV2,
    requirement_tree_object_key: str,
    requirement_tree_sha256: str,
    requirement_tree_schema_version: str,
) -> RequirementEnrichmentPublicationBindingV2:
    fields = {
        "binding_id": started.binding_id,
        "binding_preview_id": started.binding_preview_id,
        "binding_preview_sha256": started.binding_preview_sha256,
        "project_id": started.project_id,
        "document_id": started.document_id,
        "session_id": started.session_id,
        "review_revision": started.review_revision,
        "base_active_requirement_set_id": started.base_active_requirement_set_id,
        "projection_sha256": started.projection_sha256,
        "requirement_set_id": started.requirement_set_id,
        "requirement_set_version": started.requirement_set_version,
        "row_bindings": started.row_bindings,
        "requirement_tree_object_key": requirement_tree_object_key,
        "requirement_tree_sha256": requirement_tree_sha256,
        "requirement_tree_schema_version": requirement_tree_schema_version,
        "payload_sha256": "0" * 64,
    }
    draft = RequirementEnrichmentPublicationBindingV2.model_construct(**fields)
    fields["payload_sha256"] = canonical_publication_binding_v2_sha256(draft)
    return RequirementEnrichmentPublicationBindingV2.model_validate(fields)


def canonical_publication_binding_preview_v2_sha256(
    preview: RequirementEnrichmentPublicationBindingPreviewV2,
) -> str:
    return _canonical_json_sha256(
        preview.model_dump(mode="json", exclude={"payload_sha256"})
    )


def canonical_publication_row_binding_v2_sha256(
    binding: RequirementEnrichmentPublicationRowBindingV2,
) -> str:
    return _canonical_json_sha256(
        binding.model_dump(mode="json", exclude={"payload_sha256"})
    )


def canonical_publication_binding_v2_sha256(
    binding: RequirementEnrichmentPublicationBindingV2,
) -> str:
    return _canonical_json_sha256(
        binding.model_dump(mode="json", exclude={"payload_sha256"})
    )


def publication_row_decision_trace_v2_sha256(
    *,
    binding_id: str,
    publication_order: int,
    requirement_entry_id: str,
    projection_row_sha256: str,
) -> str:
    return _canonical_json_sha256(
        {
            "schema_version": "requirement-enrichment-decision-trace/v2",
            "binding_id": binding_id,
            "publication_order": publication_order,
            "requirement_entry_id": requirement_entry_id,
            "projection_row_sha256": projection_row_sha256,
        }
    )


def build_requirement_enrichment_publication_decision_trace_v2(
    *,
    preview: RequirementEnrichmentPublicationBindingPreviewV2,
    row_binding: RequirementEnrichmentPublicationRowBindingV2,
) -> dict[str, Any]:
    """Rebuild the only canonical persisted v2 decision trace."""

    expected_binding_id = stable_publication_binding_v2_id(
        preview.binding_preview_id
    )
    if row_binding.binding_id != expected_binding_id:
        raise ValueError("publication trace row does not belong to its preview")
    order = row_binding.publication_order
    if order >= len(preview.projection.rows):
        raise ValueError("publication trace row order is outside its projection")
    if preview.projection.rows[order] != row_binding.projection_row:
        raise ValueError("publication trace row does not match its projection order")
    row = row_binding.projection_row
    return {
        "schema_version": "requirement-enrichment-decision-trace/v2",
        "binding_id": row_binding.binding_id,
        "binding_preview_id": preview.binding_preview_id,
        "binding_preview_sha256": preview.payload_sha256,
        "projection_sha256": preview.projection.projection_sha256,
        "projection_row_sha256": row_binding.projection_row_sha256,
        "decision_trace_sha256": row_binding.decision_trace_sha256,
        "publication_order": row_binding.publication_order,
        "requirement_tree_node_id": row_binding.requirement_tree_node_id,
        "source_working_tree_node_id": row.source_working_tree_node_id,
        "workflow_node_id": row.workflow_node_id,
        "atomization_node_id": row.atomization_node_id,
        "atomic_requirement_id": row.atomic_requirement_id,
        "decision_mode": row.decision_mode.value,
        "intrinsic_analysis_eligible": row.intrinsic_analysis_eligible,
        "default_disposition": row.default_disposition.value,
        "source_spans": [span.model_dump(mode="json") for span in row.source_spans],
    }


def canonical_publication_decision_trace_v2_json(
    *,
    preview: RequirementEnrichmentPublicationBindingPreviewV2,
    row_binding: RequirementEnrichmentPublicationRowBindingV2,
) -> str:
    return json.dumps(
        build_requirement_enrichment_publication_decision_trace_v2(
            preview=preview,
            row_binding=row_binding,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_json_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _reject_parent_cycles(
    row_by_id: dict[str, RequirementEnrichmentPublicationRowV2],
) -> None:
    resolved: set[str] = set()
    for row_id in row_by_id:
        if row_id in resolved:
            continue
        path: list[str] = []
        active: set[str] = set()
        current_id: str | None = row_id
        while current_id is not None:
            if current_id in resolved:
                break
            if current_id in active:
                raise ValueError("publication logical row hierarchy contains a cycle")
            active.add(current_id)
            path.append(current_id)
            current = row_by_id.get(current_id)
            current_id = (
                current.parent_requirement_tree_node_id if current is not None else None
            )
        resolved.update(path)


__all__ = [
    "REQUIREMENT_ENRICHMENT_PUBLICATION_BINDING_PREVIEW_V2_SCHEMA_VERSION",
    "REQUIREMENT_ENRICHMENT_PUBLICATION_BINDING_V2_SCHEMA_VERSION",
    "REQUIREMENT_ENRICHMENT_PUBLICATION_PROJECTION_V2_SCHEMA_VERSION",
    "REQUIREMENT_ENRICHMENT_PUBLICATION_ROW_BINDING_V2_SCHEMA_VERSION",
    "REQUIREMENT_ENRICHMENT_PUBLICATION_START_V2_SCHEMA_VERSION",
    "RequirementEnrichmentAnalysisDispositionV2",
    "RequirementEnrichmentAtomizationStateV2",
    "RequirementEnrichmentPublicationBindingPreviewV2",
    "RequirementEnrichmentPublicationBindingV2",
    "RequirementEnrichmentPublicationBuilderKindV2",
    "RequirementEnrichmentPublicationEntryKindV2",
    "RequirementEnrichmentPublicationProjectionV2",
    "RequirementEnrichmentPublicationRowV2",
    "RequirementEnrichmentPublicationRowBindingV2",
    "RequirementEnrichmentPublicationStartV2",
    "build_requirement_enrichment_publication_binding_preview_v2",
    "build_requirement_enrichment_publication_binding_v2",
    "build_requirement_enrichment_publication_decision_trace_v2",
    "build_requirement_enrichment_publication_row_bindings_v2",
    "canonical_publication_decision_trace_v2_json",
    "canonical_publication_binding_preview_v2_sha256",
    "canonical_publication_binding_v2_sha256",
    "canonical_publication_projection_v2_sha256",
    "canonical_publication_row_binding_v2_sha256",
    "publication_row_decision_trace_v2_sha256",
    "stable_publication_binding_preview_v2_id",
    "stable_publication_binding_v2_id",
]
