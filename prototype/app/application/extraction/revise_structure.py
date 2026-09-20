from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError

from app.application.extraction.resolve_structure import (
    StructureResolutionRejected,
    validate_structure_tree_against_source_ir,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import (
    BilingualCounterpartGroup,
    BilingualMismatchSignal,
    DocumentLanguagePolicy,
    StructureBoundaryBasis,
    StructureFinding,
    StructureNode,
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
    VisualTableCellTextBinding,
    stable_structure_node_id,
    stable_structure_relation_id,
)
from app.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundaryResegmentStructureOperationV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    EffectiveStructureSnapshotV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructureApplyResultV1,
    StructureImpactV1,
    StructureNodeSourceSpansV1,
    StructurePatchOperationV1,
    StructurePatchProposalV1,
    StructurePatchV1,
    StructureRevisionAction,
    StructureRevisionRejected,
    StructureRevisionRejectionCode,
    StructureRevisionV1,
    StructureSourceSpanV1,
    canonical_model_sha256,
    stable_base_revision_chain_sha256,
    stable_effective_structure_snapshot_id,
    stable_effective_structure_tree_sha256,
    stable_revision_chain_sha256,
    stable_structure_patch_id,
    stable_structure_patch_proposal_id,
    stable_structure_revision_id,
)


@dataclass
class _TreeState:
    nodes_by_id: dict[str, StructureNode]
    child_ids_by_parent: dict[str, list[str]]
    relations_by_id: dict[str, StructureRelation]
    spans_by_node: dict[str, tuple[StructureSourceSpanV1, ...]]
    document_language_policy: DocumentLanguagePolicy | None
    bilingual_counterpart_groups: tuple[BilingualCounterpartGroup, ...]
    bilingual_mismatch_signals: tuple[BilingualMismatchSignal, ...]
    visual_table_cell_text_bindings: tuple[VisualTableCellTextBinding, ...]
    findings: tuple[StructureFinding, ...]


def get_base_effective_structure(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
) -> EffectiveStructureSnapshotV1:
    """Validate the deterministic base and expose revision zero."""

    source_hash_before = canonical_model_sha256(source_ir)
    source_canonical_sha256, base_canonical_sha256 = _validate_base_inputs(
        source_ir,
        base_tree,
    )
    source_text_by_ref = _source_text_catalog(source_ir)
    node_source_spans = tuple(
        StructureNodeSourceSpansV1(
            structure_node_id=node.structure_node_id,
            source_spans=tuple(
                StructureSourceSpanV1(
                    source_ref=source_ref,
                    start_offset=0,
                    end_offset=len(source_text_by_ref[source_ref]),
                )
                for source_ref in node.source_refs
            ),
        )
        for node in base_tree.nodes
    )
    relations = tuple(sorted(base_tree.relations, key=_relation_sort_key))
    chain_sha256 = stable_base_revision_chain_sha256(
        source_ir_id=source_ir.source_ir_id,
        source_ir_canonical_sha256=source_canonical_sha256,
        base_structure_snapshot_id=base_tree.structure_snapshot_id,
        base_structure_canonical_sha256=base_canonical_sha256,
    )
    effective_tree_sha256 = stable_effective_structure_tree_sha256(
        source_ir_id=source_ir.source_ir_id,
        base_structure_snapshot_id=base_tree.structure_snapshot_id,
        resolver_id=base_tree.resolver_id,
        resolver_version=base_tree.resolver_version,
        source_node_ids=base_tree.source_node_ids,
        source_cell_ids=base_tree.source_cell_ids,
        visual_text_observation_ids=base_tree.visual_text_observation_ids,
        visual_table_cell_ids=base_tree.visual_table_cell_ids,
        nodes=base_tree.nodes,
        relations=relations,
        node_source_spans=node_source_spans,
        document_language_policy=base_tree.document_language_policy,
        bilingual_counterpart_groups=base_tree.bilingual_counterpart_groups,
        bilingual_mismatch_signals=base_tree.bilingual_mismatch_signals,
        visual_table_cell_text_bindings=base_tree.visual_table_cell_text_bindings,
        findings=base_tree.findings,
    )
    effective_snapshot_id = stable_effective_structure_snapshot_id(
        source_ir_id=source_ir.source_ir_id,
        base_structure_snapshot_id=base_tree.structure_snapshot_id,
        revision_number=0,
        head_revision_id=None,
        revision_chain_sha256=chain_sha256,
        effective_tree_sha256=effective_tree_sha256,
    )
    snapshot = EffectiveStructureSnapshotV1(
        effective_snapshot_id=effective_snapshot_id,
        effective_tree_sha256=effective_tree_sha256,
        revision_chain_sha256=chain_sha256,
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        source_ir_canonical_sha256=source_canonical_sha256,
        base_structure_snapshot_id=base_tree.structure_snapshot_id,
        base_structure_canonical_sha256=base_canonical_sha256,
        resolver_id=base_tree.resolver_id,
        resolver_version=base_tree.resolver_version,
        revision_number=0,
        head_revision_id=None,
        source_node_ids=base_tree.source_node_ids,
        source_cell_ids=base_tree.source_cell_ids,
        visual_text_observation_ids=base_tree.visual_text_observation_ids,
        visual_table_cell_ids=base_tree.visual_table_cell_ids,
        nodes=base_tree.nodes,
        relations=relations,
        node_source_spans=node_source_spans,
        document_language_policy=base_tree.document_language_policy,
        bilingual_counterpart_groups=base_tree.bilingual_counterpart_groups,
        bilingual_mismatch_signals=base_tree.bilingual_mismatch_signals,
        visual_table_cell_text_bindings=base_tree.visual_table_cell_text_bindings,
        findings=base_tree.findings,
    )
    _validate_effective_snapshot(snapshot, source_ir, base_tree)
    _require_source_unchanged(source_ir, source_hash_before)
    return snapshot


def create_structure_patch_proposal(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    operations: tuple[StructurePatchOperationV1, ...],
) -> StructurePatchProposalV1:
    """Create a bound proposal without granting it apply authority."""

    if not operations:
        raise ValueError("a Structure patch proposal requires at least one operation")
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    proposal_id = stable_structure_patch_proposal_id(
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        source_ir_canonical_sha256=current.source_ir_canonical_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        base_structure_canonical_sha256=current.base_structure_canonical_sha256,
        expected_revision_number=current.revision_number,
        expected_effective_snapshot_id=current.effective_snapshot_id,
        operations=operations,
    )
    return StructurePatchProposalV1(
        proposal_id=proposal_id,
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        source_ir_canonical_sha256=current.source_ir_canonical_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        base_structure_canonical_sha256=current.base_structure_canonical_sha256,
        expected_revision_number=current.revision_number,
        expected_effective_snapshot_id=current.effective_snapshot_id,
        operations=operations,
    )


def validate_structure_patch_proposal(
    proposal: StructurePatchProposalV1,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
) -> StructurePatchV1:
    """Fail closed, then convert an untrusted proposal into an applicable patch."""

    if not isinstance(proposal, StructurePatchProposalV1):
        raise TypeError("proposal must be a StructurePatchProposalV1")
    source_hash_before = canonical_model_sha256(source_ir)
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    _require_proposal_binding(proposal, current)
    _apply_operations(
        current,
        proposal.operations,
        source_ir=source_ir,
        base_tree=base_tree,
    )
    patch_id = stable_structure_patch_id(
        accepted_proposal_id=proposal.proposal_id,
        source_ir_id=proposal.source_ir_id,
        source_sha256=proposal.source_sha256,
        source_ir_canonical_sha256=proposal.source_ir_canonical_sha256,
        base_structure_snapshot_id=proposal.base_structure_snapshot_id,
        base_structure_canonical_sha256=proposal.base_structure_canonical_sha256,
        expected_revision_number=proposal.expected_revision_number,
        expected_effective_snapshot_id=proposal.expected_effective_snapshot_id,
        operations=proposal.operations,
    )
    patch = StructurePatchV1(
        patch_id=patch_id,
        accepted_proposal_id=proposal.proposal_id,
        source_ir_id=proposal.source_ir_id,
        source_sha256=proposal.source_sha256,
        source_ir_canonical_sha256=proposal.source_ir_canonical_sha256,
        base_structure_snapshot_id=proposal.base_structure_snapshot_id,
        base_structure_canonical_sha256=proposal.base_structure_canonical_sha256,
        expected_revision_number=proposal.expected_revision_number,
        expected_effective_snapshot_id=proposal.expected_effective_snapshot_id,
        operations=proposal.operations,
    )
    _require_source_unchanged(source_ir, source_hash_before)
    return patch


def apply_structure_patch(
    patch: StructurePatchV1,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
) -> StructureApplyResultV1:
    """Append one APPLY revision, or return the prior result for the same patch."""

    if not isinstance(patch, StructurePatchV1):
        raise TypeError(
            "apply requires a validated StructurePatchV1; proposals have no write access"
        )
    source_hash_before = canonical_model_sha256(source_ir)
    current, _ = _replay_structure_revision_chain(
        source_ir,
        base_tree,
        revisions,
    )
    for revision in revisions:
        if (
            revision.action is StructureRevisionAction.APPLY
            and revision.patch is not None
            and revision.patch.patch_id == patch.patch_id
        ):
            if revision.patch != patch:
                _reject(
                    StructureRevisionRejectionCode.INVALID_PATCH,
                    "a repeated patch ID must carry the exact original patch",
                )
            _require_source_unchanged(source_ir, source_hash_before)
            return StructureApplyResultV1(
                appended=False,
                revision=revision,
                effective_snapshot=current,
                impact=revision.impact,
            )
        if (
            revision.action is StructureRevisionAction.APPLY
            and revision.patch is not None
            and revision.patch.accepted_proposal_id == patch.accepted_proposal_id
        ):
            _reject(
                StructureRevisionRejectionCode.INVALID_PATCH,
                "one accepted proposal cannot produce multiple patches",
            )

    _require_patch_binding(patch, current)
    state = _apply_operations(
        current,
        patch.operations,
        source_ir=source_ir,
        base_tree=base_tree,
    )
    revision, effective, impact = _build_transition(
        current=current,
        state=state,
        source_ir=source_ir,
        base_tree=base_tree,
        action=StructureRevisionAction.APPLY,
        patch=patch,
        undo_target_revision_id=None,
    )
    _require_source_unchanged(source_ir, source_hash_before)
    return StructureApplyResultV1(
        appended=True,
        revision=revision,
        effective_snapshot=effective,
        impact=impact,
    )


def undo_structure_revision(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    *,
    expected_revision_number: int,
    expected_effective_snapshot_id: str,
) -> StructureApplyResultV1:
    """Append an UNDO of the current head; history and the undone revision remain."""

    source_hash_before = canonical_model_sha256(source_ir)
    current, before_by_revision_id = _replay_structure_revision_chain(
        source_ir,
        base_tree,
        revisions,
    )
    if (
        expected_revision_number != current.revision_number
        or expected_effective_snapshot_id != current.effective_snapshot_id
    ):
        _reject(
            StructureRevisionRejectionCode.STALE_BASE_REVISION,
            "undo must bind the current effective Structure revision",
        )
    if current.head_revision_id is None:
        _reject(
            StructureRevisionRejectionCode.NOTHING_TO_UNDO,
            "the deterministic base revision has nothing to undo",
        )
    restored = before_by_revision_id[current.head_revision_id]
    state = _state_from_effective(restored)
    revision, effective, impact = _build_transition(
        current=current,
        state=state,
        source_ir=source_ir,
        base_tree=base_tree,
        action=StructureRevisionAction.UNDO,
        patch=None,
        undo_target_revision_id=current.head_revision_id,
    )
    _require_source_unchanged(source_ir, source_hash_before)
    return StructureApplyResultV1(
        appended=True,
        revision=revision,
        effective_snapshot=effective,
        impact=impact,
    )


def validate_structure_revision_chain(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
) -> EffectiveStructureSnapshotV1:
    """Replay and validate append-only lineage without re-deriving an effective tree."""

    current, _ = _replay_structure_revision_chain(source_ir, base_tree, revisions)
    return current


def calculate_structure_impact(
    before: EffectiveStructureSnapshotV1,
    after: EffectiveStructureSnapshotV1,
) -> StructureImpactV1:
    """Calculate deterministic invalidation scope between two effective trees."""

    if not isinstance(before, EffectiveStructureSnapshotV1) or not isinstance(
        after,
        EffectiveStructureSnapshotV1,
    ):
        raise TypeError("impact calculation requires two effective snapshots")
    return _calculate_impact(
        before,
        after.nodes,
        after.relations,
        seed_node_ids=(),
    )


def _replay_structure_revision_chain(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
) -> tuple[
    EffectiveStructureSnapshotV1,
    dict[str, EffectiveStructureSnapshotV1],
]:
    if not isinstance(revisions, tuple) or any(
        not isinstance(revision, StructureRevisionV1) for revision in revisions
    ):
        raise TypeError("revisions must be a tuple of StructureRevisionV1 values")
    source_hash_before = canonical_model_sha256(source_ir)
    current = get_base_effective_structure(source_ir, base_tree)
    before_by_revision_id: dict[str, EffectiveStructureSnapshotV1] = {}
    seen_revision_ids: set[str] = set()
    seen_patch_ids: set[str] = set()

    for index, supplied_revision in enumerate(revisions, start=1):
        if supplied_revision.revision_id in seen_revision_ids:
            _reject(
                StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
                "revision IDs must be unique within one chain",
            )
        _require_revision_lineage(supplied_revision, current, index)
        if supplied_revision.action is StructureRevisionAction.APPLY:
            assert supplied_revision.patch is not None
            if supplied_revision.patch.patch_id in seen_patch_ids:
                _reject(
                    StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
                    "one patch may be appended at most once",
                )
            _require_patch_binding(supplied_revision.patch, current)
            state = _apply_operations(
                current,
                supplied_revision.patch.operations,
                source_ir=source_ir,
                base_tree=base_tree,
            )
            expected_revision, expected_effective, _ = _build_transition(
                current=current,
                state=state,
                source_ir=source_ir,
                base_tree=base_tree,
                action=StructureRevisionAction.APPLY,
                patch=supplied_revision.patch,
                undo_target_revision_id=None,
            )
            seen_patch_ids.add(supplied_revision.patch.patch_id)
        else:
            if (
                supplied_revision.undo_target_revision_id is None
                or supplied_revision.undo_target_revision_id != current.head_revision_id
                or supplied_revision.undo_target_revision_id
                not in before_by_revision_id
            ):
                _reject(
                    StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
                    "UNDO must target the immediately preceding revision",
                )
            restored = before_by_revision_id[supplied_revision.undo_target_revision_id]
            expected_revision, expected_effective, _ = _build_transition(
                current=current,
                state=_state_from_effective(restored),
                source_ir=source_ir,
                base_tree=base_tree,
                action=StructureRevisionAction.UNDO,
                patch=None,
                undo_target_revision_id=supplied_revision.undo_target_revision_id,
            )
        if supplied_revision != expected_revision:
            _reject(
                StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
                "revision content does not match deterministic replay",
            )
        before_by_revision_id[supplied_revision.revision_id] = current
        seen_revision_ids.add(supplied_revision.revision_id)
        current = expected_effective

    _require_source_unchanged(source_ir, source_hash_before)
    return current, before_by_revision_id


def _build_transition(
    *,
    current: EffectiveStructureSnapshotV1,
    state: _TreeState,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    action: StructureRevisionAction,
    patch: StructurePatchV1 | None,
    undo_target_revision_id: str | None,
) -> tuple[
    StructureRevisionV1,
    EffectiveStructureSnapshotV1,
    StructureImpactV1,
]:
    nodes, relations, node_source_spans = _normalize_and_validate_state(
        state,
        source_ir=source_ir,
        base_tree=base_tree,
    )
    impact = _calculate_impact(
        current,
        nodes,
        relations,
        seed_node_ids=(),
    )
    effective_tree_sha256 = stable_effective_structure_tree_sha256(
        source_ir_id=current.source_ir_id,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        resolver_id=current.resolver_id,
        resolver_version=current.resolver_version,
        source_node_ids=current.source_node_ids,
        source_cell_ids=current.source_cell_ids,
        visual_text_observation_ids=current.visual_text_observation_ids,
        visual_table_cell_ids=current.visual_table_cell_ids,
        nodes=nodes,
        relations=relations,
        node_source_spans=node_source_spans,
        document_language_policy=state.document_language_policy,
        bilingual_counterpart_groups=state.bilingual_counterpart_groups,
        bilingual_mismatch_signals=state.bilingual_mismatch_signals,
        visual_table_cell_text_bindings=state.visual_table_cell_text_bindings,
        findings=state.findings,
    )
    revision_number = current.revision_number + 1
    revision_id = stable_structure_revision_id(
        revision_number=revision_number,
        parent_revision_id=current.head_revision_id,
        action=action,
        patch=patch,
        undo_target_revision_id=undo_target_revision_id,
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        source_ir_canonical_sha256=current.source_ir_canonical_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        base_structure_canonical_sha256=current.base_structure_canonical_sha256,
        previous_effective_snapshot_id=current.effective_snapshot_id,
        previous_effective_tree_sha256=current.effective_tree_sha256,
        previous_revision_chain_sha256=current.revision_chain_sha256,
        effective_tree_sha256=effective_tree_sha256,
        impact=impact,
    )
    revision_chain_sha256 = stable_revision_chain_sha256(
        previous_revision_chain_sha256=current.revision_chain_sha256,
        revision_id=revision_id,
    )
    effective_snapshot_id = stable_effective_structure_snapshot_id(
        source_ir_id=current.source_ir_id,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        revision_number=revision_number,
        head_revision_id=revision_id,
        revision_chain_sha256=revision_chain_sha256,
        effective_tree_sha256=effective_tree_sha256,
    )
    effective = EffectiveStructureSnapshotV1(
        effective_snapshot_id=effective_snapshot_id,
        effective_tree_sha256=effective_tree_sha256,
        revision_chain_sha256=revision_chain_sha256,
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        source_ir_canonical_sha256=current.source_ir_canonical_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        base_structure_canonical_sha256=current.base_structure_canonical_sha256,
        resolver_id=current.resolver_id,
        resolver_version=current.resolver_version,
        revision_number=revision_number,
        head_revision_id=revision_id,
        source_node_ids=current.source_node_ids,
        source_cell_ids=current.source_cell_ids,
        visual_text_observation_ids=current.visual_text_observation_ids,
        visual_table_cell_ids=current.visual_table_cell_ids,
        nodes=nodes,
        relations=relations,
        node_source_spans=node_source_spans,
        document_language_policy=state.document_language_policy,
        bilingual_counterpart_groups=state.bilingual_counterpart_groups,
        bilingual_mismatch_signals=state.bilingual_mismatch_signals,
        visual_table_cell_text_bindings=state.visual_table_cell_text_bindings,
        findings=state.findings,
    )
    revision = StructureRevisionV1(
        revision_id=revision_id,
        revision_number=revision_number,
        parent_revision_id=current.head_revision_id,
        action=action,
        patch=patch,
        undo_target_revision_id=undo_target_revision_id,
        source_ir_id=current.source_ir_id,
        source_sha256=current.source_sha256,
        source_ir_canonical_sha256=current.source_ir_canonical_sha256,
        base_structure_snapshot_id=current.base_structure_snapshot_id,
        base_structure_canonical_sha256=current.base_structure_canonical_sha256,
        previous_effective_snapshot_id=current.effective_snapshot_id,
        previous_effective_tree_sha256=current.effective_tree_sha256,
        previous_revision_chain_sha256=current.revision_chain_sha256,
        effective_snapshot_id=effective_snapshot_id,
        effective_tree_sha256=effective_tree_sha256,
        revision_chain_sha256=revision_chain_sha256,
        impact=impact,
    )
    _validate_effective_snapshot(effective, source_ir, base_tree)
    return revision, effective, impact


def _apply_operations(
    current: EffectiveStructureSnapshotV1,
    operations: tuple[StructurePatchOperationV1, ...],
    *,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
) -> _TreeState:
    state = _state_from_effective(current)
    source_text_by_ref = _source_text_catalog(source_ir)
    for operation in operations:
        if isinstance(operation, ReparentStructureOperationV1):
            _apply_reparent(state, operation)
        elif isinstance(operation, LogicalReorderStructureOperationV1):
            _apply_logical_reorder(state, operation)
        elif isinstance(operation, BoundarySplitStructureOperationV1):
            _apply_boundary_split(
                state,
                operation,
                source_text_by_ref=source_text_by_ref,
                current=current,
            )
        elif isinstance(operation, BoundaryResegmentStructureOperationV1):
            _apply_boundary_resegment(
                state,
                operation,
                source_text_by_ref=source_text_by_ref,
                current=current,
            )
        elif isinstance(operation, ContinuationMergeStructureOperationV1):
            _apply_continuation_merge(state, operation)
        elif isinstance(operation, AddStructureRelationOperationV1):
            _apply_add_relation(
                state,
                operation,
                source_text_by_ref=source_text_by_ref,
                current=current,
            )
        elif isinstance(operation, RemoveStructureRelationOperationV1):
            _apply_remove_relation(state, operation)
        else:  # pragma: no cover - discriminated Pydantic union is closed.
            _reject(
                StructureRevisionRejectionCode.INVALID_OPERATION,
                "unsupported Structure patch operation",
            )
    nodes, relations, node_source_spans = _normalize_and_validate_state(
        state,
        source_ir=source_ir,
        base_tree=base_tree,
    )
    if (
        nodes == current.nodes
        and relations == current.relations
        and node_source_spans == current.node_source_spans
        and state.document_language_policy == current.document_language_policy
        and state.bilingual_counterpart_groups == current.bilingual_counterpart_groups
        and state.bilingual_mismatch_signals == current.bilingual_mismatch_signals
        and state.visual_table_cell_text_bindings
        == current.visual_table_cell_text_bindings
        and state.findings == current.findings
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "Structure patch operations must produce a material effective-tree change",
        )
    return state


def _apply_reparent(
    state: _TreeState,
    operation: ReparentStructureOperationV1,
) -> None:
    node = state.nodes_by_id.get(operation.structure_node_id)
    new_parent = state.nodes_by_id.get(operation.new_parent_structure_node_id)
    if node is None or new_parent is None:
        _reject(
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
            "re-parent endpoints must exist in the current effective tree",
        )
    if node.parent_structure_node_id is None:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "the document root cannot be re-parented",
        )
    if node.parent_structure_node_id == new_parent.structure_node_id:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "same-parent movement must use logical_reorder",
        )
    cursor: StructureNode | None = new_parent
    while cursor is not None:
        if cursor.structure_node_id == node.structure_node_id:
            _reject(
                StructureRevisionRejectionCode.GRAPH_CYCLE,
                "re-parent would create a parent cycle",
            )
        parent_id = cursor.parent_structure_node_id
        cursor = None if parent_id is None else state.nodes_by_id[parent_id]

    old_parent_id = node.parent_structure_node_id
    old_siblings = state.child_ids_by_parent[old_parent_id]
    old_siblings.remove(node.structure_node_id)
    new_siblings = state.child_ids_by_parent[new_parent.structure_node_id]
    if operation.new_sibling_index > len(new_siblings):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "new_sibling_index exceeds the destination sibling scope",
        )
    new_siblings.insert(operation.new_sibling_index, node.structure_node_id)
    state.nodes_by_id[node.structure_node_id] = _replace_node(
        node,
        parent_structure_node_id=new_parent.structure_node_id,
    )


def _apply_logical_reorder(
    state: _TreeState,
    operation: LogicalReorderStructureOperationV1,
) -> None:
    if operation.parent_structure_node_id not in state.nodes_by_id:
        _reject(
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
            "logical reorder parent must exist",
        )
    current_children = tuple(
        state.child_ids_by_parent[operation.parent_structure_node_id]
    )
    proposed_children = operation.ordered_child_structure_node_ids
    if len(current_children) != len(proposed_children) or set(current_children) != set(
        proposed_children
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "logical reorder must be an exact permutation of one sibling scope",
        )
    if current_children == proposed_children:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "logical reorder must change the sibling order",
        )
    state.child_ids_by_parent[operation.parent_structure_node_id] = list(
        proposed_children
    )


def _apply_boundary_split(
    state: _TreeState,
    operation: BoundarySplitStructureOperationV1,
    *,
    source_text_by_ref: dict[StructureSourceRef, str],
    current: EffectiveStructureSnapshotV1,
) -> None:
    target = state.nodes_by_id.get(operation.structure_node_id)
    if target is None:
        _reject(
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
            "boundary split target must exist",
        )
    if target.kind not in {
        StructureNodeKind.CONTENT,
        StructureNodeKind.CONTINUATION,
        StructureNodeKind.CLAUSE,
    }:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary split currently supports grounded content-like leaf nodes",
        )
    if state.child_ids_by_parent[target.structure_node_id]:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary split requires a leaf node",
        )
    _require_node_removal_safe(state, target.structure_node_id)
    touching_relations = tuple(
        relation
        for relation in state.relations_by_id.values()
        if (
            relation.source_structure_node_id == target.structure_node_id
            or relation.target_structure_node_id == target.structure_node_id
        )
        and relation.kind is not StructureRelationKind.CONTAINS
    )
    if touching_relations:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary split requires explicit redistribution of non-parent relations",
        )

    original_spans = state.spans_by_node[target.structure_node_id]
    _validate_split_partition(
        original_spans,
        operation,
        source_text_by_ref=source_text_by_ref,
    )
    assert target.parent_structure_node_id is not None
    siblings = state.child_ids_by_parent[target.parent_structure_node_id]
    target_index = siblings.index(target.structure_node_id)
    new_node_ids: list[str] = []
    for part in operation.parts:
        identity_key = _source_span_identity_key(
            source_ir_id=current.source_ir_id,
            source_spans=part.source_spans,
        )
        new_node_id = stable_structure_node_id(
            source_ir_id=current.source_ir_id,
            resolver_id=current.resolver_id,
            resolver_version=current.resolver_version,
            kind=target.kind,
            identity_key=identity_key,
        )
        if new_node_id in state.nodes_by_id or new_node_id in new_node_ids:
            _reject(
                StructureRevisionRejectionCode.INVALID_OPERATION,
                "boundary split would duplicate an existing source-span identity",
            )
        source_refs = tuple(
            dict.fromkeys(span.source_ref for span in part.source_spans)
        )
        effective_semantic_refs = set(target.effective_semantic_source_refs)
        semantic_source_refs = tuple(
            source_ref
            for source_ref in source_refs
            if source_ref in effective_semantic_refs
        )
        new_node = StructureNode(
            structure_node_id=new_node_id,
            identity_key=identity_key,
            kind=target.kind,
            parent_structure_node_id=target.parent_structure_node_id,
            source_order=target.source_order,
            source_refs=source_refs,
            semantic_source_refs=(
                None if semantic_source_refs == source_refs else semantic_source_refs
            ),
            boundary_basis=StructureBoundaryBasis.MODEL_SUGGESTION,
        )
        state.nodes_by_id[new_node_id] = new_node
        state.child_ids_by_parent[new_node_id] = []
        state.spans_by_node[new_node_id] = part.source_spans
        new_node_ids.append(new_node_id)

    siblings[target_index : target_index + 1] = new_node_ids
    del state.nodes_by_id[target.structure_node_id]
    del state.child_ids_by_parent[target.structure_node_id]
    del state.spans_by_node[target.structure_node_id]


def _apply_boundary_resegment(
    state: _TreeState,
    operation: BoundaryResegmentStructureOperationV1,
    *,
    source_text_by_ref: dict[StructureSourceRef, str],
    current: EffectiveStructureSnapshotV1,
) -> None:
    leading = state.nodes_by_id.get(operation.preceding_structure_node_id)
    trailing = state.nodes_by_id.get(operation.following_structure_node_id)
    if leading is None or trailing is None:
        _reject(
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
            "boundary resegment endpoints must exist",
        )
    allowed_kinds = {
        StructureNodeKind.CONTENT,
        StructureNodeKind.CONTINUATION,
        StructureNodeKind.CLAUSE,
    }
    if leading.kind not in allowed_kinds or trailing.kind not in allowed_kinds:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary resegment supports content-like leaf nodes only",
        )
    if (
        leading.parent_structure_node_id is None
        or leading.parent_structure_node_id
        != trailing.parent_structure_node_id
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary resegment requires one non-root sibling scope",
        )
    if (
        state.child_ids_by_parent[leading.structure_node_id]
        or state.child_ids_by_parent[trailing.structure_node_id]
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary resegment requires two leaf nodes",
        )
    siblings = state.child_ids_by_parent[leading.parent_structure_node_id]
    leading_index = siblings.index(leading.structure_node_id)
    if (
        leading_index + 1 >= len(siblings)
        or siblings[leading_index + 1] != trailing.structure_node_id
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary resegment requires adjacent siblings in leading order",
        )
    touching_relations = tuple(
        relation
        for relation in state.relations_by_id.values()
        if (
            relation.source_structure_node_id
            in {leading.structure_node_id, trailing.structure_node_id}
            or relation.target_structure_node_id
            in {leading.structure_node_id, trailing.structure_node_id}
        )
        and relation.kind is not StructureRelationKind.CONTAINS
    )
    if touching_relations:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "boundary resegment requires endpoints without non-parent relations",
        )
    _require_node_removal_safe(state, leading.structure_node_id)
    _require_node_removal_safe(state, trailing.structure_node_id)

    leading_spans = state.spans_by_node[leading.structure_node_id]
    trailing_spans = state.spans_by_node[trailing.structure_node_id]
    if len(leading_spans) != 1 or len(trailing_spans) != 1:
        _reject(
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
            "boundary resegment requires one exact source span per endpoint",
        )
    leading_span = leading_spans[0]
    trailing_span = trailing_spans[0]
    source_ref = leading_span.source_ref
    if (
        source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
        or trailing_span.source_ref != source_ref
        or leading_span.end_offset != trailing_span.start_offset
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
            "boundary resegment requires contiguous spans over one SourceNode",
        )
    source_text = source_text_by_ref.get(source_ref)
    if source_text is None:
        _reject(
            StructureRevisionRejectionCode.INVALID_SOURCE_REF,
            "boundary resegment references unknown SourceIR evidence",
        )
    split_offset = operation.new_boundary_offset
    if (
        split_offset <= leading_span.start_offset
        or split_offset >= trailing_span.end_offset
        or split_offset == leading_span.end_offset
        or split_offset > len(source_text)
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
            "boundary resegment offset must move inside the exact combined range",
        )

    replacement_specs = (
        (
            leading,
            StructureSourceSpanV1(
                source_ref=source_ref,
                start_offset=leading_span.start_offset,
                end_offset=split_offset,
            ),
        ),
        (
            trailing,
            StructureSourceSpanV1(
                source_ref=source_ref,
                start_offset=split_offset,
                end_offset=trailing_span.end_offset,
            ),
        ),
    )
    replaced_ids = {
        leading.structure_node_id,
        trailing.structure_node_id,
    }
    new_nodes: list[StructureNode] = []
    new_ids: list[str] = []
    for original, span in replacement_specs:
        identity_key = _source_span_identity_key(
            source_ir_id=current.source_ir_id,
            source_spans=(span,),
        )
        node_id = stable_structure_node_id(
            source_ir_id=current.source_ir_id,
            resolver_id=current.resolver_id,
            resolver_version=current.resolver_version,
            kind=original.kind,
            identity_key=identity_key,
        )
        if (
            node_id in new_ids
            or (
                node_id in state.nodes_by_id
                and node_id not in replaced_ids
            )
        ):
            _reject(
                StructureRevisionRejectionCode.INVALID_OPERATION,
                "boundary resegment would duplicate a source-span identity",
            )
        new_nodes.append(
            StructureNode(
                structure_node_id=node_id,
                identity_key=identity_key,
                kind=original.kind,
                parent_structure_node_id=original.parent_structure_node_id,
                source_order=original.source_order,
                source_refs=(source_ref,),
                boundary_basis=StructureBoundaryBasis.MODEL_SUGGESTION,
            )
        )
        new_ids.append(node_id)

    siblings[leading_index : leading_index + 2] = new_ids
    for original in (leading, trailing):
        del state.nodes_by_id[original.structure_node_id]
        del state.child_ids_by_parent[original.structure_node_id]
        del state.spans_by_node[original.structure_node_id]
    for node, (_, span) in zip(new_nodes, replacement_specs):
        state.nodes_by_id[node.structure_node_id] = node
        state.child_ids_by_parent[node.structure_node_id] = []
        state.spans_by_node[node.structure_node_id] = (span,)


def _apply_continuation_merge(
    state: _TreeState,
    operation: ContinuationMergeStructureOperationV1,
) -> None:
    primary = state.nodes_by_id.get(operation.primary_structure_node_id)
    continuation = state.nodes_by_id.get(operation.continuation_structure_node_id)
    if primary is None or continuation is None:
        _reject(
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
            "continuation merge endpoints must exist",
        )
    if primary.kind not in {
        StructureNodeKind.CONTENT,
        StructureNodeKind.CONTINUATION,
    } or continuation.kind not in {
        StructureNodeKind.CONTENT,
        StructureNodeKind.CONTINUATION,
    }:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "continuation merge currently supports content-like nodes",
        )
    if (
        primary.parent_structure_node_id is None
        or primary.parent_structure_node_id != continuation.parent_structure_node_id
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "continuation merge requires one sibling scope",
        )
    if (
        state.child_ids_by_parent[primary.structure_node_id]
        or state.child_ids_by_parent[continuation.structure_node_id]
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "continuation merge currently requires two leaf nodes",
        )
    siblings = state.child_ids_by_parent[primary.parent_structure_node_id]
    primary_index = siblings.index(primary.structure_node_id)
    if (
        primary_index + 1 >= len(siblings)
        or siblings[primary_index + 1] != continuation.structure_node_id
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "continuation merge requires adjacent logical siblings in source order",
        )
    merge_relations = tuple(
        relation
        for relation in state.relations_by_id.values()
        if relation.kind is StructureRelationKind.CONTINUES
        and relation.source_structure_node_id == primary.structure_node_id
        and relation.target_structure_node_id == continuation.structure_node_id
    )
    if len(merge_relations) != 1:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "continuation merge requires the exact CONTINUES relation",
        )
    touching_non_parent = tuple(
        relation
        for relation in state.relations_by_id.values()
        if (
            relation.source_structure_node_id == continuation.structure_node_id
            or relation.target_structure_node_id == continuation.structure_node_id
        )
        and relation.kind is not StructureRelationKind.CONTAINS
        and relation not in merge_relations
    )
    if touching_non_parent:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "continuation merge cannot silently redirect unrelated relations",
        )
    _require_node_removal_safe(state, continuation.structure_node_id)

    combined_spans = _coalesce_adjacent_spans(
        (
            *state.spans_by_node[primary.structure_node_id],
            *state.spans_by_node[continuation.structure_node_id],
        )
    )
    source_refs = tuple(dict.fromkeys(span.source_ref for span in combined_spans))
    semantic_ref_set = {
        *primary.effective_semantic_source_refs,
        *continuation.effective_semantic_source_refs,
    }
    semantic_source_refs = tuple(
        source_ref for source_ref in source_refs if source_ref in semantic_ref_set
    )
    state.nodes_by_id[primary.structure_node_id] = _replace_node(
        primary,
        source_refs=source_refs,
        semantic_source_refs=(
            None if semantic_source_refs == source_refs else semantic_source_refs
        ),
    )
    state.spans_by_node[primary.structure_node_id] = combined_spans
    siblings.pop(primary_index + 1)
    del state.nodes_by_id[continuation.structure_node_id]
    del state.child_ids_by_parent[continuation.structure_node_id]
    del state.spans_by_node[continuation.structure_node_id]
    state.relations_by_id = {
        relation_id: relation
        for relation_id, relation in state.relations_by_id.items()
        if (
            relation.source_structure_node_id != continuation.structure_node_id
            and relation.target_structure_node_id != continuation.structure_node_id
        )
    }


def _apply_add_relation(
    state: _TreeState,
    operation: AddStructureRelationOperationV1,
    *,
    source_text_by_ref: dict[StructureSourceRef, str],
    current: EffectiveStructureSnapshotV1,
) -> None:
    if (
        operation.source_structure_node_id not in state.nodes_by_id
        or operation.target_structure_node_id not in state.nodes_by_id
    ):
        _reject(
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
            "relation endpoints must exist in the effective tree",
        )
    _require_source_refs_known(operation.source_refs, source_text_by_ref)
    relation_id = stable_structure_relation_id(
        source_ir_id=current.source_ir_id,
        resolver_id=current.resolver_id,
        resolver_version=current.resolver_version,
        kind=operation.kind,
        source_structure_node_id=operation.source_structure_node_id,
        target_structure_node_id=operation.target_structure_node_id,
        source_refs=operation.source_refs,
        option_state=operation.option_state,
    )
    if relation_id in state.relations_by_id:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "the effective tree already contains this relation",
        )
    state.relations_by_id[relation_id] = StructureRelation(
        relation_id=relation_id,
        kind=operation.kind,
        source_structure_node_id=operation.source_structure_node_id,
        target_structure_node_id=operation.target_structure_node_id,
        source_refs=operation.source_refs,
        option_state=operation.option_state,
    )


def _apply_remove_relation(
    state: _TreeState,
    operation: RemoveStructureRelationOperationV1,
) -> None:
    relation = state.relations_by_id.get(operation.relation_id)
    if relation is None:
        _reject(
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
            "remove_relation must reference an existing relation",
        )
    if relation.kind is StructureRelationKind.CONTAINS:
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "contains changes must use the re-parent operation",
        )
    del state.relations_by_id[operation.relation_id]


def _normalize_and_validate_state(
    state: _TreeState,
    *,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
) -> tuple[
    tuple[StructureNode, ...],
    tuple[StructureRelation, ...],
    tuple[StructureNodeSourceSpansV1, ...],
]:
    roots = tuple(
        node
        for node in state.nodes_by_id.values()
        if node.parent_structure_node_id is None
    )
    if len(roots) != 1:
        _reject(
            StructureRevisionRejectionCode.GRAPH_CYCLE,
            "effective Structure must contain exactly one root",
        )
    root = roots[0]
    ordered_node_ids: list[str] = []
    active: set[str] = set()
    completed: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in active:
            _reject(
                StructureRevisionRejectionCode.GRAPH_CYCLE,
                "effective Structure parent graph must be acyclic",
            )
        if node_id in completed:
            _reject(
                StructureRevisionRejectionCode.INVALID_OPERATION,
                "one effective node cannot appear in multiple sibling scopes",
            )
        active.add(node_id)
        ordered_node_ids.append(node_id)
        children = state.child_ids_by_parent.get(node_id)
        if children is None or len(children) != len(set(children)):
            _reject(
                StructureRevisionRejectionCode.INVALID_OPERATION,
                "effective sibling scopes must exist and contain unique nodes",
            )
        for child_id in children:
            child = state.nodes_by_id.get(child_id)
            if child is None:
                _reject(
                    StructureRevisionRejectionCode.DANGLING_ENDPOINT,
                    "effective sibling scope contains a missing node",
                )
            if child.parent_structure_node_id != node_id:
                _reject(
                    StructureRevisionRejectionCode.INVALID_OPERATION,
                    "effective sibling scope must match each node parent",
                )
            visit(child_id)
        active.remove(node_id)
        completed.add(node_id)

    visit(root.structure_node_id)
    if completed != set(state.nodes_by_id):
        _reject(
            StructureRevisionRejectionCode.GRAPH_CYCLE,
            "every effective node must be reachable from the document root",
        )

    nodes = tuple(
        _replace_node(state.nodes_by_id[node_id], source_order=source_order)
        for source_order, node_id in enumerate(ordered_node_ids)
    )
    state.nodes_by_id = {node.structure_node_id: node for node in nodes}
    non_contains_relations = tuple(
        relation
        for relation in state.relations_by_id.values()
        if relation.kind is not StructureRelationKind.CONTAINS
    )
    contains_relations = tuple(
        _contains_relation(
            node,
            source_ir_id=source_ir.source_ir_id,
            resolver_id=base_tree.resolver_id,
            resolver_version=base_tree.resolver_version,
        )
        for node in nodes
        if node.parent_structure_node_id is not None
    )
    relations = tuple(
        sorted((*contains_relations, *non_contains_relations), key=_relation_sort_key)
    )
    state.relations_by_id = {relation.relation_id: relation for relation in relations}
    node_source_spans = tuple(
        StructureNodeSourceSpansV1(
            structure_node_id=node.structure_node_id,
            source_spans=state.spans_by_node[node.structure_node_id],
        )
        for node in nodes
    )
    _validate_graph_with_existing_contract(
        nodes=nodes,
        relations=relations,
        state=state,
        base_tree=base_tree,
    )
    _validate_source_span_catalog(
        nodes=nodes,
        node_source_spans=node_source_spans,
        source_ir=source_ir,
    )
    return nodes, relations, node_source_spans


def _validate_graph_with_existing_contract(
    *,
    nodes: tuple[StructureNode, ...],
    relations: tuple[StructureRelation, ...],
    state: _TreeState,
    base_tree: StructureTreeSnapshot,
) -> None:
    try:
        StructureTreeSnapshot(
            structure_snapshot_id=base_tree.structure_snapshot_id,
            source_ir_id=base_tree.source_ir_id,
            resolver_id=base_tree.resolver_id,
            resolver_version=base_tree.resolver_version,
            source_node_ids=base_tree.source_node_ids,
            source_cell_ids=base_tree.source_cell_ids,
            visual_text_observation_ids=base_tree.visual_text_observation_ids,
            visual_table_cell_ids=base_tree.visual_table_cell_ids,
            nodes=nodes,
            relations=relations,
            document_language_policy=state.document_language_policy,
            bilingual_counterpart_groups=state.bilingual_counterpart_groups,
            bilingual_mismatch_signals=state.bilingual_mismatch_signals,
            visual_table_cell_text_bindings=state.visual_table_cell_text_bindings,
            findings=state.findings,
        )
    except ValidationError as exc:
        message = str(exc)
        code = (
            StructureRevisionRejectionCode.GRAPH_CYCLE
            if "acyclic" in message or "cycle" in message
            else StructureRevisionRejectionCode.DANGLING_ENDPOINT
            if "reference a structure node" in message
            or "must reference a node" in message
            else StructureRevisionRejectionCode.INVALID_OPERATION
        )
        raise StructureRevisionRejected(
            code,
            f"effective Structure graph is invalid: {message}",
        ) from exc


def _validate_source_span_catalog(
    *,
    nodes: tuple[StructureNode, ...],
    node_source_spans: tuple[StructureNodeSourceSpansV1, ...],
    source_ir: SourceIRSnapshot,
) -> None:
    source_text_by_ref = _source_text_catalog(source_ir)
    bindings_by_id = {
        binding.structure_node_id: binding for binding in node_source_spans
    }
    for node in nodes:
        spans = bindings_by_id[node.structure_node_id].source_spans
        if node.parent_structure_node_id is None:
            if spans:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "the document root cannot own source intervals",
                )
            continue
        if not spans:
            _reject(
                StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                "every non-root effective node requires source intervals",
            )
        observed_refs = tuple(dict.fromkeys(span.source_ref for span in spans))
        if observed_refs != node.source_refs:
            _reject(
                StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                "effective intervals must bind every node source ref in order",
            )
        closed_refs: set[StructureSourceRef] = set()
        active_ref: StructureSourceRef | None = None
        previous_end_by_ref: dict[StructureSourceRef, int] = {}
        for span in spans:
            text = source_text_by_ref.get(span.source_ref)
            if text is None:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_REF,
                    "effective interval references unknown SourceIR evidence",
                )
            if span.end_offset > len(text):
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "effective interval exceeds its immutable source text",
                )
            if active_ref is not None and span.source_ref != active_ref:
                closed_refs.add(active_ref)
            if span.source_ref in closed_refs:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "one source ref must occupy one contiguous span group",
                )
            previous_end = previous_end_by_ref.get(span.source_ref)
            if previous_end is not None and span.start_offset < previous_end:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "effective source intervals must not overlap",
                )
            if span.start_offset == span.end_offset and text:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "non-empty source text cannot be represented by an empty interval",
                )
            previous_end_by_ref[span.source_ref] = span.end_offset
            active_ref = span.source_ref


def _validate_effective_snapshot(
    snapshot: EffectiveStructureSnapshotV1,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
) -> None:
    if (
        snapshot.source_ir_id != source_ir.source_ir_id
        or snapshot.source_sha256 != source_ir.source_sha256
        or snapshot.source_ir_canonical_sha256
        != _stable_source_ir_canonical_sha256(source_ir)
        or snapshot.base_structure_snapshot_id != base_tree.structure_snapshot_id
        or snapshot.base_structure_canonical_sha256 != canonical_model_sha256(base_tree)
    ):
        _reject(
            StructureRevisionRejectionCode.SOURCE_BINDING_MISMATCH,
            "effective snapshot is not bound to the supplied SourceIR and base tree",
        )
    state = _state_from_effective(snapshot)
    nodes, relations, spans = _normalize_and_validate_state(
        state,
        source_ir=source_ir,
        base_tree=base_tree,
    )
    if (
        nodes != snapshot.nodes
        or relations != snapshot.relations
        or spans != snapshot.node_source_spans
    ):
        _reject(
            StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
            "effective snapshot must use canonical tree ordering",
        )


def _validate_split_partition(
    original_spans: tuple[StructureSourceSpanV1, ...],
    operation: BoundarySplitStructureOperationV1,
    *,
    source_text_by_ref: dict[StructureSourceRef, str],
) -> None:
    if not original_spans:
        _reject(
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
            "boundary split target has no source intervals",
        )
    original_index = 0
    cursor = original_spans[0].start_offset
    for part in operation.parts:
        previous_part_ref: StructureSourceRef | None = None
        for span in part.source_spans:
            if span.source_ref == previous_part_ref:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "one split part must use a canonical maximal interval per source ref",
                )
            text = source_text_by_ref.get(span.source_ref)
            if text is None:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_REF,
                    "boundary split references unknown SourceIR evidence",
                )
            if span.end_offset > len(text):
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "boundary split interval exceeds immutable source text",
                )
            if original_index >= len(original_spans):
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "boundary split intervals exceed target coverage",
                )
            original = original_spans[original_index]
            if (
                span.source_ref != original.source_ref
                or span.start_offset != cursor
                or span.end_offset > original.end_offset
            ):
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "boundary split must exactly cover without gaps or overlap",
                )
            cursor = span.end_offset
            if cursor == original.end_offset:
                original_index += 1
                if original_index < len(original_spans):
                    cursor = original_spans[original_index].start_offset
            previous_part_ref = span.source_ref
    if original_index != len(original_spans):
        _reject(
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
            "boundary split must cover the complete target source range",
        )


def _coalesce_adjacent_spans(
    spans: tuple[StructureSourceSpanV1, ...],
) -> tuple[StructureSourceSpanV1, ...]:
    result: list[StructureSourceSpanV1] = []
    for span in spans:
        if result and result[-1].source_ref == span.source_ref:
            previous = result[-1]
            if previous.end_offset != span.start_offset:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "same-source continuation intervals must be exactly adjacent",
                )
            result[-1] = StructureSourceSpanV1(
                source_ref=span.source_ref,
                start_offset=previous.start_offset,
                end_offset=span.end_offset,
            )
        else:
            if span.source_ref in {item.source_ref for item in result}:
                _reject(
                    StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
                    "continuation source refs must remain in physical source order",
                )
            result.append(span)
    return tuple(result)


def _require_node_removal_safe(state: _TreeState, node_id: str) -> None:
    if (
        any(finding.structure_node_id == node_id for finding in state.findings)
        or any(
            node_id in group.member_structure_node_ids
            for group in state.bilingual_counterpart_groups
        )
        or any(
            binding.table_structure_node_id == node_id
            for binding in state.visual_table_cell_text_bindings
        )
    ):
        _reject(
            StructureRevisionRejectionCode.INVALID_OPERATION,
            "node removal would orphan an existing source-bound Structure sidecar",
        )


def _require_proposal_binding(
    proposal: StructurePatchProposalV1,
    current: EffectiveStructureSnapshotV1,
) -> None:
    if (
        proposal.source_ir_id != current.source_ir_id
        or proposal.source_sha256 != current.source_sha256
        or proposal.source_ir_canonical_sha256 != current.source_ir_canonical_sha256
        or proposal.base_structure_snapshot_id != current.base_structure_snapshot_id
        or proposal.base_structure_canonical_sha256
        != current.base_structure_canonical_sha256
    ):
        _reject(
            StructureRevisionRejectionCode.SOURCE_BINDING_MISMATCH,
            "proposal source/base binding does not match the current document",
        )
    if (
        proposal.expected_revision_number != current.revision_number
        or proposal.expected_effective_snapshot_id != current.effective_snapshot_id
    ):
        _reject(
            StructureRevisionRejectionCode.STALE_BASE_REVISION,
            "proposal optimistic base revision is stale",
        )


def _require_patch_binding(
    patch: StructurePatchV1,
    current: EffectiveStructureSnapshotV1,
) -> None:
    if (
        patch.source_ir_id != current.source_ir_id
        or patch.source_sha256 != current.source_sha256
        or patch.source_ir_canonical_sha256 != current.source_ir_canonical_sha256
        or patch.base_structure_snapshot_id != current.base_structure_snapshot_id
        or patch.base_structure_canonical_sha256
        != current.base_structure_canonical_sha256
    ):
        _reject(
            StructureRevisionRejectionCode.SOURCE_BINDING_MISMATCH,
            "patch source/base binding does not match the current document",
        )
    if (
        patch.expected_revision_number != current.revision_number
        or patch.expected_effective_snapshot_id != current.effective_snapshot_id
    ):
        _reject(
            StructureRevisionRejectionCode.STALE_BASE_REVISION,
            "patch optimistic base revision is stale",
        )


def _require_revision_lineage(
    revision: StructureRevisionV1,
    current: EffectiveStructureSnapshotV1,
    expected_revision_number: int,
) -> None:
    if (
        revision.revision_number != expected_revision_number
        or revision.parent_revision_id != current.head_revision_id
        or revision.previous_effective_snapshot_id != current.effective_snapshot_id
        or revision.previous_effective_tree_sha256 != current.effective_tree_sha256
        or revision.previous_revision_chain_sha256 != current.revision_chain_sha256
        or revision.source_ir_id != current.source_ir_id
        or revision.source_sha256 != current.source_sha256
        or revision.source_ir_canonical_sha256 != current.source_ir_canonical_sha256
        or revision.base_structure_snapshot_id != current.base_structure_snapshot_id
        or revision.base_structure_canonical_sha256
        != current.base_structure_canonical_sha256
    ):
        _reject(
            StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
            "revision lineage does not extend the current effective head",
        )


def _validate_base_inputs(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
) -> tuple[str, str]:
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(base_tree, StructureTreeSnapshot):
        raise TypeError("base_tree must be a StructureTreeSnapshot")
    try:
        validate_structure_tree_against_source_ir(base_tree, source_ir)
    except StructureResolutionRejected as exc:
        raise StructureRevisionRejected(
            StructureRevisionRejectionCode.BASE_TREE_MISMATCH,
            "the deterministic base StructureTree failed its existing validator",
        ) from exc
    return (
        _stable_source_ir_canonical_sha256(source_ir),
        canonical_model_sha256(base_tree),
    )


def _stable_source_ir_canonical_sha256(source_ir: SourceIRSnapshot) -> str:
    """Bind immutable source facts without the per-build observation timestamp."""

    payload = source_ir.model_dump(
        mode="json",
        exclude={"created_at"},
    )
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _source_text_catalog(
    source_ir: SourceIRSnapshot,
) -> dict[StructureSourceRef, str]:
    catalog: dict[StructureSourceRef, str] = {}
    for node in source_ir.nodes:
        catalog[
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=node.source_node_id,
            )
        ] = node.raw_text
        if node.table_payload is not None:
            for cell in node.table_payload.cells:
                catalog[
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=cell.cell_id,
                    )
                ] = cell.raw_text
    for visual_snapshot in source_ir.visual_text_snapshots:
        for observation in visual_snapshot.observations:
            catalog[
                StructureSourceRef(
                    kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                    source_id=observation.observation_id,
                )
            ] = observation.raw_text
    return catalog


def _require_source_refs_known(
    refs: tuple[StructureSourceRef, ...],
    source_text_by_ref: dict[StructureSourceRef, str],
) -> None:
    if any(source_ref not in source_text_by_ref for source_ref in refs):
        _reject(
            StructureRevisionRejectionCode.INVALID_SOURCE_REF,
            "operation contains a source ref absent from the immutable SourceIR",
        )


def _state_from_effective(snapshot: EffectiveStructureSnapshotV1) -> _TreeState:
    child_ids_by_parent = {node.structure_node_id: [] for node in snapshot.nodes}
    for node in snapshot.nodes:
        if node.parent_structure_node_id is not None:
            child_ids_by_parent[node.parent_structure_node_id].append(
                node.structure_node_id
            )
    return _TreeState(
        nodes_by_id={node.structure_node_id: node for node in snapshot.nodes},
        child_ids_by_parent=child_ids_by_parent,
        relations_by_id={
            relation.relation_id: relation for relation in snapshot.relations
        },
        spans_by_node={
            binding.structure_node_id: binding.source_spans
            for binding in snapshot.node_source_spans
        },
        document_language_policy=snapshot.document_language_policy,
        bilingual_counterpart_groups=snapshot.bilingual_counterpart_groups,
        bilingual_mismatch_signals=snapshot.bilingual_mismatch_signals,
        visual_table_cell_text_bindings=snapshot.visual_table_cell_text_bindings,
        findings=snapshot.findings,
    )


def _contains_relation(
    node: StructureNode,
    *,
    source_ir_id: str,
    resolver_id: str,
    resolver_version: str,
) -> StructureRelation:
    assert node.parent_structure_node_id is not None
    relation_id = stable_structure_relation_id(
        source_ir_id=source_ir_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        kind=StructureRelationKind.CONTAINS,
        source_structure_node_id=node.parent_structure_node_id,
        target_structure_node_id=node.structure_node_id,
        source_refs=node.source_refs,
    )
    return StructureRelation(
        relation_id=relation_id,
        kind=StructureRelationKind.CONTAINS,
        source_structure_node_id=node.parent_structure_node_id,
        target_structure_node_id=node.structure_node_id,
        source_refs=node.source_refs,
    )


def _replace_node(node: StructureNode, **updates: object) -> StructureNode:
    payload = node.model_dump(mode="python")
    payload.update(updates)
    return StructureNode.model_validate(payload)


def _source_span_identity_key(
    *,
    source_ir_id: str,
    source_spans: tuple[StructureSourceSpanV1, ...],
) -> str:
    payload = json.dumps(
        {
            "schema_version": "structure-boundary-identity/v1",
            "source_ir_id": source_ir_id,
            "source_spans": [span.model_dump(mode="json") for span in source_spans],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"model:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _calculate_impact(
    before: EffectiveStructureSnapshotV1,
    after_nodes: tuple[StructureNode, ...],
    after_relations: tuple[StructureRelation, ...],
    *,
    seed_node_ids: tuple[str, ...],
) -> StructureImpactV1:
    before_nodes = {node.structure_node_id: node for node in before.nodes}
    after_nodes_by_id = {node.structure_node_id: node for node in after_nodes}
    direct_ids = set(seed_node_ids)
    for node_id in set(before_nodes) | set(after_nodes_by_id):
        if before_nodes.get(node_id) != after_nodes_by_id.get(node_id):
            direct_ids.add(node_id)

    before_relations = {relation.relation_id: relation for relation in before.relations}
    after_relations_by_id = {
        relation.relation_id: relation for relation in after_relations
    }
    changed_relation_ids = {
        relation_id
        for relation_id in set(before_relations) | set(after_relations_by_id)
        if before_relations.get(relation_id) != after_relations_by_id.get(relation_id)
    }
    for relation_id in changed_relation_ids:
        relation = (
            after_relations_by_id.get(relation_id) or before_relations[relation_id]
        )
        direct_ids.update(
            (
                relation.source_structure_node_id,
                relation.target_structure_node_id,
            )
        )

    affected_ids = set(direct_ids)
    for nodes_by_id in (before_nodes, after_nodes_by_id):
        children_by_parent: dict[str, list[str]] = {
            node_id: [] for node_id in nodes_by_id
        }
        for node in nodes_by_id.values():
            if node.parent_structure_node_id in children_by_parent:
                children_by_parent[node.parent_structure_node_id].append(
                    node.structure_node_id
                )
        for direct_id in tuple(direct_ids):
            node = nodes_by_id.get(direct_id)
            while node is not None and node.parent_structure_node_id is not None:
                affected_ids.add(node.parent_structure_node_id)
                node = nodes_by_id.get(node.parent_structure_node_id)
            pending = list(children_by_parent.get(direct_id, ()))
            while pending:
                descendant_id = pending.pop()
                if descendant_id in affected_ids:
                    continue
                affected_ids.add(descendant_id)
                pending.extend(children_by_parent.get(descendant_id, ()))

    affected_sections = {
        node_id
        for node_id in affected_ids
        if (
            (after_nodes_by_id.get(node_id) or before_nodes.get(node_id)) is not None
            and (after_nodes_by_id.get(node_id) or before_nodes[node_id]).kind
            is StructureNodeKind.SECTION
        )
    }
    return StructureImpactV1(
        direct_structure_node_ids=tuple(sorted(direct_ids)),
        affected_structure_node_ids=tuple(sorted(affected_ids)),
        affected_section_node_ids=tuple(sorted(affected_sections)),
        changed_relation_ids=tuple(sorted(changed_relation_ids)),
    )


def _relation_sort_key(
    relation: StructureRelation,
) -> tuple[str, str, str, str]:
    return (
        relation.kind.value,
        relation.source_structure_node_id,
        relation.target_structure_node_id,
        relation.relation_id,
    )


def _require_source_unchanged(
    source_ir: SourceIRSnapshot,
    expected_canonical_sha256: str,
) -> None:
    if canonical_model_sha256(source_ir) != expected_canonical_sha256:
        _reject(
            StructureRevisionRejectionCode.SOURCE_BINDING_MISMATCH,
            "Structure revision attempted to mutate immutable SourceIR",
        )


def _reject(
    code: StructureRevisionRejectionCode,
    message: str,
) -> None:
    raise StructureRevisionRejected(code, message)


__all__ = [
    "apply_structure_patch",
    "calculate_structure_impact",
    "create_structure_patch_proposal",
    "get_base_effective_structure",
    "undo_structure_revision",
    "validate_structure_patch_proposal",
    "validate_structure_revision_chain",
]
