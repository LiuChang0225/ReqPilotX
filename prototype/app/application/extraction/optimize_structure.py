from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.application.extraction.provider_source_policy import (
    require_complete_source_ir_for_provider,
)
from app.application.extraction.revise_structure import (
    apply_structure_patch,
    create_structure_patch_proposal,
    validate_structure_patch_proposal,
    validate_structure_revision_chain,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import (
    StructureNode,
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from app.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    EffectiveStructureSnapshotV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructureApplyResultV1,
    StructurePatchOperationV1,
    StructurePatchProposalV1,
    StructureRevisionV1,
    StructureSourceSpanV1,
    canonical_model_sha256,
)
from app.ports.structure_optimizer_provider import (
    StructureDocumentRegionV1,
    StructureOptimizationConfidence,
    StructureOptimizationIssueOrigin,
    StructureOptimizationIssueV1,
    StructureOptimizerMapNodeV1,
    StructureOptimizerProviderPort,
    StructureOptimizerSourceEvidenceV1,
    StructureOverviewProviderResultV1,
    StructureOverviewRequestV1,
    StructureRepairProviderResultV1,
    StructureRepairRequestV1,
)


STRUCTURE_OPTIMIZATION_OUTCOME_SCHEMA_VERSION = "structure-optimization-outcome/v1"
STRUCTURE_OPTIMIZATION_PROPOSAL_OUTCOME_SCHEMA_VERSION = (
    "structure-optimization-proposal-outcome/v1"
)
DEFAULT_STRUCTURE_OVERVIEW_MAX_NODES = 2048
DEFAULT_STRUCTURE_OVERVIEW_PREVIEW_CHARS = 320
DEFAULT_STRUCTURE_REPAIR_MAX_NODES = 1024
DEFAULT_STRUCTURE_REPAIR_MAX_EVIDENCE_UTF8_BYTES = 512 * 1024
_OVERVIEW_PRIMARY_KINDS = frozenset(
    {
        StructureNodeKind.DOCUMENT,
        StructureNodeKind.SECTION,
        StructureNodeKind.TABLE,
        StructureNodeKind.LIST_GROUP,
        StructureNodeKind.GROUP,
    }
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureOptimizationStatus(str, Enum):
    APPLIED = "APPLIED"
    NO_CHANGE = "NO_CHANGE"
    OVERVIEW_UNRESOLVED = "OVERVIEW_UNRESOLVED"
    REPAIR_UNRESOLVED = "REPAIR_UNRESOLVED"


class StructureOptimizationProposalStatus(str, Enum):
    PROPOSED = "PROPOSED"
    NO_CHANGE = "NO_CHANGE"
    OVERVIEW_UNRESOLVED = "OVERVIEW_UNRESOLVED"
    REPAIR_UNRESOLVED = "REPAIR_UNRESOLVED"


class StructureOptimizationProposalOutcomeV1(_StrictFrozenModel):
    """Write-free optimizer result that can be committed against a fresh chain."""

    schema_version: Literal["structure-optimization-proposal-outcome/v1"] = (
        STRUCTURE_OPTIMIZATION_PROPOSAL_OUTCOME_SCHEMA_VERSION
    )
    status: StructureOptimizationProposalStatus
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    expected_revision_number: StrictInt = Field(ge=0)
    expected_effective_snapshot_id: str = Field(min_length=1, max_length=160)
    expected_effective_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_head_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    overview: StructureOverviewProviderResultV1
    repair: StructureRepairProviderResultV1 | None = None
    proposal: StructurePatchProposalV1 | None = None
    provider_runs: tuple[SemanticProviderRunRecord, ...]

    @model_validator(mode="after")
    def validate_proposal_outcome(
        self,
    ) -> StructureOptimizationProposalOutcomeV1:
        overview_runs = _overview_provider_runs(self.overview)
        expected_runs = (
            overview_runs if self.repair is None else (*overview_runs, self.repair.run)
        )
        if self.provider_runs != expected_runs:
            raise ValueError("optimizer proposal must retain its provider receipts")
        if (
            self.source_ir_id != self.overview.source_ir_id
            or self.expected_effective_snapshot_id
            != self.overview.effective_snapshot_id
        ):
            raise ValueError("optimizer proposal overview binding is inconsistent")
        if self.repair is not None and (
            self.repair.source_ir_id != self.source_ir_id
            or self.repair.effective_snapshot_id != self.expected_effective_snapshot_id
        ):
            raise ValueError("optimizer proposal repair binding is inconsistent")
        if self.status is StructureOptimizationProposalStatus.PROPOSED:
            if self.proposal is None or self.repair is None:
                raise ValueError(
                    "a proposed optimization requires repair and a patch proposal"
                )
            if (
                self.proposal.source_ir_id != self.source_ir_id
                or self.proposal.source_sha256 != self.source_sha256
                or self.proposal.base_structure_snapshot_id
                != self.base_structure_snapshot_id
                or self.proposal.expected_revision_number
                != self.expected_revision_number
                or self.proposal.expected_effective_snapshot_id
                != self.expected_effective_snapshot_id
                or self.proposal.operations != self.repair.operations
            ):
                raise ValueError(
                    "optimizer patch proposal is not bound to its provider decision"
                )
        elif self.proposal is not None:
            raise ValueError("only a proposed optimization may carry a proposal")
        if self.status is StructureOptimizationProposalStatus.OVERVIEW_UNRESOLVED:
            if (
                self.overview.run.status is SemanticProviderRunStatus.SUCCESS
                or self.repair is not None
            ):
                raise ValueError(
                    "overview-unresolved proposals stop before focused repair"
                )
        if self.status is StructureOptimizationProposalStatus.REPAIR_UNRESOLVED:
            if (
                self.repair is None
                or self.repair.run.status is SemanticProviderRunStatus.SUCCESS
            ):
                raise ValueError(
                    "repair-unresolved proposals require a failed repair receipt"
                )
        return self


class StructureOptimizationOutcomeV1(_StrictFrozenModel):
    """Content-free receipt for one direct optimizer invocation."""

    schema_version: Literal["structure-optimization-outcome/v1"] = (
        STRUCTURE_OPTIMIZATION_OUTCOME_SCHEMA_VERSION
    )
    status: StructureOptimizationStatus
    source_ir_id: str = Field(min_length=1, max_length=160)
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    previous_effective_snapshot_id: str = Field(min_length=1, max_length=160)
    effective_snapshot: EffectiveStructureSnapshotV1
    overview: StructureOverviewProviderResultV1
    repair: StructureRepairProviderResultV1 | None = None
    apply_result: StructureApplyResultV1 | None = None
    provider_runs: tuple[SemanticProviderRunRecord, ...]

    @model_validator(mode="after")
    def validate_outcome(self) -> StructureOptimizationOutcomeV1:
        overview_runs = _overview_provider_runs(self.overview)
        expected_runs = (
            overview_runs if self.repair is None else (*overview_runs, self.repair.run)
        )
        if self.provider_runs != expected_runs:
            raise ValueError("optimizer outcome must retain all provider receipts")
        if self.status is StructureOptimizationStatus.APPLIED:
            if self.repair is None or self.apply_result is None:
                raise ValueError(
                    "an applied optimizer outcome requires repair and apply"
                )
            if self.effective_snapshot != self.apply_result.effective_snapshot:
                raise ValueError(
                    "optimizer effective snapshot must be the applied snapshot"
                )
        elif self.apply_result is not None:
            raise ValueError("only an applied optimizer outcome may carry apply_result")
        if self.status is StructureOptimizationStatus.OVERVIEW_UNRESOLVED:
            if (
                self.overview.run.status is SemanticProviderRunStatus.SUCCESS
                or self.repair is not None
            ):
                raise ValueError(
                    "overview-unresolved outcomes stop before focused repair"
                )
        if self.status is StructureOptimizationStatus.REPAIR_UNRESOLVED:
            if (
                self.repair is None
                or self.repair.run.status is SemanticProviderRunStatus.SUCCESS
            ):
                raise ValueError(
                    "repair-unresolved outcomes require a failed repair receipt"
                )
        if self.source_ir_id != self.effective_snapshot.source_ir_id:
            raise ValueError("optimizer outcome source binding is inconsistent")
        if (
            self.base_structure_snapshot_id
            != self.effective_snapshot.base_structure_snapshot_id
        ):
            raise ValueError("optimizer outcome base binding is inconsistent")
        return self


def optimize_structure_with_provider(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    provider: StructureOptimizerProviderPort,
    *,
    overview_max_nodes: int = DEFAULT_STRUCTURE_OVERVIEW_MAX_NODES,
    overview_preview_chars: int = DEFAULT_STRUCTURE_OVERVIEW_PREVIEW_CHARS,
    repair_max_nodes: int = DEFAULT_STRUCTURE_REPAIR_MAX_NODES,
    repair_max_evidence_utf8_bytes: int = (
        DEFAULT_STRUCTURE_REPAIR_MAX_EVIDENCE_UTF8_BYTES
    ),
) -> StructureOptimizationOutcomeV1:
    """Run, propose and immediately apply against the supplied revision chain."""

    proposal_outcome = propose_structure_optimization_with_provider(
        source_ir,
        base_tree,
        revisions,
        provider,
        overview_max_nodes=overview_max_nodes,
        overview_preview_chars=overview_preview_chars,
        repair_max_nodes=repair_max_nodes,
        repair_max_evidence_utf8_bytes=repair_max_evidence_utf8_bytes,
    )
    if proposal_outcome.status is StructureOptimizationProposalStatus.PROPOSED:
        return apply_structure_optimization_proposal(
            proposal_outcome,
            source_ir,
            base_tree,
            revisions,
        )
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    return _outcome(
        status=StructureOptimizationStatus(proposal_outcome.status.value),
        current=current,
        overview=proposal_outcome.overview,
        repair=proposal_outcome.repair,
    )


def propose_structure_optimization_with_provider(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    provider: StructureOptimizerProviderPort,
    *,
    overview_max_nodes: int = DEFAULT_STRUCTURE_OVERVIEW_MAX_NODES,
    overview_preview_chars: int = DEFAULT_STRUCTURE_OVERVIEW_PREVIEW_CHARS,
    repair_max_nodes: int = DEFAULT_STRUCTURE_REPAIR_MAX_NODES,
    repair_max_evidence_utf8_bytes: int = (
        DEFAULT_STRUCTURE_REPAIR_MAX_EVIDENCE_UTF8_BYTES
    ),
) -> StructureOptimizationProposalOutcomeV1:
    """Call the provider and return an immutable proposal with no write authority."""

    if not isinstance(provider, StructureOptimizerProviderPort):
        raise TypeError("provider must implement StructureOptimizerProviderPort")
    require_complete_source_ir_for_provider(
        source_ir,
        operation="structure_optimization",
    )
    source_hash_before = canonical_model_sha256(source_ir)
    base_hash_before = canonical_model_sha256(base_tree)
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    overview_requests = plan_structure_overview_requests(
        source_ir,
        current,
        max_nodes=overview_max_nodes,
        preview_chars=overview_preview_chars,
    )
    overview_results: list[StructureOverviewProviderResultV1] = []
    for overview_request in overview_requests:
        overview_result = provider.overview_structure(overview_request)
        _validate_overview_result(overview_request, overview_result, provider)
        overview_results.append(overview_result)
        if overview_result.run.status is not SemanticProviderRunStatus.SUCCESS:
            break
    overview = _aggregate_overview_results(
        overview_requests[: len(overview_results)],
        tuple(overview_results),
    )
    if overview.run.status is not SemanticProviderRunStatus.SUCCESS:
        _require_immutable_inputs_unchanged(
            source_ir,
            base_tree,
            source_hash_before=source_hash_before,
            base_hash_before=base_hash_before,
        )
        return _proposal_outcome(
            status=StructureOptimizationProposalStatus.OVERVIEW_UNRESOLVED,
            current=current,
            overview=overview,
        )
    if not overview.issues:
        _require_immutable_inputs_unchanged(
            source_ir,
            base_tree,
            source_hash_before=source_hash_before,
            base_hash_before=base_hash_before,
        )
        return _proposal_outcome(
            status=StructureOptimizationProposalStatus.NO_CHANGE,
            current=current,
            overview=overview,
        )

    repair_request = plan_structure_repair_request(
        source_ir,
        current,
        overview.issues,
        document_regions=overview.document_regions,
        max_nodes=repair_max_nodes,
        max_evidence_utf8_bytes=repair_max_evidence_utf8_bytes,
    )
    repair = provider.repair_structure(repair_request)
    _validate_repair_result(repair_request, repair, provider)
    if repair.run.status is not SemanticProviderRunStatus.SUCCESS:
        _require_immutable_inputs_unchanged(
            source_ir,
            base_tree,
            source_hash_before=source_hash_before,
            base_hash_before=base_hash_before,
        )
        return _proposal_outcome(
            status=StructureOptimizationProposalStatus.REPAIR_UNRESOLVED,
            current=current,
            overview=overview,
            repair=repair,
        )
    if not repair.operations:
        _require_immutable_inputs_unchanged(
            source_ir,
            base_tree,
            source_hash_before=source_hash_before,
            base_hash_before=base_hash_before,
        )
        return _proposal_outcome(
            status=StructureOptimizationProposalStatus.NO_CHANGE,
            current=current,
            overview=overview,
            repair=repair,
        )

    _require_operations_inside_repair_scope(repair.operations, repair_request)
    proposal = create_structure_patch_proposal(
        source_ir,
        base_tree,
        revisions,
        repair.operations,
    )
    validate_structure_patch_proposal(
        proposal,
        source_ir,
        base_tree,
        revisions,
    )
    _require_immutable_inputs_unchanged(
        source_ir,
        base_tree,
        source_hash_before=source_hash_before,
        base_hash_before=base_hash_before,
    )
    return _proposal_outcome(
        status=StructureOptimizationProposalStatus.PROPOSED,
        current=current,
        overview=overview,
        repair=repair,
        proposal=proposal,
    )


def apply_structure_optimization_proposal(
    proposal_outcome: StructureOptimizationProposalOutcomeV1,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
) -> StructureOptimizationOutcomeV1:
    """Validate a provider proposal again against a freshly loaded chain."""

    if not isinstance(
        proposal_outcome,
        StructureOptimizationProposalOutcomeV1,
    ):
        raise TypeError(
            "proposal_outcome must be a StructureOptimizationProposalOutcomeV1"
        )
    if (
        proposal_outcome.status is not StructureOptimizationProposalStatus.PROPOSED
        or proposal_outcome.proposal is None
        or proposal_outcome.repair is None
    ):
        raise ValueError("only a proposed Structure optimization can be applied")
    require_complete_source_ir_for_provider(
        source_ir,
        operation="structure_optimization_apply",
    )
    source_hash_before = canonical_model_sha256(source_ir)
    base_hash_before = canonical_model_sha256(base_tree)
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    patch = validate_structure_patch_proposal(
        proposal_outcome.proposal,
        source_ir,
        base_tree,
        revisions,
    )
    if (
        current.effective_tree_sha256 != proposal_outcome.expected_effective_tree_sha256
        or current.revision_chain_sha256
        != proposal_outcome.expected_revision_chain_sha256
        or current.head_revision_id != proposal_outcome.expected_head_revision_id
    ):
        raise ValueError(
            "Structure optimization proposal identity does not match the fresh chain"
        )
    applied = apply_structure_patch(
        patch,
        source_ir,
        base_tree,
        revisions,
    )
    _require_immutable_inputs_unchanged(
        source_ir,
        base_tree,
        source_hash_before=source_hash_before,
        base_hash_before=base_hash_before,
    )
    return _outcome(
        status=StructureOptimizationStatus.APPLIED,
        current=current,
        overview=proposal_outcome.overview,
        repair=proposal_outcome.repair,
        apply_result=applied,
    )


def plan_structure_overview_request(
    source_ir: SourceIRSnapshot,
    current: EffectiveStructureSnapshotV1,
    *,
    max_nodes: int = DEFAULT_STRUCTURE_OVERVIEW_MAX_NODES,
    preview_chars: int = DEFAULT_STRUCTURE_OVERVIEW_PREVIEW_CHARS,
) -> StructureOverviewRequestV1:
    """Build one bounded sampled map for compatibility and diagnostics.

    Direct optimization uses :func:`plan_structure_overview_requests`, which
    gives every effective node exactly one inspection owner. This singular
    helper intentionally remains useful for inspecting the previous bounded
    map contract without implying full-document coverage.
    """

    _require_positive_limit(max_nodes, "max_nodes", maximum=4096)
    _require_positive_limit(preview_chars, "preview_chars", maximum=480)
    selected_ids = _select_overview_node_ids(current.nodes, max_nodes=max_nodes)
    inspection_relation_ids = {
        relation.relation_id
        for relation in current.relations
        if relation.kind is not StructureRelationKind.CONTAINS
        and relation.source_structure_node_id in selected_ids
        and relation.target_structure_node_id in selected_ids
    }
    return _build_structure_overview_request(
        source_ir,
        current,
        selected_ids=selected_ids,
        inspection_ids=selected_ids,
        inspection_relation_ids=inspection_relation_ids,
        partition_index=0,
        partition_count=1,
        preview_chars=preview_chars,
    )


def plan_structure_overview_requests(
    source_ir: SourceIRSnapshot,
    current: EffectiveStructureSnapshotV1,
    *,
    max_nodes: int = DEFAULT_STRUCTURE_OVERVIEW_MAX_NODES,
    preview_chars: int = DEFAULT_STRUCTURE_OVERVIEW_PREVIEW_CHARS,
) -> tuple[StructureOverviewRequestV1, ...]:
    """Partition the effective tree into bounded, deterministic inspection maps.

    Every current node belongs to exactly one contiguous inspection partition.
    Ancestors and at most one source-order neighbor on each side may repeat as
    read-only context, so a model never needs to infer an omitted parent.
    """

    _require_positive_limit(max_nodes, "max_nodes", maximum=4096)
    _require_positive_limit(preview_chars, "preview_chars", maximum=480)
    ordered = tuple(
        sorted(
            current.nodes,
            key=lambda node: (node.source_order, node.structure_node_id),
        )
    )
    if not ordered:
        raise ValueError("Structure overview requires at least one effective node")
    nodes_by_id = {node.structure_node_id: node for node in ordered}
    ancestor_ids_by_node_id = {
        node.structure_node_id: _ancestor_ids(node, nodes_by_id) for node in ordered
    }
    node_specifications: list[tuple[set[str], set[str], set[str]]] = []
    cursor = 0
    while cursor < len(ordered):
        start = cursor
        inspection_ids: set[str] = set()
        selected_ids: set[str] = set()
        while cursor < len(ordered):
            node_id = ordered[cursor].structure_node_id
            required_ids = {
                node_id,
                *ancestor_ids_by_node_id[node_id],
            }
            if len(selected_ids | required_ids) > max_nodes:
                break
            inspection_ids.add(node_id)
            selected_ids.update(required_ids)
            cursor += 1
        if not inspection_ids:
            node_id = ordered[cursor].structure_node_id
            required_count = 1 + len(ancestor_ids_by_node_id[node_id])
            raise ValueError(
                "whole-document Structure overview node budget cannot fit one "
                f"inspection path ({required_count} nodes required)"
            )

        for neighbor_index in (start - 1, cursor):
            if neighbor_index < 0 or neighbor_index >= len(ordered):
                continue
            neighbor_id = ordered[neighbor_index].structure_node_id
            context_ids = {
                neighbor_id,
                *ancestor_ids_by_node_id[neighbor_id],
            }
            if len(selected_ids | context_ids) <= max_nodes:
                selected_ids.update(context_ids)
        node_specifications.append((inspection_ids, selected_ids, set()))

    inspectable_relations = tuple(
        relation
        for relation in current.relations
        if relation.kind is not StructureRelationKind.CONTAINS
    )
    assigned_relation_ids: set[str] = set()
    for _, selected_ids, inspection_relation_ids in node_specifications:
        for relation in inspectable_relations:
            if (
                relation.relation_id not in assigned_relation_ids
                and relation.source_structure_node_id in selected_ids
                and relation.target_structure_node_id in selected_ids
            ):
                assigned_relation_ids.add(relation.relation_id)
                inspection_relation_ids.add(relation.relation_id)

    relation_specifications: list[tuple[set[str], set[str], set[str]]] = []
    relation_cursor = 0
    uncovered_relations = tuple(
        relation
        for relation in inspectable_relations
        if relation.relation_id not in assigned_relation_ids
    )
    while relation_cursor < len(uncovered_relations):
        selected_ids: set[str] = set()
        inspection_relation_ids: set[str] = set()
        while relation_cursor < len(uncovered_relations):
            relation = uncovered_relations[relation_cursor]
            endpoint_ids = {
                relation.source_structure_node_id,
                relation.target_structure_node_id,
            }
            required_ids = {
                node_id
                for endpoint_id in endpoint_ids
                for node_id in (
                    endpoint_id,
                    *ancestor_ids_by_node_id[endpoint_id],
                )
            }
            if len(selected_ids | required_ids) > max_nodes:
                break
            selected_ids.update(required_ids)
            inspection_relation_ids.add(relation.relation_id)
            relation_cursor += 1
        if not inspection_relation_ids:
            relation = uncovered_relations[relation_cursor]
            required_ids = {
                node_id
                for endpoint_id in (
                    relation.source_structure_node_id,
                    relation.target_structure_node_id,
                )
                for node_id in (
                    endpoint_id,
                    *ancestor_ids_by_node_id[endpoint_id],
                )
            }
            raise ValueError(
                "whole-document Structure overview node budget cannot fit one "
                f"relation context ({len(required_ids)} nodes required)"
            )
        relation_specifications.append((set(), selected_ids, inspection_relation_ids))

    specifications = (*node_specifications, *relation_specifications)
    inspected_node_ids = [
        node_id for inspection_ids, _, _ in specifications for node_id in inspection_ids
    ]
    inspected_relation_ids = [
        relation_id
        for _, _, relation_ids in specifications
        for relation_id in relation_ids
    ]
    if (
        len(inspected_node_ids) != len(set(inspected_node_ids))
        or set(inspected_node_ids) != set(nodes_by_id)
        or len(inspected_relation_ids) != len(set(inspected_relation_ids))
        or set(inspected_relation_ids)
        != {relation.relation_id for relation in inspectable_relations}
    ):
        raise RuntimeError("Structure overview partition coverage is inconsistent")
    partition_count = len(specifications)
    return tuple(
        _build_structure_overview_request(
            source_ir,
            current,
            selected_ids=selected_ids,
            inspection_ids=inspection_ids,
            inspection_relation_ids=inspection_relation_ids,
            partition_index=partition_index,
            partition_count=partition_count,
            preview_chars=preview_chars,
        )
        for partition_index, (
            inspection_ids,
            selected_ids,
            inspection_relation_ids,
        ) in enumerate(specifications)
    )


def _build_structure_overview_request(
    source_ir: SourceIRSnapshot,
    current: EffectiveStructureSnapshotV1,
    *,
    selected_ids: set[str],
    inspection_ids: set[str],
    inspection_relation_ids: set[str],
    partition_index: int,
    partition_count: int,
    preview_chars: int,
) -> StructureOverviewRequestV1:
    catalog = _source_catalog(source_ir)
    source_spans_by_node_id = _source_spans_by_node_id(current)
    nodes = _map_nodes(
        current.nodes,
        selected_ids=selected_ids,
        catalog=catalog,
        source_spans_by_node_id=source_spans_by_node_id,
        preview_chars=preview_chars,
    )
    relations = tuple(
        relation
        for relation in current.relations
        if relation.source_structure_node_id in selected_ids
        and relation.target_structure_node_id in selected_ids
    )
    ordered_ids = tuple(
        node.structure_node_id
        for node in sorted(
            current.nodes,
            key=lambda node: (node.source_order, node.structure_node_id),
        )
    )
    ordered_relation_ids = tuple(
        relation.relation_id
        for relation in current.relations
        if relation.kind is not StructureRelationKind.CONTAINS
    )
    return StructureOverviewRequestV1(
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        revision_number=current.revision_number,
        effective_snapshot_id=current.effective_snapshot_id,
        effective_tree_sha256=current.effective_tree_sha256,
        total_effective_node_count=len(current.nodes),
        map_truncated=len(nodes) < len(current.nodes),
        total_effective_relation_count=len(current.relations),
        relation_map_truncated=len(relations) < len(current.relations),
        partition_index=partition_index,
        partition_count=partition_count,
        overview_coverage_sha256=_overview_coverage_sha256(ordered_ids),
        relation_coverage_sha256=_overview_coverage_sha256(ordered_relation_ids),
        inspection_structure_node_ids=tuple(
            node_id for node_id in ordered_ids if node_id in inspection_ids
        ),
        inspection_structure_relation_ids=tuple(
            relation_id
            for relation_id in ordered_relation_ids
            if relation_id in inspection_relation_ids
        ),
        nodes=nodes,
        relations=relations,
    )


def plan_structure_repair_request(
    source_ir: SourceIRSnapshot,
    current: EffectiveStructureSnapshotV1,
    issues: tuple[StructureOptimizationIssueV1, ...],
    *,
    document_regions: tuple[StructureDocumentRegionV1, ...] = (),
    max_nodes: int = DEFAULT_STRUCTURE_REPAIR_MAX_NODES,
    max_evidence_utf8_bytes: int = (DEFAULT_STRUCTURE_REPAIR_MAX_EVIDENCE_UTF8_BYTES),
) -> StructureRepairRequestV1:
    """Expand suspicious outline scopes into exact, bounded source evidence."""

    if not issues:
        raise ValueError("focused Structure repair requires at least one issue")
    _require_positive_limit(max_nodes, "max_nodes", maximum=1024)
    _require_positive_limit(
        max_evidence_utf8_bytes,
        "max_evidence_utf8_bytes",
        maximum=8 * 1024 * 1024,
    )
    catalog = _source_catalog(source_ir)
    source_spans_by_node_id = _source_spans_by_node_id(current)
    selected_ids, focus_truncated = _select_repair_node_ids(
        current.nodes,
        current.relations,
        issues,
        document_regions,
        max_nodes=max_nodes,
    )
    nodes = _map_nodes(
        current.nodes,
        selected_ids=selected_ids,
        catalog=catalog,
        source_spans_by_node_id=source_spans_by_node_id,
        preview_chars=DEFAULT_STRUCTURE_OVERVIEW_PREVIEW_CHARS,
    )
    issue_refs = tuple(
        dict.fromkeys(
            source_ref for issue in issues for source_ref in issue.evidence_source_refs
        )
    )
    region_refs = tuple(
        dict.fromkeys(
            source_ref
            for region in document_regions
            for source_ref in region.evidence_source_refs
        )
    )
    evidence_refs = tuple(
        dict.fromkeys(
            (
                *(source_ref for node in nodes for source_ref in node.source_refs),
                *issue_refs,
                *region_refs,
            )
        )
    )
    if any(source_ref not in catalog for source_ref in evidence_refs):
        raise ValueError("overview evidence is not grounded in the immutable SourceIR")
    evidence = tuple(
        StructureOptimizerSourceEvidenceV1(
            source_ref=source_ref,
            raw_text=catalog[source_ref][0],
            page_numbers=catalog[source_ref][1],
        )
        for source_ref in evidence_refs
    )
    evidence_bytes = sum(len(item.raw_text.encode("utf-8")) for item in evidence)
    if evidence_bytes > max_evidence_utf8_bytes:
        raise ValueError(
            "focused Structure repair evidence exceeds the explicit byte budget"
        )
    relations = tuple(
        relation
        for relation in current.relations
        if relation.source_structure_node_id in selected_ids
        and relation.target_structure_node_id in selected_ids
    )
    return StructureRepairRequestV1(
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        revision_number=current.revision_number,
        effective_snapshot_id=current.effective_snapshot_id,
        effective_tree_sha256=current.effective_tree_sha256,
        total_effective_node_count=len(current.nodes),
        focus_truncated=focus_truncated,
        total_effective_relation_count=len(current.relations),
        overview_issues=issues,
        document_regions=document_regions,
        nodes=nodes,
        evidence=evidence,
        relations=relations,
    )


def _outcome(
    *,
    status: StructureOptimizationStatus,
    current: EffectiveStructureSnapshotV1,
    overview: StructureOverviewProviderResultV1,
    repair: StructureRepairProviderResultV1 | None = None,
    apply_result: StructureApplyResultV1 | None = None,
) -> StructureOptimizationOutcomeV1:
    effective = current if apply_result is None else apply_result.effective_snapshot
    overview_runs = _overview_provider_runs(overview)
    runs = overview_runs if repair is None else (*overview_runs, repair.run)
    return StructureOptimizationOutcomeV1(
        status=status,
        source_ir_id=current.source_ir_id,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        previous_effective_snapshot_id=current.effective_snapshot_id,
        effective_snapshot=effective,
        overview=overview,
        repair=repair,
        apply_result=apply_result,
        provider_runs=runs,
    )


def _proposal_outcome(
    *,
    status: StructureOptimizationProposalStatus,
    current: EffectiveStructureSnapshotV1,
    overview: StructureOverviewProviderResultV1,
    repair: StructureRepairProviderResultV1 | None = None,
    proposal: StructurePatchProposalV1 | None = None,
) -> StructureOptimizationProposalOutcomeV1:
    overview_runs = _overview_provider_runs(overview)
    runs = overview_runs if repair is None else (*overview_runs, repair.run)
    return StructureOptimizationProposalOutcomeV1(
        status=status,
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        expected_revision_number=current.revision_number,
        expected_effective_snapshot_id=current.effective_snapshot_id,
        expected_effective_tree_sha256=current.effective_tree_sha256,
        expected_revision_chain_sha256=current.revision_chain_sha256,
        expected_head_revision_id=current.head_revision_id,
        overview=overview,
        repair=repair,
        proposal=proposal,
        provider_runs=runs,
    )


def _overview_provider_runs(
    overview: StructureOverviewProviderResultV1,
) -> tuple[SemanticProviderRunRecord, ...]:
    return overview.provider_runs or (overview.run,)


def _require_immutable_inputs_unchanged(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    *,
    source_hash_before: str,
    base_hash_before: str,
) -> None:
    if canonical_model_sha256(source_ir) != source_hash_before:
        raise RuntimeError("Structure optimization mutated immutable SourceIR")
    if canonical_model_sha256(base_tree) != base_hash_before:
        raise RuntimeError("Structure optimization mutated the deterministic base tree")


def _validate_overview_result(
    request: StructureOverviewRequestV1,
    result: StructureOverviewProviderResultV1,
    provider: StructureOptimizerProviderPort,
) -> None:
    if not isinstance(result, StructureOverviewProviderResultV1):
        raise TypeError(
            "overview provider must return StructureOverviewProviderResultV1"
        )
    _require_result_binding(
        source_ir_id=result.source_ir_id,
        effective_snapshot_id=result.effective_snapshot_id,
        request_source_ir_id=request.source_ir_id,
        request_effective_snapshot_id=request.effective_snapshot_id,
    )
    _require_provider_run_binding(result.run, provider, "overview_structure")
    visible_ids = {node.structure_node_id for node in request.nodes}
    inspection_ids = set(request.inspection_structure_node_ids)
    inspected_relation_endpoint_ids = {
        node_id
        for relation in request.relations
        if relation.relation_id in set(request.inspection_structure_relation_ids)
        for node_id in (
            relation.source_structure_node_id,
            relation.target_structure_node_id,
        )
    }
    visible_refs = {
        source_ref for node in request.nodes for source_ref in node.source_refs
    }
    visible_refs.update(
        source_ref
        for relation in request.relations
        for source_ref in relation.source_refs
    )
    visible_order = {
        node.structure_node_id: node.source_order for node in request.nodes
    }
    for region in result.document_regions:
        if (
            region.start_structure_node_id not in visible_ids
            or region.end_structure_node_id not in visible_ids
        ):
            raise ValueError(
                "overview provider region escaped the visible document map"
            )
        if (
            visible_order[region.start_structure_node_id]
            > visible_order[region.end_structure_node_id]
        ):
            raise ValueError("overview provider returned a reversed document region")
        if not set(region.evidence_source_refs).issubset(visible_refs):
            raise ValueError("overview provider region used evidence outside its map")
        if not {
            region.start_structure_node_id,
            region.end_structure_node_id,
        }.intersection(inspection_ids):
            raise ValueError(
                "overview provider region must touch its inspection partition"
            )
    for issue in result.issues:
        target_is_inspected = issue.target_structure_node_id in inspection_ids or (
            issue.category.value == "relation_error"
            and issue.target_structure_node_id in inspected_relation_endpoint_ids
        )
        if not target_is_inspected or not set(
            issue.related_structure_node_ids
        ).issubset(visible_ids):
            raise ValueError("overview provider referenced a node outside its map")
        if not set(issue.evidence_source_refs).issubset(visible_refs):
            raise ValueError("overview provider referenced evidence outside its map")


def _aggregate_overview_results(
    requests: tuple[StructureOverviewRequestV1, ...],
    results: tuple[StructureOverviewProviderResultV1, ...],
) -> StructureOverviewProviderResultV1:
    if not requests or len(requests) != len(results):
        raise ValueError("Structure overview aggregation requires paired partitions")
    source_ir_id = requests[0].source_ir_id
    effective_snapshot_id = requests[0].effective_snapshot_id
    coverage_sha256 = requests[0].overview_coverage_sha256
    relation_coverage_sha256 = requests[0].relation_coverage_sha256
    if any(
        request.source_ir_id != source_ir_id
        or request.effective_snapshot_id != effective_snapshot_id
        or request.overview_coverage_sha256 != coverage_sha256
        or request.relation_coverage_sha256 != relation_coverage_sha256
        for request in requests
    ):
        raise ValueError("Structure overview partitions do not share one identity")
    runs = tuple(result.run for result in results)
    unresolved = next(
        (
            result
            for result in results
            if result.run.status is not SemanticProviderRunStatus.SUCCESS
        ),
        None,
    )
    if unresolved is not None:
        return StructureOverviewProviderResultV1(
            source_ir_id=source_ir_id,
            effective_snapshot_id=effective_snapshot_id,
            document_regions=(),
            issues=(),
            run=unresolved.run,
            provider_runs=runs,
        )

    source_order = {
        node.structure_node_id: node.source_order
        for request in requests
        for node in request.nodes
    }
    issues = _merge_overview_issues(
        tuple(issue for result in results for issue in result.issues)
    )
    if len(issues) > 64:
        raise ValueError("whole-document Structure overview exceeds the issue budget")
    document_regions = _merge_document_regions(
        tuple(region for result in results for region in result.document_regions),
        source_order=source_order,
    )
    return StructureOverviewProviderResultV1(
        source_ir_id=source_ir_id,
        effective_snapshot_id=effective_snapshot_id,
        document_regions=document_regions,
        issues=issues,
        run=results[-1].run,
        provider_runs=runs,
    )


def _merge_overview_issues(
    issues: tuple[StructureOptimizationIssueV1, ...],
) -> tuple[StructureOptimizationIssueV1, ...]:
    grouped: dict[
        tuple[
            object,
            str,
            tuple[str, ...],
        ],
        list[StructureOptimizationIssueV1],
    ] = defaultdict(list)
    for issue in issues:
        grouped[
            (
                issue.category,
                issue.target_structure_node_id,
                issue.related_structure_node_ids,
            )
        ].append(issue)
    merged: list[StructureOptimizationIssueV1] = []
    for _, group in grouped.items():
        first = group[0]
        origins = {item.origin for item in group}
        merged.append(
            StructureOptimizationIssueV1(
                category=first.category,
                target_structure_node_id=first.target_structure_node_id,
                related_structure_node_ids=first.related_structure_node_ids,
                evidence_source_refs=tuple(
                    dict.fromkeys(
                        source_ref
                        for item in group
                        for source_ref in item.evidence_source_refs
                    )
                ),
                origin=(
                    first.origin
                    if len(origins) == 1
                    else StructureOptimizationIssueOrigin.UNCERTAIN
                ),
                confidence=_conservative_confidence(
                    tuple(item.confidence for item in group)
                ),
                reason_codes=tuple(
                    sorted(
                        {
                            reason_code
                            for item in group
                            for reason_code in item.reason_codes
                        }
                    )
                ),
            )
        )
    return tuple(
        sorted(
            merged,
            key=lambda issue: (
                issue.target_structure_node_id,
                issue.category.value,
                issue.related_structure_node_ids,
            ),
        )
    )


def _merge_document_regions(
    regions: tuple[StructureDocumentRegionV1, ...],
    *,
    source_order: dict[str, int],
) -> tuple[StructureDocumentRegionV1, ...]:
    grouped: dict[object, list[StructureDocumentRegionV1]] = defaultdict(list)
    for region in regions:
        grouped[region.kind].append(region)
    merged: list[StructureDocumentRegionV1] = []
    for kind, group in grouped.items():
        start = min(
            group,
            key=lambda item: (
                source_order[item.start_structure_node_id],
                item.start_structure_node_id,
            ),
        )
        end = max(
            group,
            key=lambda item: (
                source_order[item.end_structure_node_id],
                item.end_structure_node_id,
            ),
        )
        merged.append(
            StructureDocumentRegionV1(
                kind=kind,
                start_structure_node_id=start.start_structure_node_id,
                end_structure_node_id=end.end_structure_node_id,
                evidence_source_refs=tuple(
                    dict.fromkeys(
                        source_ref
                        for item in group
                        for source_ref in item.evidence_source_refs
                    )
                ),
                confidence=_conservative_confidence(
                    tuple(item.confidence for item in group)
                ),
                reason_codes=tuple(
                    sorted(
                        {
                            reason_code
                            for item in group
                            for reason_code in item.reason_codes
                        }
                    )
                ),
            )
        )
    return tuple(
        sorted(
            merged,
            key=lambda region: (
                source_order[region.start_structure_node_id],
                region.kind.value,
            ),
        )
    )


def _conservative_confidence(
    values: tuple[StructureOptimizationConfidence, ...],
) -> StructureOptimizationConfidence:
    rank = {
        StructureOptimizationConfidence.LOW: 0,
        StructureOptimizationConfidence.MEDIUM: 1,
        StructureOptimizationConfidence.HIGH: 2,
    }
    return min(values, key=rank.__getitem__)


def _validate_repair_result(
    request: StructureRepairRequestV1,
    result: StructureRepairProviderResultV1,
    provider: StructureOptimizerProviderPort,
) -> None:
    if not isinstance(result, StructureRepairProviderResultV1):
        raise TypeError("repair provider must return StructureRepairProviderResultV1")
    _require_result_binding(
        source_ir_id=result.source_ir_id,
        effective_snapshot_id=result.effective_snapshot_id,
        request_source_ir_id=request.source_ir_id,
        request_effective_snapshot_id=request.effective_snapshot_id,
    )
    _require_provider_run_binding(result.run, provider, "repair_structure")


def _require_result_binding(
    *,
    source_ir_id: str,
    effective_snapshot_id: str,
    request_source_ir_id: str,
    request_effective_snapshot_id: str,
) -> None:
    if (
        source_ir_id != request_source_ir_id
        or effective_snapshot_id != request_effective_snapshot_id
    ):
        raise ValueError(
            "Structure optimizer provider result is not bound to its request"
        )


def _require_provider_run_binding(
    run: SemanticProviderRunRecord,
    provider: StructureOptimizerProviderPort,
    operation: str,
) -> None:
    if (
        run.operation != operation
        or run.provider_id != provider.provider_id
        or run.provider_version != provider.provider_version
        or run.model_id != provider.model_id
    ):
        raise ValueError(
            "Structure optimizer provider receipt does not match the active provider"
        )


def _require_operations_inside_repair_scope(
    operations: tuple[StructurePatchOperationV1, ...],
    request: StructureRepairRequestV1,
) -> None:
    allowed_node_ids = {node.structure_node_id for node in request.nodes}
    allowed_relation_ids = {relation.relation_id for relation in request.relations}
    allowed_source_refs = {item.source_ref for item in request.evidence}
    for operation in operations:
        node_ids, relation_ids, source_refs = _operation_bindings(operation)
        if not node_ids.issubset(allowed_node_ids):
            raise ValueError(
                "Structure optimizer operation escaped its focused node scope"
            )
        if not relation_ids.issubset(allowed_relation_ids):
            raise ValueError(
                "Structure optimizer operation escaped its focused relation scope"
            )
        if not source_refs.issubset(allowed_source_refs):
            raise ValueError(
                "Structure optimizer operation used evidence outside its request"
            )


def _operation_bindings(
    operation: StructurePatchOperationV1,
) -> tuple[set[str], set[str], set[StructureSourceRef]]:
    if isinstance(operation, ReparentStructureOperationV1):
        return (
            {
                operation.structure_node_id,
                operation.new_parent_structure_node_id,
            },
            set(),
            set(),
        )
    if isinstance(operation, LogicalReorderStructureOperationV1):
        return (
            {
                operation.parent_structure_node_id,
                *operation.ordered_child_structure_node_ids,
            },
            set(),
            set(),
        )
    if isinstance(operation, BoundarySplitStructureOperationV1):
        return (
            {operation.structure_node_id},
            set(),
            {span.source_ref for part in operation.parts for span in part.source_spans},
        )
    if isinstance(operation, ContinuationMergeStructureOperationV1):
        return (
            {
                operation.primary_structure_node_id,
                operation.continuation_structure_node_id,
            },
            set(),
            set(),
        )
    if isinstance(operation, AddStructureRelationOperationV1):
        return (
            {
                operation.source_structure_node_id,
                operation.target_structure_node_id,
            },
            set(),
            set(operation.source_refs),
        )
    if isinstance(operation, RemoveStructureRelationOperationV1):
        return set(), {operation.relation_id}, set()
    raise TypeError("unsupported Structure optimizer operation")


def _select_overview_node_ids(
    nodes: tuple[StructureNode, ...],
    *,
    max_nodes: int,
) -> set[str]:
    ordered = tuple(
        sorted(nodes, key=lambda node: (node.source_order, node.structure_node_id))
    )
    primary = tuple(node for node in ordered if node.kind in _OVERVIEW_PRIMARY_KINDS)
    if len(primary) > max_nodes:
        raise ValueError(
            "whole-document Structure outline exceeds the overview node budget"
        )
    selected = {node.structure_node_id for node in primary}
    clauses = tuple(
        node
        for node in ordered
        if node.kind is StructureNodeKind.CLAUSE
        and node.structure_node_id not in selected
    )
    other_nodes = tuple(
        node
        for node in ordered
        if node.structure_node_id not in selected
        and node.kind is not StructureNodeKind.CLAUSE
    )
    remaining = max_nodes - len(selected)
    if clauses and other_nodes and remaining > 1:
        other_quota = max(1, remaining // 4)
        clause_quota = remaining - other_quota
    else:
        clause_quota = remaining if clauses else 0
        other_quota = remaining - clause_quota
    selected_clauses = _sample_evenly(clauses, clause_quota)
    for node in selected_clauses:
        selected.add(node.structure_node_id)
    remaining = max_nodes - len(selected)
    for node in _sample_evenly(other_nodes, min(other_quota, remaining)):
        selected.add(node.structure_node_id)
    remaining = max_nodes - len(selected)
    if remaining:
        unselected = tuple(
            node for node in ordered if node.structure_node_id not in selected
        )
        for node in _sample_evenly(unselected, remaining):
            selected.add(node.structure_node_id)
    return selected


def _ancestor_ids(
    node: StructureNode,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    ancestors: list[str] = []
    seen = {node.structure_node_id}
    cursor = node.parent_structure_node_id
    while cursor is not None:
        if cursor in seen or cursor not in nodes_by_id:
            raise ValueError("effective Structure ancestry is invalid")
        seen.add(cursor)
        ancestors.append(cursor)
        cursor = nodes_by_id[cursor].parent_structure_node_id
    return tuple(ancestors)


def _overview_coverage_sha256(ordered_node_ids: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(ordered_node_ids).encode("utf-8")).hexdigest()


def _sample_evenly(
    nodes: tuple[StructureNode, ...],
    count: int,
) -> tuple[StructureNode, ...]:
    if count <= 0 or not nodes:
        return ()
    if count >= len(nodes):
        return nodes
    if count == 1:
        return (nodes[len(nodes) // 2],)
    indexes = tuple((index * (len(nodes) - 1)) // (count - 1) for index in range(count))
    return tuple(nodes[index] for index in indexes)


def _select_repair_node_ids(
    nodes: tuple[StructureNode, ...],
    relations: tuple[StructureRelation, ...],
    issues: tuple[StructureOptimizationIssueV1, ...],
    document_regions: tuple[StructureDocumentRegionV1, ...],
    *,
    max_nodes: int,
) -> tuple[set[str], bool]:
    nodes_by_id = {node.structure_node_id: node for node in nodes}
    children_by_parent: dict[str | None, list[str]] = defaultdict(list)
    ordered_ids = [
        node.structure_node_id
        for node in sorted(
            nodes,
            key=lambda item: (item.source_order, item.structure_node_id),
        )
    ]
    for node in nodes:
        children_by_parent[node.parent_structure_node_id].append(node.structure_node_id)
    seed_ids = {
        node_id
        for issue in issues
        for node_id in (
            issue.target_structure_node_id,
            *issue.related_structure_node_ids,
        )
    }
    region_context_ids = {
        node_id
        for region in document_regions
        for node_id in (
            region.start_structure_node_id,
            region.end_structure_node_id,
        )
    }
    region_evidence_refs = {
        source_ref
        for region in document_regions
        for source_ref in region.evidence_source_refs
    }
    region_context_ids.update(
        node.structure_node_id
        for node in nodes
        if set(node.source_refs).intersection(region_evidence_refs)
    )
    if not seed_ids or not seed_ids.issubset(nodes_by_id):
        raise ValueError("optimizer issues must reference current effective nodes")
    if not region_context_ids.issubset(nodes_by_id):
        raise ValueError("optimizer document regions must reference current nodes")
    selected = set(seed_ids | region_context_ids)

    for seed_id in tuple(seed_ids | region_context_ids):
        cursor = nodes_by_id[seed_id].parent_structure_node_id
        while cursor is not None:
            selected.add(cursor)
            cursor = nodes_by_id[cursor].parent_structure_node_id

    focus_truncated = False
    for seed_id in tuple(seed_ids):
        parent_id = nodes_by_id[seed_id].parent_structure_node_id
        sibling_group = set(children_by_parent[parent_id])
        if len(selected | sibling_group) <= max_nodes:
            selected.update(sibling_group)
        else:
            focus_truncated = True

    queue: deque[str] = deque(
        child_id
        for seed_id in sorted(seed_ids)
        for child_id in children_by_parent[seed_id]
    )
    while queue and len(selected) < max_nodes:
        node_id = queue.popleft()
        if node_id in selected:
            continue
        selected.add(node_id)
        queue.extend(children_by_parent[node_id])
    if queue:
        focus_truncated = True

    positions = {node_id: index for index, node_id in enumerate(ordered_ids)}
    for seed_id in tuple(seed_ids):
        position = positions[seed_id]
        for neighbor_position in range(
            max(0, position - 2),
            min(len(ordered_ids), position + 3),
        ):
            if len(selected) >= max_nodes:
                if ordered_ids[neighbor_position] not in selected:
                    focus_truncated = True
                break
            selected.add(ordered_ids[neighbor_position])

    for relation in relations:
        endpoints = {
            relation.source_structure_node_id,
            relation.target_structure_node_id,
        }
        if endpoints.intersection(selected) and len(selected | endpoints) <= max_nodes:
            selected.update(endpoints)
        elif endpoints.intersection(selected) and not endpoints.issubset(selected):
            focus_truncated = True
    if len(selected) > max_nodes:
        raise ValueError("focused Structure repair exceeds the node budget")
    return selected, focus_truncated


def _map_nodes(
    nodes: tuple[StructureNode, ...],
    *,
    selected_ids: set[str],
    catalog: dict[StructureSourceRef, tuple[str, tuple[int, ...]]],
    source_spans_by_node_id: dict[str, tuple[StructureSourceSpanV1, ...]],
    preview_chars: int,
) -> tuple[StructureOptimizerMapNodeV1, ...]:
    nodes_by_id = {node.structure_node_id: node for node in nodes}
    children_by_parent: dict[str | None, list[StructureNode]] = defaultdict(list)
    for node in nodes:
        children_by_parent[node.parent_structure_node_id].append(node)
    result: list[StructureOptimizerMapNodeV1] = []
    for node in sorted(
        nodes, key=lambda item: (item.source_order, item.structure_node_id)
    ):
        if node.structure_node_id not in selected_ids:
            continue
        parent_id = node.parent_structure_node_id
        visible_parent_id = parent_id
        while visible_parent_id is not None and visible_parent_id not in selected_ids:
            visible_parent_id = nodes_by_id[visible_parent_id].parent_structure_node_id
        source_spans = source_spans_by_node_id[node.structure_node_id]
        texts = tuple(
            catalog[span.source_ref][0][span.start_offset : span.end_offset]
            for span in source_spans
            if span.source_ref in catalog
            and catalog[span.source_ref][0][span.start_offset : span.end_offset]
        )
        page_numbers = tuple(
            sorted(
                {
                    page_number
                    for source_ref in node.source_refs
                    if source_ref in catalog
                    for page_number in catalog[source_ref][1]
                }
            )
        )
        children = children_by_parent[node.structure_node_id]
        result.append(
            StructureOptimizerMapNodeV1(
                structure_node_id=node.structure_node_id,
                parent_structure_node_id=parent_id,
                nearest_visible_parent_structure_node_id=visible_parent_id,
                kind=node.kind,
                source_order=node.source_order,
                boundary_basis=node.boundary_basis,
                explicit_label=node.explicit_label,
                list_ordinal=node.list_ordinal,
                source_refs=node.source_refs,
                source_spans=source_spans,
                page_numbers=page_numbers,
                text_preview=_bounded_preview(texts, preview_chars),
                direct_child_count=len(children),
                omitted_direct_child_count=sum(
                    child.structure_node_id not in selected_ids for child in children
                ),
            )
        )
    return tuple(result)


def _source_spans_by_node_id(
    current: EffectiveStructureSnapshotV1,
) -> dict[str, tuple[StructureSourceSpanV1, ...]]:
    return {
        binding.structure_node_id: binding.source_spans
        for binding in current.node_source_spans
    }


def _bounded_preview(texts: tuple[str, ...], limit: int) -> str:
    combined = "\n".join(text for text in texts if text)
    if len(combined) <= limit:
        return combined
    head_length = max(1, (limit * 2) // 3)
    tail_length = max(1, limit - head_length - 3)
    return f"{combined[:head_length]}...{combined[-tail_length:]}"


def _source_catalog(
    source_ir: SourceIRSnapshot,
) -> dict[StructureSourceRef, tuple[str, tuple[int, ...]]]:
    catalog: dict[StructureSourceRef, tuple[str, tuple[int, ...]]] = {}
    for node in source_ir.nodes:
        node_pages = _locator_pages(node.native_locator.page_number)
        catalog[
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=node.source_node_id,
            )
        ] = (node.raw_text, node_pages)
        if node.table_payload is not None:
            for cell in node.table_payload.cells:
                catalog[
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=cell.cell_id,
                    )
                ] = (
                    cell.raw_text,
                    _locator_pages(cell.native_locator.page_number),
                )
    for visual_snapshot in source_ir.visual_text_snapshots:
        for observation in visual_snapshot.observations:
            catalog[
                StructureSourceRef(
                    kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                    source_id=observation.observation_id,
                )
            ] = (observation.raw_text, (observation.page_number,))
    return catalog


def _locator_pages(page_number: int | None) -> tuple[int, ...]:
    return () if page_number is None else (page_number,)


def _require_positive_limit(value: int, name: str, *, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > maximum
    ):
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


__all__ = [
    "DEFAULT_STRUCTURE_OVERVIEW_MAX_NODES",
    "DEFAULT_STRUCTURE_OVERVIEW_PREVIEW_CHARS",
    "DEFAULT_STRUCTURE_REPAIR_MAX_EVIDENCE_UTF8_BYTES",
    "DEFAULT_STRUCTURE_REPAIR_MAX_NODES",
    "STRUCTURE_OPTIMIZATION_OUTCOME_SCHEMA_VERSION",
    "STRUCTURE_OPTIMIZATION_PROPOSAL_OUTCOME_SCHEMA_VERSION",
    "StructureOptimizationOutcomeV1",
    "StructureOptimizationProposalOutcomeV1",
    "StructureOptimizationProposalStatus",
    "StructureOptimizationStatus",
    "apply_structure_optimization_proposal",
    "optimize_structure_with_provider",
    "plan_structure_overview_request",
    "plan_structure_overview_requests",
    "plan_structure_repair_request",
    "propose_structure_optimization_with_provider",
]
