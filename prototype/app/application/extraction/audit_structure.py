from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.extraction.provider_source_policy import (
    require_complete_source_ir_for_provider,
)
from app.application.extraction.llm_semantics import (
    plan_content_classification_grounding_request,
)
from app.application.extraction.llm_provision_batch import (
    llm_document_remote_chunk_lease,
)
from app.application.extraction.structure_snapshot import (
    StructureSnapshot,
    require_structure_snapshot,
    structure_snapshot_id,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import (
    StructureNode,
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructurePatchOperationV1,
)
from app.ports.semantic_provider import (
    SemanticClassificationRequest,
    SemanticNodeInput,
)
from app.ports.structure_audit_provider import (
    StructureAuditCategory,
    StructureAuditContinuationInput,
    StructureAuditDecision,
    StructureAuditPhysicalOrderInput,
    StructureAuditProposalEvidence,
    StructureAuditProviderPort,
    StructureAuditProviderResult,
    StructureAuditRequest,
)


STRUCTURE_AUDIT_RESULT_SCHEMA_VERSION = "structure-audit-result/v2"
DEFAULT_STRUCTURE_AUDIT_MAX_NODES_PER_REQUEST = 32
_AUDITABLE_STRUCTURE_NODE_KINDS = frozenset(
    {
        StructureNodeKind.SECTION,
        StructureNodeKind.CLAUSE,
        StructureNodeKind.CONTENT,
        StructureNodeKind.TABLE,
        StructureNodeKind.LIST_GROUP,
        StructureNodeKind.GROUP,
    }
)
_FINDING_ID_PATTERN = re.compile(r"^structure-audit-finding-[0-9a-f]{32}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureAuditStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"


class StructureAuditFinding(_StrictFrozenModel):
    """A source-bound suspicion; never a replacement StructureTree fact."""

    finding_id: str = Field(min_length=1, max_length=160)
    category: StructureAuditCategory
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    related_structure_node_ids: tuple[str, ...] = ()
    evidence_source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    provider_run_id: str = Field(min_length=1, max_length=160)
    proposed_operations: tuple[StructurePatchOperationV1, ...] = Field(
        default=(),
        max_length=1,
    )
    proposal_evidence: StructureAuditProposalEvidence | None = None

    @field_validator("finding_id")
    @classmethod
    def validate_finding_id(cls, value: str) -> str:
        if not _FINDING_ID_PATTERN.fullmatch(value):
            raise ValueError("audit finding ID must use a stable identity")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("audit reason codes must use machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("audit reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_finding_shape(self) -> StructureAuditFinding:
        if len(self.related_structure_node_ids) != len(
            set(self.related_structure_node_ids)
        ):
            raise ValueError("audit related node IDs must be unique")
        if self.target_structure_node_id in self.related_structure_node_ids:
            raise ValueError("audit target cannot also be a related node")
        if len(self.evidence_source_refs) != len(set(self.evidence_source_refs)):
            raise ValueError("audit evidence source refs must be unique")
        if bool(self.proposed_operations) != (self.proposal_evidence is not None):
            raise ValueError(
                "audit proposal evidence is required exactly when an operation is proposed"
            )
        return self


class StructureAuditResult(_StrictFrozenModel):
    schema_version: Literal["structure-audit-result/v2"] = (
        STRUCTURE_AUDIT_RESULT_SCHEMA_VERSION
    )
    source_ir_id: str = Field(min_length=1, max_length=160)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    status: StructureAuditStatus
    findings: tuple[StructureAuditFinding, ...] = ()
    provider_runs: tuple[SemanticProviderRunRecord, ...]
    resolved_window_count: int = Field(ge=0)
    unresolved_window_count: int = Field(ge=0)
    failed_window_count: int = Field(default=0, ge=0)
    unresolved_scope_structure_node_ids: tuple[str, ...] = ()
    rejected_finding_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_result_shape(self) -> StructureAuditResult:
        window_count = self.resolved_window_count + self.unresolved_window_count
        if (
            window_count < 1
            or self.failed_window_count > self.unresolved_window_count
            or len(self.provider_runs) + self.failed_window_count != window_count
        ):
            raise ValueError(
                "audit result must account for every receipted or failed window"
            )
        expected_status = (
            StructureAuditStatus.COMPLETE
            if self.unresolved_window_count == 0
            else StructureAuditStatus.PARTIAL
            if self.resolved_window_count > 0
            else StructureAuditStatus.UNRESOLVED
        )
        if self.status is not expected_status:
            raise ValueError("audit result status does not match window outcomes")
        if self.unresolved_window_count == 0:
            if self.unresolved_scope_structure_node_ids:
                raise ValueError("complete audit cannot contain unresolved scope IDs")
        elif not self.unresolved_scope_structure_node_ids:
            raise ValueError("unresolved audit windows require their scoped node IDs")
        if len(self.unresolved_scope_structure_node_ids) != len(
            set(self.unresolved_scope_structure_node_ids)
        ):
            raise ValueError("unresolved audit scope IDs must be unique")
        finding_ids = tuple(item.finding_id for item in self.findings)
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("audit result findings must have unique stable identities")
        run_ids = {item.provider_run_id for item in self.provider_runs}
        if any(item.provider_run_id not in run_ids for item in self.findings):
            raise ValueError("audit findings require a retained provider run receipt")
        return self


def plan_structure_audit_provider_requests(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    *,
    max_nodes_per_request: int = DEFAULT_STRUCTURE_AUDIT_MAX_NODES_PER_REQUEST,
) -> tuple[StructureAuditRequest, ...]:
    """Build full-document, evidence-complete audit windows without transport.

    Classification planning remains the single grounding implementation.
    Audit adds only the missing comparison views: native SourceIR order, the
    document-level outline spine, and the nearest outline boundaries around a
    window.  It does not infer, repair, or rewrite StructureTree.
    """

    if (
        isinstance(max_nodes_per_request, bool)
        or not isinstance(max_nodes_per_request, int)
        or max_nodes_per_request < 1
    ):
        raise ValueError("max_nodes_per_request must be a positive integer")
    require_structure_snapshot(tree)

    # Reuse semantic grounding, but never reuse its atomic-provision request
    # limit.  Audit owns its own source-bound slicing below, including inside
    # a long provision such as one collapsed numbered section.
    grounding_request = plan_content_classification_grounding_request(source_ir, tree)
    all_nodes_by_id = {
        node.target_structure_node_id: _as_structure_audit_node(node)
        for node in grounding_request.nodes
    }
    expected_ids = set(grounding_request.scope_structure_node_ids)
    if set(all_nodes_by_id) != expected_ids:
        raise ValueError("audit planner did not retain the complete structure scope")

    physical_orders_by_ref = _physical_source_orders_by_ref(source_ir)
    positions_by_id = {
        node_id: StructureAuditPhysicalOrderInput(
            structure_node_id=node_id,
            physical_source_orders=tuple(
                sorted(
                    {
                        physical_orders_by_ref[source_ref]
                        for source_ref in node.target_source_refs
                        if source_ref in physical_orders_by_ref
                    }
                )
            ),
        )
        for node_id, node in all_nodes_by_id.items()
    }
    tree_nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    document = next(
        item for item in tree.nodes if item.kind is StructureNodeKind.DOCUMENT
    )
    root_outline_ids = tuple(
        item.structure_node_id
        for item in sorted(
            (
                item
                for item in tree.nodes
                if item.kind is StructureNodeKind.SECTION
                and item.parent_structure_node_id == document.structure_node_id
            ),
            key=lambda item: _node_physical_sort_key(
                item.structure_node_id,
                positions_by_id=positions_by_id,
                fallback_source_order=item.source_order,
            ),
        )
    )
    ordered_section_ids = tuple(
        item.structure_node_id
        for item in sorted(
            (item for item in tree.nodes if item.kind is StructureNodeKind.SECTION),
            key=lambda item: _node_physical_sort_key(
                item.structure_node_id,
                positions_by_id=positions_by_id,
                fallback_source_order=item.source_order,
            ),
        )
    )
    ordered_table_ids = tuple(
        item.structure_node_id
        for item in sorted(
            (item for item in tree.nodes if item.kind is StructureNodeKind.TABLE),
            key=lambda item: _node_physical_sort_key(
                item.structure_node_id,
                positions_by_id=positions_by_id,
                fallback_source_order=item.source_order,
            ),
        )
    )
    continuation_relations = tuple(
        relation
        for relation in tree.relations
        if relation.kind is StructureRelationKind.CONTINUES
        and relation.source_structure_node_id in all_nodes_by_id
        and relation.target_structure_node_id in all_nodes_by_id
    )
    continuations_by_endpoint: dict[str, list[tuple[str, str]]] = {}
    for relation in continuation_relations:
        identity = (
            relation.source_structure_node_id,
            relation.target_structure_node_id,
        )
        continuations_by_endpoint.setdefault(identity[0], []).append(identity)
        continuations_by_endpoint.setdefault(identity[1], []).append(identity)

    audit_scope_ids = tuple(
        node_id
        for node_id in grounding_request.scope_structure_node_ids
        if tree_nodes_by_id[node_id].kind in _AUDITABLE_STRUCTURE_NODE_KINDS
    )
    if not audit_scope_ids:
        raise ValueError("structure audit requires at least one auditable boundary")
    planned_windows = tuple(
        scope_ids
        for offset in range(
            0,
            len(audit_scope_ids),
            max_nodes_per_request,
        )
        if (scope_ids := audit_scope_ids[offset : offset + max_nodes_per_request])
    )
    requests_with_order: list[
        tuple[tuple[int, int, str], int, StructureAuditRequest]
    ] = []
    for window_index, scope_ids in enumerate(planned_windows):
        target_ids = set(scope_ids)
        context_by_id = {
            node.target_structure_node_id: all_nodes_by_id[
                node.target_structure_node_id
            ]
            for node in grounding_request.context_nodes
            if node.target_structure_node_id not in target_ids
        }
        for target_id in target_ids:
            for ancestor_id in all_nodes_by_id[target_id].ancestor_structure_node_ids:
                if ancestor_id in all_nodes_by_id and ancestor_id not in target_ids:
                    context_by_id[ancestor_id] = all_nodes_by_id[ancestor_id]
        for target_id in target_ids:
            for source_id, continuation_id in continuations_by_endpoint.get(
                target_id,
                (),
            ):
                for context_id in (source_id, continuation_id):
                    if context_id not in target_ids:
                        context_by_id[context_id] = all_nodes_by_id[context_id]
        # A local root spine is enough to detect an outline break.  Adding
        # every root section to every request made total input grow with the
        # whole document even though the writable target window stayed fixed.
        for context_id in _adjacent_outline_ids(
            target_ids,
            ordered_section_ids=root_outline_ids,
            positions_by_id=positions_by_id,
            tree_nodes_by_id=tree_nodes_by_id,
        ):
            if context_id not in target_ids:
                context_by_id[context_id] = all_nodes_by_id[context_id]
        for context_id in _adjacent_outline_ids(
            target_ids,
            ordered_section_ids=ordered_section_ids,
            positions_by_id=positions_by_id,
            tree_nodes_by_id=tree_nodes_by_id,
        ):
            if context_id not in target_ids:
                context_by_id[context_id] = all_nodes_by_id[context_id]
        # A missing table continuation has no existing relation from which to
        # discover its peer.  Include the nearest physical TABLE endpoints so
        # either side of an audit-window boundary remains comparable.
        for context_id in _adjacent_outline_ids(
            target_ids,
            ordered_section_ids=ordered_table_ids,
            positions_by_id=positions_by_id,
            tree_nodes_by_id=tree_nodes_by_id,
        ):
            if context_id not in target_ids:
                context_by_id[context_id] = all_nodes_by_id[context_id]
        context_nodes = tuple(
            sorted(context_by_id.values(), key=lambda node: node.source_order)
        )
        evidence_request = SemanticClassificationRequest(
            source_ir_id=grounding_request.source_ir_id,
            source_sha256=grounding_request.source_sha256,
            structure_snapshot_id=grounding_request.structure_snapshot_id,
            scope_structure_node_ids=scope_ids,
            nodes=tuple(all_nodes_by_id[node_id] for node_id in scope_ids),
            context_nodes=context_nodes,
            images=(),
        )
        request_node_ids = {
            node.target_structure_node_id
            for node in (*evidence_request.nodes, *evidence_request.context_nodes)
        }
        physical_order = tuple(
            positions_by_id[node_id]
            for node_id in sorted(
                request_node_ids,
                key=lambda node_id: _node_physical_sort_key(
                    node_id,
                    positions_by_id=positions_by_id,
                    fallback_source_order=tree_nodes_by_id[node_id].source_order,
                ),
            )
        )
        outline_spine = tuple(
            node_id for node_id in root_outline_ids if node_id in request_node_ids
        )
        current_continuations = tuple(
            StructureAuditContinuationInput(
                source_structure_node_id=relation.source_structure_node_id,
                target_structure_node_id=relation.target_structure_node_id,
                source_refs=relation.source_refs,
            )
            for relation in continuation_relations
            if relation.source_structure_node_id in request_node_ids
            and relation.target_structure_node_id in request_node_ids
        )
        audit_request = StructureAuditRequest(
            evidence_request=evidence_request,
            physical_order=physical_order,
            outline_spine_structure_node_ids=outline_spine,
            current_continuations=current_continuations,
        )
        window_order = min(
            (
                _node_physical_sort_key(
                    node_id,
                    positions_by_id=positions_by_id,
                    fallback_source_order=tree_nodes_by_id[node_id].source_order,
                )
                for node_id in evidence_request.scope_structure_node_ids
            ),
            default=(10**12, 10**12, ""),
        )
        requests_with_order.append((window_order, window_index, audit_request))

    return tuple(
        item
        for _order, _index, item in sorted(
            requests_with_order,
            key=lambda value: (value[0], value[1]),
        )
    )


def audit_structure_with_provider(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    provider: StructureAuditProviderPort,
    *,
    max_nodes_per_request: int = DEFAULT_STRUCTURE_AUDIT_MAX_NODES_PER_REQUEST,
    max_concurrency: int = 4,
) -> StructureAuditResult:
    """Run independent structure review while preserving facts and source order."""

    if not isinstance(provider, StructureAuditProviderPort):
        raise TypeError("provider must implement StructureAuditProviderPort")
    require_complete_source_ir_for_provider(
        source_ir,
        operation="structure_audit",
    )
    require_structure_snapshot(tree)
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or max_concurrency < 1
    ):
        raise ValueError("max_concurrency must be a positive integer")

    requests = plan_structure_audit_provider_requests(
        source_ir,
        tree,
        max_nodes_per_request=max_nodes_per_request,
    )
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = tuple(
            executor.submit(_audit_window_with_shared_lease, provider, request)
            for request in requests
        )
        # Reading futures in submission order keeps the aggregate deterministic
        # without sacrificing concurrent execution.
        raw_results = tuple(_audit_future_result(future) for future in futures)

    findings: list[StructureAuditFinding] = []
    findings_by_identity: set[str] = set()
    runs: list[SemanticProviderRunRecord] = []
    unresolved_scope_ids: list[str] = []
    resolved_window_count = 0
    failed_window_count = 0
    application_rejected_finding_count = 0
    for request, raw_result in zip(requests, raw_results, strict=True):
        if raw_result is None:
            failed_window_count += 1
            unresolved_scope_ids.extend(
                request.evidence_request.scope_structure_node_ids
            )
            continue
        if not isinstance(raw_result, StructureAuditProviderResult):
            raise TypeError("audit provider must return a StructureAuditProviderResult")
        runs.append(raw_result.run)
        (
            is_resolved,
            window_findings,
            window_rejected_finding_count,
        ) = _validated_window_findings(
            source_ir,
            tree,
            provider,
            request,
            raw_result,
        )
        application_rejected_finding_count += window_rejected_finding_count
        if not is_resolved:
            unresolved_scope_ids.extend(
                request.evidence_request.scope_structure_node_ids
            )
        else:
            resolved_window_count += 1
        for finding in window_findings:
            if finding.finding_id in findings_by_identity:
                continue
            findings_by_identity.add(finding.finding_id)
            findings.append(finding)

    unresolved_window_count = len(requests) - resolved_window_count
    status = (
        StructureAuditStatus.COMPLETE
        if unresolved_window_count == 0
        else StructureAuditStatus.PARTIAL
        if resolved_window_count > 0
        else StructureAuditStatus.UNRESOLVED
    )
    return StructureAuditResult(
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        status=status,
        findings=tuple(findings),
        provider_runs=tuple(runs),
        resolved_window_count=resolved_window_count,
        unresolved_window_count=unresolved_window_count,
        failed_window_count=failed_window_count,
        unresolved_scope_structure_node_ids=tuple(dict.fromkeys(unresolved_scope_ids)),
        rejected_finding_count=sum(
            item.rejected_finding_count for item in raw_results if item is not None
        )
        + application_rejected_finding_count,
    )


def _audit_window_with_shared_lease(
    provider: StructureAuditProviderPort,
    request: StructureAuditRequest,
) -> StructureAuditProviderResult:
    with llm_document_remote_chunk_lease():
        return provider.audit_structure(request)


def _audit_future_result(
    future: Future[StructureAuditProviderResult],
) -> StructureAuditProviderResult | None:
    """Keep one audit-window exception from erasing completed peer windows."""

    try:
        return future.result()
    except Exception:
        return None


def stable_structure_audit_finding_id(
    *,
    source_ir_id: str,
    structure_snapshot_id: str,
    category: StructureAuditCategory,
    target_structure_node_id: str,
    related_structure_node_ids: tuple[str, ...],
) -> str:
    payload = json.dumps(
        (
            source_ir_id,
            structure_snapshot_id,
            category.value,
            target_structure_node_id,
            tuple(sorted(related_structure_node_ids)),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "structure-audit-finding-"
        + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    )


def _validated_window_findings(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    provider: StructureAuditProviderPort,
    request: StructureAuditRequest,
    result: StructureAuditProviderResult,
) -> tuple[bool, tuple[StructureAuditFinding, ...], int]:
    run = result.run
    if (
        run.operation != "audit_structure"
        or (run.provider_id, run.provider_version, run.model_id)
        != (provider.provider_id, provider.provider_version, provider.model_id)
        or run.status
        not in {
            SemanticProviderRunStatus.SUCCESS,
            SemanticProviderRunStatus.PARTIAL,
        }
    ):
        return False, (), 0

    evidence_request = request.evidence_request
    target_ids = {item.target_structure_node_id for item in evidence_request.nodes}
    all_ids = {
        item.target_structure_node_id
        for item in (*evidence_request.nodes, *evidence_request.context_nodes)
    }
    allowed_evidence_refs = {
        evidence.source_ref
        for node in (*evidence_request.nodes, *evidence_request.context_nodes)
        for evidence in node.evidence
    }
    tree_nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    findings: list[StructureAuditFinding] = []
    rejected_finding_count = 0
    for decision in result.findings:
        if (
            decision.target_structure_node_id not in target_ids
            or not set(decision.related_structure_node_ids).issubset(all_ids)
            or not set(decision.evidence_source_refs).issubset(allowed_evidence_refs)
            or not _decision_nodes_are_source_bound(
                decision,
                nodes_by_id=tree_nodes_by_id,
            )
            or not _decision_proposal_is_request_bound(
                decision,
                request=request,
            )
        ):
            rejected_finding_count += 1
            continue
        findings.append(
            _bind_finding(
                source_ir,
                tree,
                decision,
                provider_run_id=run.provider_run_id,
            )
        )
    return (
        run.status is SemanticProviderRunStatus.SUCCESS
        and result.rejected_finding_count == 0
        and rejected_finding_count == 0,
        tuple(findings),
        rejected_finding_count,
    )


def _decision_nodes_are_source_bound(
    decision: StructureAuditDecision,
    *,
    nodes_by_id: dict[str, StructureNode],
) -> bool:
    evidence_refs = set(decision.evidence_source_refs)
    cited_node_ids = (
        decision.target_structure_node_id,
        *decision.related_structure_node_ids,
    )
    return all(
        node_id in nodes_by_id
        and bool(set(nodes_by_id[node_id].source_refs).intersection(evidence_refs))
        for node_id in cited_node_ids
    )


def _decision_proposal_is_request_bound(
    decision: StructureAuditDecision,
    *,
    request: StructureAuditRequest,
) -> bool:
    if not decision.proposed_operations:
        return True
    cited_node_ids = {
        decision.target_structure_node_id,
        *decision.related_structure_node_ids,
    }
    evidence_refs = set(decision.evidence_source_refs)
    operation = decision.proposed_operations[0]
    if isinstance(operation, ReparentStructureOperationV1):
        return (
            decision.category is StructureAuditCategory.POSSIBLE_MISPLACED_PARENT
            and operation.structure_node_id == decision.target_structure_node_id
            and operation.new_parent_structure_node_id
            in decision.related_structure_node_ids
        )
    if isinstance(operation, LogicalReorderStructureOperationV1):
        return (
            decision.category is StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR
            and operation.parent_structure_node_id in cited_node_ids
            and decision.target_structure_node_id
            in operation.ordered_child_structure_node_ids
            and set(operation.ordered_child_structure_node_ids).issubset(cited_node_ids)
        )
    if isinstance(operation, BoundarySplitStructureOperationV1):
        return (
            decision.category is StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY
            and operation.structure_node_id == decision.target_structure_node_id
            and all(
                span.source_ref in evidence_refs
                for part in operation.parts
                for span in part.source_spans
            )
            and _boundary_split_exactly_covers_request_target(
                operation,
                request=request,
            )
        )
    if isinstance(operation, ContinuationMergeStructureOperationV1):
        endpoints = (
            operation.primary_structure_node_id,
            operation.continuation_structure_node_id,
        )
        return (
            decision.category is StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT
            and decision.target_structure_node_id in endpoints
            and set(endpoints).issubset(cited_node_ids)
            and endpoints
            in {
                (
                    item.source_structure_node_id,
                    item.target_structure_node_id,
                )
                for item in request.current_continuations
            }
        )
    if isinstance(operation, AddStructureRelationOperationV1):
        endpoints = {
            operation.source_structure_node_id,
            operation.target_structure_node_id,
        }
        current_pairs = {
            frozenset(
                {
                    item.source_structure_node_id,
                    item.target_structure_node_id,
                }
            )
            for item in request.current_continuations
        }
        return (
            decision.category
            is StructureAuditCategory.POSSIBLE_TABLE_CONTINUATION_ERROR
            and operation.kind is StructureRelationKind.CONTINUES
            and decision.target_structure_node_id in endpoints
            and endpoints.issubset(cited_node_ids)
            and set(operation.source_refs).issubset(evidence_refs)
            and frozenset(endpoints) not in current_pairs
        )
    if isinstance(operation, RemoveStructureRelationOperationV1):
        return False
    return False


def _boundary_split_exactly_covers_request_target(
    operation: BoundarySplitStructureOperationV1,
    *,
    request: StructureAuditRequest,
) -> bool:
    target = next(
        (
            node
            for node in request.evidence_request.nodes
            if node.target_structure_node_id == operation.structure_node_id
        ),
        None,
    )
    if target is None or not target.evidence:
        return False
    expected = tuple(
        (evidence.source_ref, len(evidence.text)) for evidence in target.evidence
    )
    expected_index = 0
    cursor = 0
    for span in (span for part in operation.parts for span in part.source_spans):
        if expected_index >= len(expected):
            return False
        expected_ref, expected_length = expected[expected_index]
        if (
            span.source_ref != expected_ref
            or span.start_offset != cursor
            or span.end_offset > expected_length
        ):
            return False
        cursor = span.end_offset
        if cursor == expected_length:
            expected_index += 1
            cursor = 0
    return expected_index == len(expected) and cursor == 0


def _bind_finding(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    decision: StructureAuditDecision,
    *,
    provider_run_id: str,
) -> StructureAuditFinding:
    return StructureAuditFinding(
        finding_id=stable_structure_audit_finding_id(
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=structure_snapshot_id(tree),
            category=decision.category,
            target_structure_node_id=decision.target_structure_node_id,
            related_structure_node_ids=decision.related_structure_node_ids,
        ),
        category=decision.category,
        target_structure_node_id=decision.target_structure_node_id,
        related_structure_node_ids=decision.related_structure_node_ids,
        evidence_source_refs=decision.evidence_source_refs,
        reason_codes=decision.reason_codes,
        provider_run_id=provider_run_id,
        proposed_operations=decision.proposed_operations,
        proposal_evidence=decision.proposal_evidence,
    )


def _physical_source_orders_by_ref(
    source_ir: SourceIRSnapshot,
) -> dict[StructureSourceRef, int]:
    values = {
        StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=node.source_node_id,
        ): node.source_order
        for node in source_ir.nodes
    }
    for node in source_ir.nodes:
        if node.table_payload is None:
            continue
        for cell in node.table_payload.cells:
            values[
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
            ] = node.source_order
    return values


def _as_structure_audit_node(node: SemanticNodeInput) -> SemanticNodeInput:
    """Remove semantic-only descendant duplication from one audit node."""

    target_refs = set(node.target_source_refs)
    target_evidence = tuple(
        item for item in node.evidence if item.source_ref in target_refs
    )
    return node.model_copy(
        update={
            "image_page_numbers": (),
            "provision_structure_node_ids": (),
            "evidence": target_evidence,
        }
    )


def _node_physical_sort_key(
    node_id: str,
    *,
    positions_by_id: dict[str, StructureAuditPhysicalOrderInput],
    fallback_source_order: int,
) -> tuple[int, int, str]:
    orders = positions_by_id[node_id].physical_source_orders
    first_order = orders[0] if orders else 10**12 + fallback_source_order
    return (first_order, fallback_source_order, node_id)


def _adjacent_outline_ids(
    target_ids: set[str],
    *,
    ordered_section_ids: tuple[str, ...],
    positions_by_id: dict[str, StructureAuditPhysicalOrderInput],
    tree_nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    target_keys = tuple(
        _node_physical_sort_key(
            node_id,
            positions_by_id=positions_by_id,
            fallback_source_order=tree_nodes_by_id[node_id].source_order,
        )
        for node_id in target_ids
    )
    if not target_keys:
        return ()
    lower = min(target_keys)
    upper = max(target_keys)
    before: str | None = None
    after: str | None = None
    for node_id in ordered_section_ids:
        if node_id in target_ids:
            continue
        key = _node_physical_sort_key(
            node_id,
            positions_by_id=positions_by_id,
            fallback_source_order=tree_nodes_by_id[node_id].source_order,
        )
        if key < lower:
            before = node_id
        elif key > upper:
            after = node_id
            break
    return tuple(item for item in (before, after) if item is not None)


__all__ = [
    "DEFAULT_STRUCTURE_AUDIT_MAX_NODES_PER_REQUEST",
    "STRUCTURE_AUDIT_RESULT_SCHEMA_VERSION",
    "StructureAuditFinding",
    "StructureAuditResult",
    "StructureAuditStatus",
    "audit_structure_with_provider",
    "plan_structure_audit_provider_requests",
    "stable_structure_audit_finding_id",
]
