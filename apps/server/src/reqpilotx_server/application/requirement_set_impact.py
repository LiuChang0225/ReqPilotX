from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from reqpilotx_server.domain.requirement_set_impact import (
    REQUIREMENT_SET_IMPACT_ALGORITHM_VERSION,
    RequirementSetImpactInputNodeV1,
    RequirementSetImpactNodeV1,
    RequirementSetImpactOutcome,
    RequirementSetImpactReason,
    RequirementSetImpactReceiptV1,
    canonical_requirement_set_impact_receipt_sha256,
    stable_requirement_set_impact_receipt_id,
)

ImpactInput = RequirementSetImpactInputNodeV1 | Mapping[str, Any]


def compare_requirement_set_impact(
    *,
    project_id: str,
    document_id: str,
    old_requirement_set_id: str,
    new_requirement_set_id: str,
    old_tree_sha256: str,
    new_tree_sha256: str,
    matrix_state_schema_version: str,
    old_matrix_state_sha256: str,
    new_matrix_state_sha256: str,
    old_nodes: Iterable[ImpactInput],
    new_nodes: Iterable[ImpactInput],
    algorithm_version: str = REQUIREMENT_SET_IMPACT_ALGORITHM_VERSION,
) -> RequirementSetImpactReceiptV1:
    """Hash-join normalized records in O(n), then emit canonical sorted output.

    The caller owns construction of both hashes.  This comparator never reads
    a tree, database, provider, or AI configuration.  Set-local requirement
    identities are returned for later persistence mapping but are deliberately
    not part of equivalence: the supplied hashes are the sole proof boundary.
    """

    old_by_id = _index_nodes(old_nodes, side="old")
    new_by_id = _index_nodes(new_nodes, side="new")
    node_results = tuple(
        _compare_node(old_by_id.get(node_id), new_by_id.get(node_id))
        for node_id in sorted(old_by_id.keys() | new_by_id.keys())
    )
    reason_counts: dict[str, int] = {}
    for item in node_results:
        for reason in item.reasons:
            reason_counts[reason.value] = reason_counts.get(reason.value, 0) + 1
    reason_counts = dict(sorted(reason_counts.items()))

    fields: dict[str, Any] = {
        "algorithm_version": algorithm_version,
        "receipt_id": "requirement-set-impact-receipt-" + "0" * 32,
        "project_id": project_id,
        "document_id": document_id,
        "old_requirement_set_id": old_requirement_set_id,
        "new_requirement_set_id": new_requirement_set_id,
        "old_tree_sha256": old_tree_sha256,
        "new_tree_sha256": new_tree_sha256,
        "matrix_state_schema_version": matrix_state_schema_version,
        "old_matrix_state_sha256": old_matrix_state_sha256,
        "new_matrix_state_sha256": new_matrix_state_sha256,
        "node_results": node_results,
        "retained_result_node_ids": tuple(
            item.node_id
            for item in node_results
            if item.outcome is RequirementSetImpactOutcome.RETAINED
        ),
        "stale_result_node_ids": tuple(
            item.node_id
            for item in node_results
            if item.outcome is RequirementSetImpactOutcome.STALE
        ),
        "changed_node_ids": tuple(
            item.node_id for item in node_results if item.intrinsic_changed
        ),
        "new_node_ids": tuple(
            item.node_id
            for item in node_results
            if item.outcome is RequirementSetImpactOutcome.NEW
        ),
        "removed_node_ids": tuple(
            item.node_id
            for item in node_results
            if item.outcome is RequirementSetImpactOutcome.REMOVED
        ),
        "reason_counts": reason_counts,
        "payload_sha256": "0" * 64,
    }
    draft = RequirementSetImpactReceiptV1.model_construct(**fields)
    fields["receipt_id"] = stable_requirement_set_impact_receipt_id(draft)
    draft = RequirementSetImpactReceiptV1.model_construct(**fields)
    fields["payload_sha256"] = canonical_requirement_set_impact_receipt_sha256(
        draft
    )
    return RequirementSetImpactReceiptV1.model_validate(fields)


def _index_nodes(
    nodes: Iterable[ImpactInput],
    *,
    side: str,
) -> dict[str, RequirementSetImpactInputNodeV1]:
    indexed: dict[str, RequirementSetImpactInputNodeV1] = {}
    for raw in nodes:
        node = (
            raw
            if isinstance(raw, RequirementSetImpactInputNodeV1)
            else RequirementSetImpactInputNodeV1.model_validate(raw)
        )
        if node.node_id in indexed:
            raise ValueError(f"duplicate {side} impact node_id: {node.node_id}")
        indexed[node.node_id] = node
    return indexed


def _compare_node(
    old: RequirementSetImpactInputNodeV1 | None,
    new: RequirementSetImpactInputNodeV1 | None,
) -> RequirementSetImpactNodeV1:
    if old is None:
        assert new is not None
        return RequirementSetImpactNodeV1(
            node_id=new.node_id,
            old_requirement_id=None,
            new_requirement_id=new.requirement_id,
            old_intrinsic_sha256=None,
            new_intrinsic_sha256=new.intrinsic_sha256,
            old_analysis_input_sha256=None,
            new_analysis_input_sha256=new.analysis_input_sha256,
            outcome=RequirementSetImpactOutcome.NEW,
            reasons=(RequirementSetImpactReason.NODE_ADDED,),
            intrinsic_changed=True,
            source_result_metadata={},
        )
    if new is None:
        return RequirementSetImpactNodeV1(
            node_id=old.node_id,
            old_requirement_id=old.requirement_id,
            new_requirement_id=None,
            old_intrinsic_sha256=old.intrinsic_sha256,
            new_intrinsic_sha256=None,
            old_analysis_input_sha256=old.analysis_input_sha256,
            new_analysis_input_sha256=None,
            outcome=RequirementSetImpactOutcome.REMOVED,
            reasons=(RequirementSetImpactReason.NODE_REMOVED,),
            intrinsic_changed=True,
            source_result_metadata=(
                old.result_metadata if old.has_current_result else {}
            ),
        )

    intrinsic_proven = bool(old.intrinsic_sha256 and new.intrinsic_sha256)
    analysis_input_proven = bool(
        old.analysis_input_sha256 and new.analysis_input_sha256
    )
    intrinsic_changed = (
        not intrinsic_proven
        or old.intrinsic_sha256 != new.intrinsic_sha256
    )
    reasons: list[RequirementSetImpactReason] = []
    if intrinsic_proven and old.intrinsic_sha256 != new.intrinsic_sha256:
        reasons.append(RequirementSetImpactReason.INTRINSIC_CHANGED)
    if (
        analysis_input_proven
        and old.analysis_input_sha256 != new.analysis_input_sha256
    ):
        reasons.append(RequirementSetImpactReason.ANALYSIS_INPUT_CHANGED)
    if not intrinsic_proven or not analysis_input_proven:
        reasons.append(RequirementSetImpactReason.UNPROVEN_EQUIVALENCE)

    if not old.has_current_result:
        reasons.append(RequirementSetImpactReason.NO_PRIOR_RESULT)
        return RequirementSetImpactNodeV1(
            node_id=old.node_id,
            old_requirement_id=old.requirement_id,
            new_requirement_id=new.requirement_id,
            old_intrinsic_sha256=old.intrinsic_sha256,
            new_intrinsic_sha256=new.intrinsic_sha256,
            old_analysis_input_sha256=old.analysis_input_sha256,
            new_analysis_input_sha256=new.analysis_input_sha256,
            outcome=RequirementSetImpactOutcome.NO_PRIOR_RESULT,
            reasons=tuple(reasons),
            intrinsic_changed=intrinsic_changed,
            source_result_metadata={},
        )

    if not reasons:
        reasons.append(RequirementSetImpactReason.FULL_EQUIVALENCE)
        outcome = RequirementSetImpactOutcome.RETAINED
    else:
        outcome = RequirementSetImpactOutcome.STALE
    return RequirementSetImpactNodeV1(
        node_id=old.node_id,
        old_requirement_id=old.requirement_id,
        new_requirement_id=new.requirement_id,
        old_intrinsic_sha256=old.intrinsic_sha256,
        new_intrinsic_sha256=new.intrinsic_sha256,
        old_analysis_input_sha256=old.analysis_input_sha256,
        new_analysis_input_sha256=new.analysis_input_sha256,
        outcome=outcome,
        reasons=tuple(reasons),
        intrinsic_changed=intrinsic_changed,
        source_result_metadata=old.result_metadata,
    )


__all__ = ["compare_requirement_set_impact"]
