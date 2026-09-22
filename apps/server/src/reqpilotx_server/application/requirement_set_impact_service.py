from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from reqpilotx_server import db
from reqpilotx_server.adapters.persistence.requirement_matrix_states import (
    REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION,
    requirement_matrix_state_sha256,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction.requirement_tree_snapshots import (
    RequirementTreeSnapshotError,
    load_requirement_set_tree_snapshot,
)
from reqpilotx_server.application.requirement_matrix import (
    build_requirement_matrix_snapshot,
    select_matrix_analysis_requirements,
)
from reqpilotx_server.application.requirement_set_impact import compare_requirement_set_impact
from reqpilotx_server.domain.requirement_matrix import (
    MatrixAnalysisEligibility,
    MatrixNodeDisposition,
    MatrixNodeVisibility,
    MatrixSnapshotState,
    RequirementMatrixSnapshotV1,
)
from reqpilotx_server.domain.requirement_set_impact import (
    REQUIREMENT_SET_IMPACT_INPUT_NODE_SCHEMA_VERSION,
    RequirementSetImpactInputNodeV1,
    RequirementSetImpactReceiptV1,
)
from reqpilotx_server.services.ai_copilot import build_assessment_context

REQUIREMENT_ANALYSIS_SEMANTIC_CONTEXT_SCHEMA_VERSION = (
    "requirement-analysis-semantic-context/v1"
)
# Compatibility name for the persisted ``analysis_input_*`` columns.  The
# value intentionally names the narrower proof boundary: it is not a digest of
# knowledge documents, retrieval output, provider settings, or prompt bytes.
REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION = (
    REQUIREMENT_ANALYSIS_SEMANTIC_CONTEXT_SCHEMA_VERSION
)
REQUIREMENT_NODE_INTRINSIC_SCHEMA_VERSION = "requirement-node-intrinsic/v1"


def build_requirement_set_impact_receipt(
    *,
    project_id: str,
    document_id: str,
    old_requirement_set_id: str,
    new_requirement_set_id: str,
) -> RequirementSetImpactReceiptV1:
    """Compare semantic assessment contexts of two version-bound Matrix lenses.

    Requirement row IDs are local to one Requirement Set, so they can never be
    used as the join key.  The immutable Requirement Tree node ID is the stable
    identity; old and new semantic-context hashes prove that a previously
    published result still applies to the Requirement itself.  Provider/model/
    prompt provenance remains on the result row, while knowledge freshness is
    governed by the document's AI projection and knowledge-base version.
    """

    old_set = _require_impact_set(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=old_requirement_set_id,
    )
    new_set = _require_impact_set(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=new_requirement_set_id,
    )
    if str(new_set.get("supersedes_id") or "") != old_requirement_set_id:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_impact_lineage_invalid",
                "message": "影响比较只允许在直接相邻的需求版本之间执行。",
            },
        )

    old_matrix_state_sha256 = requirement_matrix_state_sha256(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=old_requirement_set_id,
    )
    new_matrix_state_sha256 = requirement_matrix_state_sha256(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=new_requirement_set_id,
    )

    old_matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        old_requirement_set_id,
    )
    new_matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        new_requirement_set_id,
    )
    if (
        old_matrix.state is not MatrixSnapshotState.READY
        or new_matrix.state is not MatrixSnapshotState.READY
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_impact_matrix_not_ready",
                "message": "版本结构无法验证，不能声称分析结果仍然有效。",
            },
        )

    try:
        old_tree = load_requirement_set_tree_snapshot(old_set)
        new_tree = load_requirement_set_tree_snapshot(new_set)
    except RequirementTreeSnapshotError as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_impact_tree_not_ready",
                "message": "版本结构无法验签，不能计算分析影响。",
            },
        ) from exc
    old_tree_nodes = {node.id: node for node in old_tree.nodes}
    new_tree_nodes = {node.id: node for node in new_tree.nodes}
    old_evidence_by_node = _requirement_evidence_by_node(old_tree)
    new_evidence_by_node = _requirement_evidence_by_node(new_tree)

    old_requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=old_requirement_set_id,
    )
    new_requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=new_requirement_set_id,
    )
    old_targets = select_matrix_analysis_requirements(
        old_matrix,
        old_requirements,
    )
    old_candidates = _all_impact_candidate_requirements(
        old_matrix,
        old_requirements,
    )
    new_candidates = _all_impact_candidate_requirements(
        new_matrix,
        new_requirements,
    )
    old_nodes = _impact_input_nodes(
        targets=old_candidates,
        tree_nodes=old_tree_nodes,
        evidence_by_node=old_evidence_by_node,
        current_result_node_ids={
            str(row.get("matrix_node_id") or "") for row in old_targets
        },
    )
    new_nodes = _impact_input_nodes(
        targets=new_candidates,
        tree_nodes=new_tree_nodes,
        evidence_by_node=new_evidence_by_node,
        current_result_node_ids=set(),
    )
    if (
        requirement_matrix_state_sha256(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=old_requirement_set_id,
        )
        != old_matrix_state_sha256
        or requirement_matrix_state_sha256(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=new_requirement_set_id,
        )
        != new_matrix_state_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_impact_state_changed",
                "message": "需求清单在影响计算期间已变化，请基于最新状态重试。",
            },
        )
    return compare_requirement_set_impact(
        project_id=project_id,
        document_id=document_id,
        old_requirement_set_id=old_requirement_set_id,
        new_requirement_set_id=new_requirement_set_id,
        old_tree_sha256=old_matrix.tree_sha256,
        new_tree_sha256=new_matrix.tree_sha256,
        matrix_state_schema_version=(
            REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
        ),
        old_matrix_state_sha256=old_matrix_state_sha256,
        new_matrix_state_sha256=new_matrix_state_sha256,
        old_nodes=old_nodes,
        new_nodes=new_nodes,
    )


def reconcile_requirement_set_impact_for_draft(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> dict[str, Any] | None:
    """Recompute and atomically apply affected-only status for one DRAFT."""

    target = _require_impact_set(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
    )
    source_requirement_set_id = str(target.get("supersedes_id") or "").strip()
    if (
        str(target.get("status") or "").upper() != "DRAFT"
        or not source_requirement_set_id
    ):
        return None
    from reqpilotx_server.adapters.persistence.requirement_set_impacts import (
        RequirementSetImpactStateChangedError,
        apply_requirement_set_impact_receipt,
    )

    result = None
    for attempt in range(3):
        try:
            receipt = build_requirement_set_impact_receipt(
                project_id=project_id,
                document_id=document_id,
                old_requirement_set_id=source_requirement_set_id,
                new_requirement_set_id=requirement_set_id,
            )
            result = apply_requirement_set_impact_receipt(
                receipt,
                analysis_input_schema_version=(
                    REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
                ),
            )
            break
        except RequirementSetImpactStateChangedError:
            if attempt == 2:
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "requirement_set_impact_state_changed",
                        "message": "需求清单持续变化，影响结果尚未对齐；请刷新后重试。",
                    },
                )
        except ApplicationError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if (
                detail.get("code") != "requirement_set_impact_state_changed"
                or attempt == 2
            ):
                raise
    if result is None:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_impact_state_changed",
                "message": "需求清单影响结果尚未对齐，请刷新后重试。",
            },
        )
    return {
        **result.receipt.model_dump(mode="json"),
        "created": result.created,
        "applied": result.applied,
        "retained_result_count": result.retained_count,
        "stale_result_count": result.stale_count,
        "cleared_result_count": result.cleared_count,
    }


def requirement_set_analysis_target_node_ids(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> tuple[str, ...]:
    """Return the canonical current Matrix analysis target identities."""

    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
    )
    if matrix.state is not MatrixSnapshotState.READY:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_impact_matrix_not_ready",
                "message": "版本结构无法验证，不能投影分析结果状态。",
            },
        )
    requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=requirement_set_id,
    )
    return tuple(
        sorted(
            str(row.get("matrix_node_id") or "")
            for row in select_matrix_analysis_requirements(
                matrix,
                requirements,
            )
        )
    )


def requirement_analysis_semantic_context_sha256(
    requirement_row: Mapping[str, Any],
) -> str:
    """Hash the Requirement-owned semantic assessment context.

    This deliberately excludes knowledge documents, retrieved chunks and the
    provider/profile/prompt execution identity.  Those inputs govern result
    provenance and document-level freshness; they are not needed to decide
    whether an already-published result applies to an unchanged Requirement.
    """

    context = build_assessment_context(dict(requirement_row))
    # These two values are Requirement-Set-local presentation identities.  The
    # remaining node identities come from the immutable tree and intentionally
    # stay in the proof because they describe hierarchy and adjacent context.
    context.pop("requirement_id", None)
    context.pop("req_code", None)
    return _canonical_sha256(
        {
            "schema_version": REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
            "assessment_context": context,
        }
    )


def requirement_analysis_input_sha256(
    requirement_row: Mapping[str, Any],
) -> str:
    """Compatibility alias for the semantic-context digest."""

    return requirement_analysis_semantic_context_sha256(requirement_row)


def requirement_node_intrinsic_sha256(
    requirement_row: Mapping[str, Any],
    *,
    tree_node: object | None = None,
    requirement_evidence: Mapping[str, Any] | None = None,
) -> str:
    """Hash semantics owned by one analysis node, excluding local row IDs.

    Parent placement is represented in the semantic-context hash instead. This
    separation lets a harmless structural move retain a result when the actual
    parent/adjacent context remains equivalent, while still recording content,
    classification, atomic-boundary and evidence changes as intrinsic changes.
    """

    return _canonical_sha256(
        {
            "schema_version": REQUIREMENT_NODE_INTRINSIC_SCHEMA_VERSION,
            "matrix_node_id": str(requirement_row.get("matrix_node_id") or ""),
            "matrix_display_role": str(
                requirement_row.get("matrix_display_role") or ""
            ),
            "matrix_disposition": str(
                requirement_row.get("matrix_disposition") or ""
            ),
            "matrix_analysis_eligibility": str(
                requirement_row.get("matrix_analysis_eligibility") or ""
            ),
            "matrix_visibility": str(
                requirement_row.get("matrix_visibility") or ""
            ),
            "entry_kind": str(requirement_row.get("entry_kind") or "").upper(),
            "requirement_type": str(
                requirement_row.get("requirement_type") or ""
            ),
            "priority": str(requirement_row.get("priority") or ""),
            "text_relation": str(requirement_row.get("text_relation") or ""),
            "requirement_text": str(
                requirement_row.get("requirement_text") or ""
            ),
            "content_blocks": list(requirement_row.get("content_blocks") or []),
            "table_refs": list(requirement_row.get("table_refs") or []),
            "source_refs": list(requirement_row.get("source_refs") or []),
            "matrix_source_refs": list(
                requirement_row.get("matrix_source_refs") or []
            ),
            "matrix_table_record_id": str(
                requirement_row.get("matrix_table_record_id") or ""
            ),
            "matrix_logical_table_ids": list(
                requirement_row.get("matrix_logical_table_ids") or []
            ),
            "tree_classification": str(
                getattr(tree_node, "classification", "") or ""
            ),
            "tree_projection_role": str(
                getattr(tree_node, "projection_role", "") or ""
            ),
            "tree_semantic_state": str(
                getattr(tree_node, "semantic_state", "") or ""
            ),
            "table_evidence_binding_id": str(
                getattr(tree_node, "table_evidence_binding_id", "") or ""
            ),
            "requirement_evidence": dict(requirement_evidence or {}),
            "deterministic_rule_evidence": list(
                getattr(tree_node, "deterministic_rule_evidence", ()) or ()
            ),
        }
    )


def _impact_input_nodes(
    *,
    targets: list[dict[str, Any]],
    tree_nodes: Mapping[str, object],
    evidence_by_node: Mapping[str, Mapping[str, Any]],
    current_result_node_ids: set[str],
) -> tuple[RequirementSetImpactInputNodeV1, ...]:
    output: list[RequirementSetImpactInputNodeV1] = []
    for target in targets:
        node_id = str(target.get("matrix_node_id") or "").strip()
        requirement_id = str(target.get("id") or "").strip()
        if not node_id or not requirement_id:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "requirement_set_impact_target_identity_invalid",
                    "message": "分析范围存在无法绑定的稳定节点。",
                },
            )
        assessment = (
            db.get_assessment(requirement_id)
            if node_id in current_result_node_ids
            else None
        )
        has_current_result = _has_current_assessment_result(assessment)
        computed_analysis_input_sha256 = (
            requirement_analysis_semantic_context_sha256(target)
        )
        proven_analysis_input_sha256 = computed_analysis_input_sha256
        if has_current_result and (
            str(
                (assessment or {}).get("analysis_input_schema_version") or ""
            )
            != REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
            or str((assessment or {}).get("analysis_input_sha256") or "")
            != computed_analysis_input_sha256
        ):
            # A historical payload without matching semantic-context provenance
            # is readable, but cannot be claimed applicable. The comparator emits
            # UNPROVEN_EQUIVALENCE and carries it only as a stale reference.
            proven_analysis_input_sha256 = None
        output.append(
            RequirementSetImpactInputNodeV1(
                schema_version=(
                    REQUIREMENT_SET_IMPACT_INPUT_NODE_SCHEMA_VERSION
                ),
                node_id=node_id,
                requirement_id=requirement_id,
                has_current_result=has_current_result,
                intrinsic_sha256=requirement_node_intrinsic_sha256(
                    target,
                    tree_node=tree_nodes.get(node_id),
                    requirement_evidence=evidence_by_node.get(node_id),
                ),
                analysis_input_sha256=proven_analysis_input_sha256,
                result_metadata=(
                    _assessment_result_metadata(assessment)
                    if has_current_result
                    else {}
                ),
            )
        )
    return tuple(output)


def _requirement_evidence_by_node(tree: object) -> dict[str, dict[str, Any]]:
    bundle = getattr(tree, "requirement_evidence_bundle", None)
    binding_by_id = {
        str(binding.binding_id): binding
        for binding in (getattr(bundle, "requirement_bindings", ()) or ())
    }
    output: dict[str, dict[str, Any]] = {}
    for node in getattr(tree, "nodes", ()) or ():
        binding = binding_by_id.get(
            str(getattr(node, "requirement_evidence_binding_id", "") or "")
        )
        if binding is None:
            continue
        payload = binding.model_dump(mode="json")
        payload.pop("binding_id", None)
        payload.pop("requirement_entry_id", None)
        payload.pop("evidence_sha256", None)
        output[str(node.id)] = payload
    return output


def _all_impact_candidate_requirements(
    matrix: RequirementMatrixSnapshotV1,
    requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build AI context for every intrinsically decidable Matrix node.

    Disposition controls whether a result is currently used, not whether the
    stable node exists.  Including retained/excluded candidates lets a change
    from ANALYZE to another disposition bind the target row and explicitly
    mark its former result stale instead of leaving an unaudited clone behind.
    """

    actual_by_id = {node.node_id: node for node in matrix.nodes}
    projected_nodes = tuple(
        node.model_copy(
            update={
                "matrix_visibility": MatrixNodeVisibility.VISIBLE,
                "disposition": MatrixNodeDisposition.ANALYZE,
            }
        )
        if (
            node.requirement_id
            and node.analysis_eligibility is MatrixAnalysisEligibility.ELIGIBLE
        )
        else node
        for node in matrix.nodes
    )
    projected = matrix.model_copy(update={"nodes": projected_nodes})
    candidates = select_matrix_analysis_requirements(projected, requirements)
    for candidate in candidates:
        actual = actual_by_id[str(candidate.get("matrix_node_id") or "")]
        candidate["matrix_disposition"] = actual.disposition.value
        candidate["matrix_visibility"] = actual.matrix_visibility.value
    return candidates


def _has_current_assessment_result(
    assessment: Mapping[str, Any] | None,
) -> bool:
    row = assessment or {}
    if str(row.get("reference_status") or "CURRENT").upper() != "CURRENT":
        return False
    return any(
        str(row.get(field) or "").strip()
        for field in (
            "ai_decision",
            "compliance_status",
            "ai_reason",
            "final_decision",
        )
    )


def _assessment_result_metadata(
    assessment: Mapping[str, Any] | None,
) -> dict[str, Any]:
    row = assessment or {}
    return {
        "assessment_id": str(row.get("id") or ""),
        "assessment_updated_at": str(row.get("updated_at") or ""),
        "result_origin": str(row.get("result_origin") or "NATIVE").upper(),
        "source_assessment_id": str(row.get("source_assessment_id") or ""),
        "has_manual_decision": bool(
            str(row.get("final_decision") or "").strip()
        ),
    }


def _require_impact_set(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> dict[str, Any]:
    requirement_set = db.get_requirement_set(requirement_set_id)
    if (
        requirement_set is None
        or str(requirement_set.get("project_id") or "") != project_id
        or str(requirement_set.get("sor_document_id") or "") != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "requirement_set_impact_set_not_found",
                "message": "需求版本不属于当前项目和文档。",
            },
        )
    return requirement_set


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION",
    "REQUIREMENT_ANALYSIS_SEMANTIC_CONTEXT_SCHEMA_VERSION",
    "REQUIREMENT_NODE_INTRINSIC_SCHEMA_VERSION",
    "build_requirement_set_impact_receipt",
    "reconcile_requirement_set_impact_for_draft",
    "requirement_set_analysis_target_node_ids",
    "requirement_analysis_input_sha256",
    "requirement_analysis_semantic_context_sha256",
    "requirement_node_intrinsic_sha256",
]
