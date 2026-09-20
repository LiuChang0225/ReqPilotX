from __future__ import annotations

import re
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
)
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
)
from app.domain.extraction.structure_revision import (
    StructurePatchOperationV1,
    StructureSourceSpanV1,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STRUCTURE_NODE_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")
_EFFECTIVE_SNAPSHOT_ID_PATTERN = re.compile(r"^effective-structure-[0-9a-f]{32}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureOptimizationIssueCategory(str, Enum):
    DOCUMENT_REGION_ERROR = "document_region_error"
    MISPLACED_PARENT = "misplaced_parent"
    SOURCE_ORDER_ERROR = "source_order_error"
    MISSED_BOUNDARY = "missed_boundary"
    CONTINUATION_ERROR = "continuation_error"
    RELATION_ERROR = "relation_error"


class StructureOptimizationIssueOrigin(str, Enum):
    SOURCE_DOCUMENT = "source_document"
    BASE_RESOLVER = "base_resolver"
    UNCERTAIN = "uncertain"


class StructureOptimizationConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class StructureDocumentRegionKind(str, Enum):
    FRONT_MATTER = "front_matter"
    TABLE_OF_CONTENTS = "table_of_contents"
    MAIN_BODY = "main_body"
    APPENDIX = "appendix"
    BACK_MATTER = "back_matter"


class StructureOptimizerMapNodeV1(_StrictFrozenModel):
    """Compact, text-bounded outline input for whole-document reasoning."""

    structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    parent_structure_node_id: str | None = Field(
        default=None,
        pattern=_STRUCTURE_NODE_ID_PATTERN.pattern,
    )
    nearest_visible_parent_structure_node_id: str | None = Field(
        default=None,
        pattern=_STRUCTURE_NODE_ID_PATTERN.pattern,
    )
    kind: StructureNodeKind
    source_order: StrictInt = Field(ge=0)
    boundary_basis: StructureBoundaryBasis
    explicit_label: str | None = Field(default=None, max_length=80)
    list_ordinal: StrictInt | None = Field(default=None, ge=1, le=10_000)
    source_refs: tuple[StructureSourceRef, ...] = ()
    source_spans: tuple[StructureSourceSpanV1, ...] = ()
    page_numbers: tuple[StrictInt, ...] = ()
    text_preview: str = Field(default="", max_length=480)
    direct_child_count: StrictInt = Field(default=0, ge=0)
    omitted_direct_child_count: StrictInt = Field(default=0, ge=0)

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(
        cls,
        value: tuple[StructureSourceRef, ...],
    ) -> tuple[StructureSourceRef, ...]:
        if len(value) != len(set(value)):
            raise ValueError("optimizer map source refs must be unique")
        return value

    @field_validator("page_numbers")
    @classmethod
    def validate_page_numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page_number < 1 for page_number in value):
            raise ValueError("optimizer map page numbers must be positive")
        if value != tuple(sorted(set(value))):
            raise ValueError("optimizer map page numbers must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_node_shape(self) -> StructureOptimizerMapNodeV1:
        if self.parent_structure_node_id == self.structure_node_id:
            raise ValueError("optimizer map nodes cannot parent themselves")
        if self.nearest_visible_parent_structure_node_id == self.structure_node_id:
            raise ValueError("optimizer map visible parent cannot be the node itself")
        if self.omitted_direct_child_count > self.direct_child_count:
            raise ValueError(
                "omitted direct child count cannot exceed direct child count"
            )
        if any(span.source_ref not in self.source_refs for span in self.source_spans):
            raise ValueError("optimizer node spans must remain inside source refs")
        return self


class StructureOverviewRequestV1(_StrictFrozenModel):
    """One partition of a current-effective-bound whole-document overview."""

    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    revision_number: StrictInt = Field(ge=0)
    effective_snapshot_id: str = Field(pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern)
    effective_tree_sha256: str
    total_effective_node_count: StrictInt = Field(ge=1)
    map_truncated: StrictBool
    total_effective_relation_count: StrictInt = Field(ge=0)
    relation_map_truncated: StrictBool
    partition_index: StrictInt = Field(default=0, ge=0)
    partition_count: StrictInt = Field(default=1, ge=1, le=100_000)
    overview_coverage_sha256: str
    relation_coverage_sha256: str
    inspection_structure_node_ids: tuple[str, ...] = Field(
        default=(),
        max_length=4096,
    )
    inspection_structure_relation_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8192,
    )
    nodes: tuple[StructureOptimizerMapNodeV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    relations: tuple[StructureRelation, ...] = Field(
        default=(),
        max_length=8192,
    )

    @field_validator(
        "source_sha256",
        "effective_tree_sha256",
        "overview_coverage_sha256",
        "relation_coverage_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("optimizer request digests must be lowercase SHA-256")
        return value

    @field_validator("inspection_structure_node_ids")
    @classmethod
    def validate_inspection_node_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("optimizer inspection nodes must use Structure node IDs")
        if len(value) != len(set(value)):
            raise ValueError("optimizer inspection nodes must be unique")
        return value

    @field_validator("inspection_structure_relation_ids")
    @classmethod
    def validate_inspection_relation_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("optimizer inspection relations must be unique")
        return value

    @model_validator(mode="after")
    def validate_outline(self) -> StructureOverviewRequestV1:
        node_ids = tuple(node.structure_node_id for node in self.nodes)
        node_id_set = set(node_ids)
        if len(node_ids) != len(node_id_set):
            raise ValueError("optimizer overview nodes must be unique")
        if not any(node.kind is StructureNodeKind.DOCUMENT for node in self.nodes):
            raise ValueError("optimizer overview requires the document root")
        if len(self.nodes) > self.total_effective_node_count:
            raise ValueError("optimizer overview exceeds the effective node count")
        if self.map_truncated != (len(self.nodes) < self.total_effective_node_count):
            raise ValueError(
                "optimizer overview truncation must match effective node coverage"
            )
        if self.partition_index >= self.partition_count:
            raise ValueError("optimizer overview partition index is out of range")
        if (
            not self.inspection_structure_node_ids
            and not self.inspection_structure_relation_ids
        ):
            raise ValueError(
                "an overview partition must inspect nodes or non-parent relations"
            )
        if not set(self.inspection_structure_node_ids).issubset(node_id_set):
            raise ValueError(
                "optimizer inspection nodes must be present in the overview map"
            )
        if (
            self.partition_count == 1
            and not self.map_truncated
            and set(self.inspection_structure_node_ids) != node_id_set
        ):
            raise ValueError(
                "a complete single overview partition must inspect every node"
            )
        if any(
            node.nearest_visible_parent_structure_node_id is not None
            and node.nearest_visible_parent_structure_node_id not in node_id_set
            for node in self.nodes
        ):
            raise ValueError(
                "optimizer visible parents must be present in the overview map"
            )
        relation_ids = tuple(relation.relation_id for relation in self.relations)
        relation_id_set = set(relation_ids)
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("optimizer overview relations must be unique")
        if not set(self.inspection_structure_relation_ids).issubset(
            relation_id_set
        ):
            raise ValueError(
                "optimizer inspection relations must be present in the map"
            )
        if len(self.relations) > self.total_effective_relation_count:
            raise ValueError("optimizer overview exceeds the effective relation count")
        if self.relation_map_truncated != (
            len(self.relations) < self.total_effective_relation_count
        ):
            raise ValueError(
                "optimizer relation truncation must match effective relation coverage"
            )
        inspectable_relation_ids = {
            relation.relation_id
            for relation in self.relations
            if relation.kind is not StructureRelationKind.CONTAINS
        }
        if (
            self.partition_count == 1
            and not self.relation_map_truncated
            and set(self.inspection_structure_relation_ids)
            != inspectable_relation_ids
        ):
            raise ValueError(
                "a complete single overview partition must inspect every "
                "non-parent relation"
            )
        if any(
            relation.source_structure_node_id not in node_id_set
            or relation.target_structure_node_id not in node_id_set
            for relation in self.relations
        ):
            raise ValueError(
                "optimizer overview relation endpoints must be visible nodes"
            )
        return self


class StructureOptimizationIssueV1(_StrictFrozenModel):
    """Content-free overview conclusion grounded to current nodes and source refs."""

    category: StructureOptimizationIssueCategory
    target_structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    related_structure_node_ids: tuple[str, ...] = ()
    evidence_source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)
    origin: StructureOptimizationIssueOrigin
    confidence: StructureOptimizationConfidence
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("related_structure_node_ids")
    @classmethod
    def validate_related_node_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("optimizer related nodes must use Structure node IDs")
        if len(value) != len(set(value)):
            raise ValueError("optimizer related nodes must be unique")
        return value

    @field_validator("evidence_source_refs")
    @classmethod
    def validate_evidence_refs(
        cls,
        value: tuple[StructureSourceRef, ...],
    ) -> tuple[StructureSourceRef, ...]:
        if len(value) != len(set(value)):
            raise ValueError("optimizer evidence refs must be unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("optimizer reason codes must be machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("optimizer reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_issue_shape(self) -> StructureOptimizationIssueV1:
        if self.target_structure_node_id in self.related_structure_node_ids:
            raise ValueError("optimizer target cannot also be a related node")
        return self


class StructureDocumentRegionV1(_StrictFrozenModel):
    """One overview-level document range over visible current Structure nodes."""

    kind: StructureDocumentRegionKind
    start_structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    end_structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    evidence_source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)
    confidence: StructureOptimizationConfidence
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("evidence_source_refs")
    @classmethod
    def validate_evidence_refs(
        cls,
        value: tuple[StructureSourceRef, ...],
    ) -> tuple[StructureSourceRef, ...]:
        if len(value) != len(set(value)):
            raise ValueError("optimizer region evidence refs must be unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError(
                "optimizer region reason codes must be machine-readable keys"
            )
        if value != tuple(sorted(set(value))):
            raise ValueError("optimizer region reason codes must be unique and sorted")
        return value


class StructureOverviewProviderResultV1(_StrictFrozenModel):
    source_ir_id: str = Field(min_length=1, max_length=160)
    effective_snapshot_id: str = Field(pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern)
    document_regions: tuple[StructureDocumentRegionV1, ...] = Field(
        default=(),
        max_length=16,
    )
    issues: tuple[StructureOptimizationIssueV1, ...] = Field(
        default=(),
        max_length=64,
    )
    run: SemanticProviderRunRecord
    provider_runs: tuple[SemanticProviderRunRecord, ...] = ()

    @model_validator(mode="after")
    def validate_result_shape(self) -> StructureOverviewProviderResultV1:
        identities = tuple(
            (
                issue.category,
                issue.target_structure_node_id,
                issue.related_structure_node_ids,
            )
            for issue in self.issues
        )
        if len(identities) != len(set(identities)):
            raise ValueError("optimizer overview issues must be unique")
        region_identities = tuple(
            (
                region.kind,
                region.start_structure_node_id,
                region.end_structure_node_id,
            )
            for region in self.document_regions
        )
        if len(region_identities) != len(set(region_identities)):
            raise ValueError("optimizer document regions must be unique")
        if self.run.status is not SemanticProviderRunStatus.SUCCESS and (
            self.document_regions or self.issues
        ):
            raise ValueError(
                "an unresolved overview provider run cannot return conclusions"
            )
        if self.provider_runs and self.provider_runs[-1] != self.run:
            raise ValueError(
                "aggregated overview receipts must end with the exposed run"
            )
        if any(
            run.operation != "overview_structure" for run in self.provider_runs
        ):
            raise ValueError(
                "aggregated overview receipts must be overview operations"
            )
        return self


class StructureOptimizerSourceEvidenceV1(_StrictFrozenModel):
    """Ephemeral exact source text supplied only to the focused repair call."""

    source_ref: StructureSourceRef
    raw_text: str
    page_numbers: tuple[StrictInt, ...] = ()

    @field_validator("page_numbers")
    @classmethod
    def validate_page_numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page_number < 1 for page_number in value):
            raise ValueError("optimizer evidence page numbers must be positive")
        if value != tuple(sorted(set(value))):
            raise ValueError(
                "optimizer evidence page numbers must be unique and sorted"
            )
        return value


class StructureRepairRequestV1(_StrictFrozenModel):
    """Focused evidence for one direct, aggregate patch decision."""

    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    revision_number: StrictInt = Field(ge=0)
    effective_snapshot_id: str = Field(pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern)
    effective_tree_sha256: str
    total_effective_node_count: StrictInt = Field(ge=1)
    focus_truncated: StrictBool
    total_effective_relation_count: StrictInt = Field(ge=0)
    overview_issues: tuple[StructureOptimizationIssueV1, ...] = Field(
        min_length=1,
        max_length=64,
    )
    document_regions: tuple[StructureDocumentRegionV1, ...] = Field(
        default=(),
        max_length=16,
    )
    nodes: tuple[StructureOptimizerMapNodeV1, ...] = Field(
        min_length=1,
        max_length=1024,
    )
    evidence: tuple[StructureOptimizerSourceEvidenceV1, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    relations: tuple[StructureRelation, ...] = Field(
        default=(),
        max_length=8192,
    )

    @field_validator("source_sha256", "effective_tree_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("optimizer request digests must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_focus(self) -> StructureRepairRequestV1:
        node_ids = tuple(node.structure_node_id for node in self.nodes)
        node_id_set = set(node_ids)
        if len(node_ids) != len(node_id_set):
            raise ValueError("optimizer repair nodes must be unique")
        if len(self.nodes) > self.total_effective_node_count:
            raise ValueError("optimizer repair exceeds the effective node count")
        evidence_refs = tuple(item.source_ref for item in self.evidence)
        evidence_ref_set = set(evidence_refs)
        if len(evidence_refs) != len(evidence_ref_set):
            raise ValueError("optimizer repair evidence refs must be unique")
        if any(
            issue.target_structure_node_id not in node_id_set
            or not set(issue.related_structure_node_ids).issubset(node_id_set)
            for issue in self.overview_issues
        ):
            raise ValueError("optimizer repair issues must remain inside focus nodes")
        if any(
            not set(issue.evidence_source_refs).issubset(evidence_ref_set)
            for issue in self.overview_issues
        ):
            raise ValueError(
                "optimizer repair must include every issue evidence source ref"
            )
        if any(
            region.start_structure_node_id not in node_id_set
            or region.end_structure_node_id not in node_id_set
            for region in self.document_regions
        ):
            raise ValueError(
                "optimizer repair document regions must remain inside focus nodes"
            )
        if any(
            not set(region.evidence_source_refs).issubset(evidence_ref_set)
            for region in self.document_regions
        ):
            raise ValueError(
                "optimizer repair must include every region evidence source ref"
            )
        if any(
            not set(node.source_refs).issubset(evidence_ref_set) for node in self.nodes
        ):
            raise ValueError(
                "optimizer repair must include exact evidence for every focus node"
            )
        relation_ids = tuple(relation.relation_id for relation in self.relations)
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("optimizer repair relations must be unique")
        if len(self.relations) > self.total_effective_relation_count:
            raise ValueError("optimizer repair exceeds the effective relation count")
        if any(
            relation.source_structure_node_id not in node_id_set
            or relation.target_structure_node_id not in node_id_set
            for relation in self.relations
        ):
            raise ValueError("optimizer repair relation endpoints must be focus nodes")
        return self


class StructureRepairProviderResultV1(_StrictFrozenModel):
    source_ir_id: str = Field(min_length=1, max_length=160)
    effective_snapshot_id: str = Field(pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern)
    operations: tuple[StructurePatchOperationV1, ...] = Field(
        default=(),
        max_length=64,
    )
    run: SemanticProviderRunRecord

    @model_validator(mode="after")
    def validate_result_shape(self) -> StructureRepairProviderResultV1:
        if self.run.status is not SemanticProviderRunStatus.SUCCESS and self.operations:
            raise ValueError(
                "an unresolved repair provider run cannot return patch operations"
            )
        return self


@runtime_checkable
class StructureOptimizerProviderPort(Protocol):
    provider_id: str
    provider_version: str
    model_id: str

    def overview_structure(
        self,
        request: StructureOverviewRequestV1,
    ) -> StructureOverviewProviderResultV1: ...

    def repair_structure(
        self,
        request: StructureRepairRequestV1,
    ) -> StructureRepairProviderResultV1: ...


__all__ = [
    "StructureDocumentRegionKind",
    "StructureDocumentRegionV1",
    "StructureOptimizationConfidence",
    "StructureOptimizationIssueCategory",
    "StructureOptimizationIssueOrigin",
    "StructureOptimizationIssueV1",
    "StructureOptimizerMapNodeV1",
    "StructureOptimizerProviderPort",
    "StructureOptimizerSourceEvidenceV1",
    "StructureOverviewProviderResultV1",
    "StructureOverviewRequestV1",
    "StructureRepairProviderResultV1",
    "StructureRepairRequestV1",
]
