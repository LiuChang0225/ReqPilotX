from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.adapters.documents.bailian_semantic_provider import (
    BailianQwenSemanticProvider,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.structure import (
    StructureOptionState,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
)
from app.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitPartV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructurePatchOperationV1,
    StructureSourceSpanV1,
)
from app.ports.structure_optimizer_provider import (
    StructureDocumentRegionKind,
    StructureDocumentRegionV1,
    StructureOptimizationConfidence,
    StructureOptimizationIssueCategory,
    StructureOptimizationIssueOrigin,
    StructureOptimizationIssueV1,
    StructureOverviewProviderResultV1,
    StructureOverviewRequestV1,
    StructureRepairProviderResultV1,
    StructureRepairRequestV1,
)


STRUCTURE_OVERVIEW_PROMPT_VERSION = "reqpilotx-structure-overview/v3"
STRUCTURE_OVERVIEW_RESPONSE_SCHEMA = "qwen-structure-overview/v3"
STRUCTURE_REPAIR_PROMPT_VERSION = "reqpilotx-structure-repair/v2"
STRUCTURE_REPAIR_RESPONSE_SCHEMA = "qwen-structure-repair/v2"

_NODE_ALIAS_PATTERN = r"^n[0-9]+$"
_EVIDENCE_ALIAS_PATTERN = r"^e[0-9]+$"
_RELATION_ALIAS_PATTERN = r"^r[0-9]+$"
_MODEL_OUTPUT_BINDING_ERROR = "provider_output_binding_invalid"
_NodeAlias = Annotated[str, Field(pattern=_NODE_ALIAS_PATTERN)]
_EvidenceAlias = Annotated[str, Field(pattern=_EVIDENCE_ALIAS_PATTERN)]
_RelationAlias = Annotated[str, Field(pattern=_RELATION_ALIAS_PATTERN)]


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ModelDocumentRegion(_StrictPayload):
    kind: StructureDocumentRegionKind
    start_node: _NodeAlias
    end_node: _NodeAlias
    evidence: tuple[_EvidenceAlias, ...] = Field(min_length=1)
    confidence: StructureOptimizationConfidence
    reason_codes: tuple[str, ...] = Field(min_length=1)


class _ModelStructureIssue(_StrictPayload):
    category: StructureOptimizationIssueCategory
    target_node: _NodeAlias
    related_nodes: tuple[_NodeAlias, ...] = ()
    evidence: tuple[_EvidenceAlias, ...] = Field(min_length=1)
    origin: StructureOptimizationIssueOrigin
    confidence: StructureOptimizationConfidence
    reason_codes: tuple[str, ...] = Field(min_length=1)


class _StructureOverviewPayload(_StrictPayload):
    document_regions: tuple[_ModelDocumentRegion, ...] = Field(
        default=(),
        max_length=16,
    )
    issues: tuple[_ModelStructureIssue, ...] = Field(
        default=(),
        max_length=64,
    )


class _ModelReparentOperation(_StrictPayload):
    operation: Literal["re_parent"] = "re_parent"
    node: _NodeAlias
    new_parent: _NodeAlias
    new_sibling_index: StrictInt = Field(ge=0)


class _ModelLogicalReorderOperation(_StrictPayload):
    operation: Literal["logical_reorder"] = "logical_reorder"
    parent: _NodeAlias
    ordered_children: tuple[_NodeAlias, ...] = Field(min_length=1)


class _ModelSourceSpan(_StrictPayload):
    evidence: _EvidenceAlias
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(ge=1)


class _ModelBoundarySplitPart(_StrictPayload):
    source_spans: tuple[_ModelSourceSpan, ...] = Field(min_length=1)


class _ModelBoundarySplitOperation(_StrictPayload):
    operation: Literal["boundary_split"] = "boundary_split"
    node: _NodeAlias
    parts: tuple[_ModelBoundarySplitPart, ...] = Field(min_length=2, max_length=64)


class _ModelContinuationMergeOperation(_StrictPayload):
    operation: Literal["continuation_merge"] = "continuation_merge"
    primary: _NodeAlias
    continuation: _NodeAlias


class _ModelAddRelationOperation(_StrictPayload):
    operation: Literal["add_relation"] = "add_relation"
    kind: StructureRelationKind
    source: _NodeAlias
    target: _NodeAlias
    evidence: tuple[_EvidenceAlias, ...] = ()
    option_state: StructureOptionState | None = None


class _ModelRemoveRelationOperation(_StrictPayload):
    operation: Literal["remove_relation"] = "remove_relation"
    relation: _RelationAlias


_ModelPatchOperation = Annotated[
    _ModelReparentOperation
    | _ModelLogicalReorderOperation
    | _ModelBoundarySplitOperation
    | _ModelContinuationMergeOperation
    | _ModelAddRelationOperation
    | _ModelRemoveRelationOperation,
    Field(discriminator="operation"),
]


class _StructureRepairPayload(_StrictPayload):
    operations: tuple[_ModelPatchOperation, ...] = Field(
        default=(),
        max_length=64,
    )


@dataclass(frozen=True, slots=True)
class _AliasCatalog:
    node_id_by_alias: dict[str, str]
    node_alias_by_id: dict[str, str]
    evidence_ref_by_alias: dict[str, StructureSourceRef]
    evidence_alias_by_ref: dict[StructureSourceRef, str]
    relation_by_alias: dict[str, StructureRelation]
    relation_alias_by_id: dict[str, str]


class BailianQwenStructureOptimizerProvider(BailianQwenSemanticProvider):
    """Two-phase Structure optimizer using compact aliases on the model wire."""

    overview_prompt_version = STRUCTURE_OVERVIEW_PROMPT_VERSION
    overview_response_schema_version = STRUCTURE_OVERVIEW_RESPONSE_SCHEMA
    repair_prompt_version = STRUCTURE_REPAIR_PROMPT_VERSION
    repair_response_schema_version = STRUCTURE_REPAIR_RESPONSE_SCHEMA

    def overview_structure(
        self,
        request: StructureOverviewRequestV1,
    ) -> StructureOverviewProviderResultV1:
        if not isinstance(request, StructureOverviewRequestV1):
            raise TypeError("request must be a StructureOverviewRequestV1")
        user_payload, aliases = _structure_overview_model_request(request)
        invocation = self._invoke_with_invalid_output_retry(
            operation="overview_structure",
            prompt_version=STRUCTURE_OVERVIEW_PROMPT_VERSION,
            response_schema_version=STRUCTURE_OVERVIEW_RESPONSE_SCHEMA,
            system_prompt=_structure_overview_system_prompt(),
            user_payload=user_payload,
            images=(),
            payload_type=_StructureOverviewPayload,
        )
        if not isinstance(invocation.payload, _StructureOverviewPayload):
            return _empty_overview_result(request, invocation.run)
        try:
            document_regions = tuple(
                _document_region_from_model(item, aliases)
                for item in invocation.payload.document_regions
            )
            issues = tuple(
                _structure_issue_from_model(item, aliases)
                for item in invocation.payload.issues
            )
        except (KeyError, TypeError, ValueError):
            return _empty_overview_result(
                request,
                _mark_output_binding_invalid(invocation.run),
            )
        return StructureOverviewProviderResultV1(
            source_ir_id=request.source_ir_id,
            effective_snapshot_id=request.effective_snapshot_id,
            document_regions=document_regions,
            issues=issues,
            run=invocation.run,
        )

    def repair_structure(
        self,
        request: StructureRepairRequestV1,
    ) -> StructureRepairProviderResultV1:
        if not isinstance(request, StructureRepairRequestV1):
            raise TypeError("request must be a StructureRepairRequestV1")
        user_payload, aliases = _structure_repair_model_request(request)
        invocation = self._invoke_with_invalid_output_retry(
            operation="repair_structure",
            prompt_version=STRUCTURE_REPAIR_PROMPT_VERSION,
            response_schema_version=STRUCTURE_REPAIR_RESPONSE_SCHEMA,
            system_prompt=_structure_repair_system_prompt(),
            user_payload=user_payload,
            images=(),
            payload_type=_StructureRepairPayload,
        )
        if not isinstance(invocation.payload, _StructureRepairPayload):
            return _empty_repair_result(request, invocation.run)
        try:
            operations = tuple(
                _patch_operation_from_model(operation, aliases)
                for operation in invocation.payload.operations
            )
        except (KeyError, TypeError, ValueError):
            return _empty_repair_result(
                request,
                _mark_output_binding_invalid(invocation.run),
            )
        return StructureRepairProviderResultV1(
            source_ir_id=request.source_ir_id,
            effective_snapshot_id=request.effective_snapshot_id,
            operations=operations,
            run=invocation.run,
        )


def _structure_overview_model_request(
    request: StructureOverviewRequestV1,
) -> tuple[dict[str, object], _AliasCatalog]:
    aliases = _alias_catalog(
        node_ids=tuple(node.structure_node_id for node in request.nodes),
        evidence_refs=_ordered_evidence_refs(
            tuple(
                source_ref
                for node in request.nodes
                for source_ref in (
                    *node.source_refs,
                    *(span.source_ref for span in node.source_spans),
                )
            ),
            tuple(
                source_ref
                for relation in request.relations
                if relation.kind is not StructureRelationKind.CONTAINS
                for source_ref in relation.source_refs
            ),
        ),
        relations=tuple(
            relation
            for relation in request.relations
            if relation.kind is not StructureRelationKind.CONTAINS
        ),
    )
    return (
        {
            "task": "inspect_current_document_structure",
            "binding": {
                "source_sha256": request.source_sha256,
                "revision_number": request.revision_number,
                "effective_tree_sha256": request.effective_tree_sha256,
                "total_effective_node_count": request.total_effective_node_count,
                "map_truncated": request.map_truncated,
                "partition_index": request.partition_index,
                "partition_count": request.partition_count,
                "overview_coverage_sha256": request.overview_coverage_sha256,
                "relation_coverage_sha256": request.relation_coverage_sha256,
                "inspection_node_count": len(
                    request.inspection_structure_node_ids
                ),
                "inspection_relation_count": len(
                    request.inspection_structure_relation_ids
                ),
                "total_effective_relation_count": (
                    request.total_effective_relation_count
                ),
                "relation_map_truncated": request.relation_map_truncated,
                "contains_relations_encoded_by_parent_columns": True,
            },
            "inspection_nodes": tuple(
                _node_alias(aliases, node_id)
                for node_id in request.inspection_structure_node_ids
            ),
            "inspection_relations": tuple(
                aliases.relation_alias_by_id[relation_id]
                for relation_id in request.inspection_structure_relation_ids
            ),
            "node_columns": _NODE_COLUMNS,
            "nodes": _node_rows(request.nodes, aliases),
            "relation_columns": _RELATION_COLUMNS,
            "non_parent_relations": _relation_rows(aliases),
            "required_output": {
                "document_regions": (
                    "copy node/evidence aliases for grounded document ranges"
                ),
                "issues": "copy node/evidence aliases for structural anomalies",
            },
        },
        aliases,
    )


def _structure_repair_model_request(
    request: StructureRepairRequestV1,
) -> tuple[dict[str, object], _AliasCatalog]:
    aliases = _alias_catalog(
        node_ids=tuple(node.structure_node_id for node in request.nodes),
        evidence_refs=tuple(item.source_ref for item in request.evidence),
        relations=tuple(
            relation
            for relation in request.relations
            if relation.kind is not StructureRelationKind.CONTAINS
        ),
    )
    return (
        {
            "task": "directly_repair_current_document_structure",
            "binding": {
                "source_sha256": request.source_sha256,
                "revision_number": request.revision_number,
                "effective_tree_sha256": request.effective_tree_sha256,
                "total_effective_node_count": request.total_effective_node_count,
                "focus_truncated": request.focus_truncated,
                "total_effective_relation_count": (
                    request.total_effective_relation_count
                ),
                "contains_relations_encoded_by_parent_columns": True,
            },
            "overview_issues": tuple(
                _structure_issue_to_model(item, aliases)
                for item in request.overview_issues
            ),
            "document_regions": tuple(
                _document_region_to_model(item, aliases)
                for item in request.document_regions
            ),
            "node_columns": _NODE_COLUMNS,
            "nodes": _node_rows(request.nodes, aliases),
            "evidence_columns": _EVIDENCE_COLUMNS,
            "evidence": tuple(
                (
                    _evidence_alias(aliases, item.source_ref),
                    item.raw_text,
                    item.page_numbers,
                )
                for item in request.evidence
            ),
            "relation_columns": _RELATION_COLUMNS,
            "non_parent_relations": _relation_rows(aliases),
            "required_output": {
                "operations": (
                    "one ordered patch list using only supplied node, evidence "
                    "and relation aliases, or an empty list"
                )
            },
        },
        aliases,
    )


_NODE_COLUMNS = (
    "node",
    "parent",
    "nearest_visible_parent",
    "kind",
    "source_order",
    "boundary_basis",
    "explicit_label",
    "list_ordinal",
    "page_numbers",
    "text_preview",
    "direct_child_count",
    "omitted_direct_child_count",
    "evidence",
    "source_spans",
)
_EVIDENCE_COLUMNS = ("evidence", "raw_text", "page_numbers")
_RELATION_COLUMNS = (
    "relation",
    "kind",
    "source_node",
    "target_node",
    "evidence",
    "option_state",
)


def _node_rows(
    nodes: tuple[object, ...],
    aliases: _AliasCatalog,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            _node_alias(aliases, node.structure_node_id),
            aliases.node_alias_by_id.get(node.parent_structure_node_id),
            aliases.node_alias_by_id.get(node.nearest_visible_parent_structure_node_id),
            node.kind.value,
            node.source_order,
            node.boundary_basis.value,
            node.explicit_label,
            node.list_ordinal,
            node.page_numbers,
            node.text_preview,
            node.direct_child_count,
            node.omitted_direct_child_count,
            tuple(
                _evidence_alias(aliases, source_ref) for source_ref in node.source_refs
            ),
            tuple(
                (
                    _evidence_alias(aliases, span.source_ref),
                    span.start_offset,
                    span.end_offset,
                )
                for span in node.source_spans
            ),
        )
        for node in nodes
    )


def _relation_rows(
    aliases: _AliasCatalog,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            relation_alias,
            relation.kind.value,
            _node_alias(aliases, relation.source_structure_node_id),
            _node_alias(aliases, relation.target_structure_node_id),
            tuple(
                _evidence_alias(aliases, source_ref)
                for source_ref in relation.source_refs
            ),
            relation.option_state.value if relation.option_state is not None else None,
        )
        for relation_alias, relation in aliases.relation_by_alias.items()
    )


def _structure_issue_to_model(
    issue: StructureOptimizationIssueV1,
    aliases: _AliasCatalog,
) -> dict[str, object]:
    return {
        "category": issue.category.value,
        "target_node": _node_alias(aliases, issue.target_structure_node_id),
        "related_nodes": tuple(
            _node_alias(aliases, node_id)
            for node_id in issue.related_structure_node_ids
        ),
        "evidence": tuple(
            _evidence_alias(aliases, source_ref)
            for source_ref in issue.evidence_source_refs
        ),
        "origin": issue.origin.value,
        "confidence": issue.confidence.value,
        "reason_codes": issue.reason_codes,
    }


def _document_region_to_model(
    region: StructureDocumentRegionV1,
    aliases: _AliasCatalog,
) -> dict[str, object]:
    return {
        "kind": region.kind.value,
        "start_node": _node_alias(aliases, region.start_structure_node_id),
        "end_node": _node_alias(aliases, region.end_structure_node_id),
        "evidence": tuple(
            _evidence_alias(aliases, source_ref)
            for source_ref in region.evidence_source_refs
        ),
        "confidence": region.confidence.value,
        "reason_codes": region.reason_codes,
    }


def _structure_issue_from_model(
    issue: _ModelStructureIssue,
    aliases: _AliasCatalog,
) -> StructureOptimizationIssueV1:
    return StructureOptimizationIssueV1(
        category=issue.category,
        target_structure_node_id=_node_id(aliases, issue.target_node),
        related_structure_node_ids=tuple(
            _node_id(aliases, node_alias) for node_alias in issue.related_nodes
        ),
        evidence_source_refs=tuple(
            _evidence_ref(aliases, evidence_alias) for evidence_alias in issue.evidence
        ),
        origin=issue.origin,
        confidence=issue.confidence,
        reason_codes=issue.reason_codes,
    )


def _document_region_from_model(
    region: _ModelDocumentRegion,
    aliases: _AliasCatalog,
) -> StructureDocumentRegionV1:
    return StructureDocumentRegionV1(
        kind=region.kind,
        start_structure_node_id=_node_id(aliases, region.start_node),
        end_structure_node_id=_node_id(aliases, region.end_node),
        evidence_source_refs=tuple(
            _evidence_ref(aliases, evidence_alias) for evidence_alias in region.evidence
        ),
        confidence=region.confidence,
        reason_codes=region.reason_codes,
    )


def _patch_operation_from_model(
    operation: _ModelPatchOperation,
    aliases: _AliasCatalog,
) -> StructurePatchOperationV1:
    if isinstance(operation, _ModelReparentOperation):
        return ReparentStructureOperationV1(
            structure_node_id=_node_id(aliases, operation.node),
            new_parent_structure_node_id=_node_id(aliases, operation.new_parent),
            new_sibling_index=operation.new_sibling_index,
        )
    if isinstance(operation, _ModelLogicalReorderOperation):
        return LogicalReorderStructureOperationV1(
            parent_structure_node_id=_node_id(aliases, operation.parent),
            ordered_child_structure_node_ids=tuple(
                _node_id(aliases, node_alias)
                for node_alias in operation.ordered_children
            ),
        )
    if isinstance(operation, _ModelBoundarySplitOperation):
        return BoundarySplitStructureOperationV1(
            structure_node_id=_node_id(aliases, operation.node),
            parts=tuple(
                BoundarySplitPartV1(
                    source_spans=tuple(
                        StructureSourceSpanV1(
                            source_ref=_evidence_ref(aliases, span.evidence),
                            start_offset=span.start_offset,
                            end_offset=span.end_offset,
                        )
                        for span in part.source_spans
                    )
                )
                for part in operation.parts
            ),
        )
    if isinstance(operation, _ModelContinuationMergeOperation):
        return ContinuationMergeStructureOperationV1(
            primary_structure_node_id=_node_id(aliases, operation.primary),
            continuation_structure_node_id=_node_id(
                aliases,
                operation.continuation,
            ),
        )
    if isinstance(operation, _ModelAddRelationOperation):
        return AddStructureRelationOperationV1(
            kind=operation.kind,
            source_structure_node_id=_node_id(aliases, operation.source),
            target_structure_node_id=_node_id(aliases, operation.target),
            source_refs=tuple(
                _evidence_ref(aliases, evidence_alias)
                for evidence_alias in operation.evidence
            ),
            option_state=operation.option_state,
        )
    if isinstance(operation, _ModelRemoveRelationOperation):
        return RemoveStructureRelationOperationV1(
            relation_id=_relation(aliases, operation.relation).relation_id,
        )
    raise TypeError("unsupported compact Structure patch operation")


def _alias_catalog(
    *,
    node_ids: tuple[str, ...],
    evidence_refs: tuple[StructureSourceRef, ...],
    relations: tuple[StructureRelation, ...],
) -> _AliasCatalog:
    node_alias_by_id = {node_id: f"n{index}" for index, node_id in enumerate(node_ids)}
    evidence_alias_by_ref = {
        source_ref: f"e{index}" for index, source_ref in enumerate(evidence_refs)
    }
    relation_alias_by_id = {
        relation.relation_id: f"r{index}" for index, relation in enumerate(relations)
    }
    return _AliasCatalog(
        node_id_by_alias={
            alias: node_id for node_id, alias in node_alias_by_id.items()
        },
        node_alias_by_id=node_alias_by_id,
        evidence_ref_by_alias={
            alias: source_ref for source_ref, alias in evidence_alias_by_ref.items()
        },
        evidence_alias_by_ref=evidence_alias_by_ref,
        relation_by_alias={
            alias: relation
            for relation, alias in (
                (relation, relation_alias_by_id[relation.relation_id])
                for relation in relations
            )
        },
        relation_alias_by_id=relation_alias_by_id,
    )


def _ordered_evidence_refs(
    *groups: tuple[StructureSourceRef, ...],
) -> tuple[StructureSourceRef, ...]:
    ordered: list[StructureSourceRef] = []
    seen: set[StructureSourceRef] = set()
    for group in groups:
        for source_ref in group:
            if source_ref in seen:
                continue
            seen.add(source_ref)
            ordered.append(source_ref)
    return tuple(ordered)


def _node_alias(aliases: _AliasCatalog, node_id: str) -> str:
    try:
        return aliases.node_alias_by_id[node_id]
    except KeyError as error:
        raise ValueError(
            "Structure node is outside the compact alias catalog"
        ) from error


def _node_id(aliases: _AliasCatalog, node_alias: str) -> str:
    try:
        return aliases.node_id_by_alias[node_alias]
    except KeyError as error:
        raise ValueError("model returned an unknown Structure node alias") from error


def _evidence_alias(
    aliases: _AliasCatalog,
    source_ref: StructureSourceRef,
) -> str:
    try:
        return aliases.evidence_alias_by_ref[source_ref]
    except KeyError as error:
        raise ValueError(
            "source evidence is outside the compact alias catalog"
        ) from error


def _evidence_ref(
    aliases: _AliasCatalog,
    evidence_alias: str,
) -> StructureSourceRef:
    try:
        return aliases.evidence_ref_by_alias[evidence_alias]
    except KeyError as error:
        raise ValueError("model returned an unknown source evidence alias") from error


def _relation(
    aliases: _AliasCatalog,
    relation_alias: str,
) -> StructureRelation:
    try:
        return aliases.relation_by_alias[relation_alias]
    except KeyError as error:
        raise ValueError(
            "model returned an unknown Structure relation alias"
        ) from error


def _empty_overview_result(
    request: StructureOverviewRequestV1,
    run: SemanticProviderRunRecord,
) -> StructureOverviewProviderResultV1:
    return StructureOverviewProviderResultV1(
        source_ir_id=request.source_ir_id,
        effective_snapshot_id=request.effective_snapshot_id,
        document_regions=(),
        issues=(),
        run=run,
    )


def _empty_repair_result(
    request: StructureRepairRequestV1,
    run: SemanticProviderRunRecord,
) -> StructureRepairProviderResultV1:
    return StructureRepairProviderResultV1(
        source_ir_id=request.source_ir_id,
        effective_snapshot_id=request.effective_snapshot_id,
        operations=(),
        run=run,
    )


def _mark_output_binding_invalid(
    run: SemanticProviderRunRecord,
) -> SemanticProviderRunRecord:
    if run.status is not SemanticProviderRunStatus.SUCCESS:
        return run
    status = SemanticProviderRunStatus.INVALID_OUTPUT
    return run.model_copy(
        update={
            "provider_run_id": stable_semantic_provider_run_id(
                operation=run.operation,
                provider_id=run.provider_id,
                model_id=run.model_id,
                request_sha256=run.request_sha256,
                response_sha256=run.response_sha256,
                status=status,
                started_at=run.started_at,
            ),
            "status": status,
            "error_code": _MODEL_OUTPUT_BINDING_ERROR,
        }
    )


def _structure_overview_system_prompt() -> str:
    return """
You are ReqPilotX's whole-document Structure Overview engine. Overview is one
phase, but a large effective tree is divided into deterministic bounded
partitions and the server aggregates every partition before repair.

Inspect the supplied current effective outline as a document, not as isolated
sentences. Nodes and non-parent relations are compact row arrays whose columns
are declared in the request. Short n/e/r aliases are request-local handles;
copy them exactly in the output. Parent/contains edges are represented by the
node parent columns and intentionally omitted from non_parent_relations.

inspection_nodes are the nodes exclusively assigned to this partition. Inspect
every inspection node. Other supplied nodes are read-only ancestor or boundary
context: use them to understand hierarchy, but never make a context-only node
the target of a non-relation issue. inspection_relations are non-parent
relations exclusively assigned to this partition. A relation_error may target
one of that relation's visible endpoints. A document region must touch at least
one inspection node; return no regions in a relation-only partition. Do not
make conclusions about nodes or relations assigned to another partition.

Determine the likely TOC, main-body and appendix regions; inspect hierarchy
depth, numbering continuity, list/table placement, source order and cross-page
continuation. Decide whether each anomaly is more likely present in the source
document, introduced by the deterministic base resolver, or uncertain.

Return only the required JSON. Every region endpoint, target and related node
must use a supplied n alias. Every evidence item must use a supplied e alias.
Use sorted, unique, short machine-readable reason_codes. Do not invent text,
aliases, headings or patch operations. This overview locates suspicious scopes;
the next call performs repair. Return an empty issues list when the current
partition has no source-grounded anomaly. If map_truncated or
relation_map_truncated is true, omitted nodes and relations are handled by
other partitions or are outside this local relation context; never claim they
were checked in this call.
""".strip()


def _structure_repair_system_prompt() -> str:
    return """
You are ReqPilotX's focused Structure Repair engine. Directly return one
aggregate ordered operation list for the supplied current revision.

Nodes, evidence and non-parent relations use request-local n/e/r aliases. Their
row columns are declared in the request. Parent/contains edges are encoded by
node parent columns. Copy only supplied aliases; the server maps them back to
immutable identities and validates the complete patch.

Use only these operations: re_parent, logical_reorder, boundary_split,
continuation_merge, add_relation and remove_relation. Never rewrite source
text, create a heading, create an arbitrary node, change a node kind, or repair
an anomaly that lacks exact source evidence.

If focus_truncated is true, never infer that an omitted descendant, sibling or
relation does not exist. Emit only operations whose complete required scope is
visible; otherwise return an empty operations list.

For re_parent, preserve the target and use a visible valid parent. For
logical_reorder, provide the complete current child set of one parent exactly
once. For boundary_split, use Unicode half-open offsets over the exact supplied
raw_text and the target node's current source spans; parts must be ordered,
non-overlapping and exactly cover those current spans. For continuation_merge,
use two visible nodes with genuine continuation evidence. Add a relation only
with visible endpoints and grounded evidence aliases. Remove only a supplied
non-parent relation alias.

Return only the required JSON. Return an empty operations list when no safe
direct repair is supported. The server independently validates the entire
batch and rejects it atomically if any operation is invalid.
""".strip()


__all__ = [
    "BailianQwenStructureOptimizerProvider",
    "STRUCTURE_OVERVIEW_PROMPT_VERSION",
    "STRUCTURE_OVERVIEW_RESPONSE_SCHEMA",
    "STRUCTURE_REPAIR_PROMPT_VERSION",
    "STRUCTURE_REPAIR_RESPONSE_SCHEMA",
]
