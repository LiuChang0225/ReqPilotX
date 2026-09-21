from __future__ import annotations

import hashlib
import json
from enum import Enum

from reqpilotx_server.application.extraction.adapt_semantic_unit_decisions import (
    ControlledSemanticUnitDecisionExecution,
)
from reqpilotx_server.application.extraction.adapt_semantic_unit_downstream import (
    ControlledSemanticUnitDownstreamExecution,
)
from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LlmRequirementModelPrediction,
    LlmRequirementPredictionNode,
    source_binding_sha256,
    stable_llm_requirement_prediction_id,
)
from reqpilotx_server.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from reqpilotx_server.domain.extraction.requirement_view import RequirementDecisionMode
from reqpilotx_server.domain.extraction.semantic_unit_decisions import (
    SEMANTIC_UNIT_DECISION_SCHEMA_VERSION,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure import StructureTreeSnapshot


CONTROLLED_SEMANTIC_UNIT_REVIEW_ADAPTER_VERSION = (
    "controlled-semantic-unit-review-adapter/v1"
)
CONTROLLED_SEMANTIC_UNIT_ASSEMBLY_CONTRACT_VERSION = (
    "controlled-semantic-unit-assembly/v1"
)


class ControlledSemanticUnitReviewRejectionCode(str, Enum):
    INPUT_MISMATCH = "controlled_semantic_unit_review_input_mismatch"
    UNSUPPORTED_LINEAGE = "controlled_semantic_unit_review_unsupported_lineage"
    INVALID_GROUNDING = "controlled_semantic_unit_review_invalid_grounding"
    INVALID_PREDICTION = "controlled_semantic_unit_review_invalid_prediction"


class ControlledSemanticUnitReviewRejected(RuntimeError):
    """Fail-closed controlled review-adapter error without source text."""

    def __init__(
        self,
        code: ControlledSemanticUnitReviewRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


def build_controlled_semantic_unit_review_prediction(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    decision_execution: ControlledSemanticUnitDecisionExecution,
    downstream: ControlledSemanticUnitDownstreamExecution,
) -> LlmRequirementModelPrediction:
    """Freeze the isolated multi-unit chain as an immutable review prediction.

    This adapter deliberately accepts only the already controlled unit-addressed
    path. It does not alter the production planner, batch boundary, provider wire,
    prompt, cache, serving state, or Requirement Set.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(tree, StructureTreeSnapshot):
        raise TypeError("tree must be a StructureTreeSnapshot")
    if not isinstance(
        decision_execution,
        ControlledSemanticUnitDecisionExecution,
    ):
        raise TypeError(
            "decision_execution must be a ControlledSemanticUnitDecisionExecution"
        )
    if not isinstance(downstream, ControlledSemanticUnitDownstreamExecution):
        raise TypeError(
            "downstream must be a ControlledSemanticUnitDownstreamExecution"
        )

    plan = decision_execution.plan.semantic_unit_plan
    if (
        downstream.semantic_unit_plan != plan
        or source_ir.source_ir_id != plan.source_ir_id
        or tree.source_ir_id != source_ir.source_ir_id
        or tree.structure_snapshot_id != plan.structure_snapshot_id
        or downstream.semantics.source_ir_id != source_ir.source_ir_id
        or downstream.semantics.structure_snapshot_id != tree.structure_snapshot_id
        or downstream.semantics.semantic_unit_plan_id != plan.semantic_unit_plan_id
        or downstream.assembly.semantic_unit_plan_id != plan.semantic_unit_plan_id
        or downstream.requirement_view.semantic_unit_plan_id
        != plan.semantic_unit_plan_id
    ):
        _reject(
            ControlledSemanticUnitReviewRejectionCode.INPUT_MISMATCH,
            "Controlled review inputs do not retain one unit-plan binding.",
        )

    unit_ids = plan.semantic_unit_ids
    annotations = downstream.semantics.annotations
    items = downstream.assembly.items
    requests = decision_execution.plan.requests
    decisions = decision_execution.decisions
    if (
        tuple(item.semantic_unit_id for item in annotations) != unit_ids
        or tuple(item.semantic_unit_id for item in items) != unit_ids
        or tuple(item.semantic_unit_id for item in requests) != unit_ids
        or tuple(item.semantic_unit_id for item in decisions) != unit_ids
    ):
        _reject(
            ControlledSemanticUnitReviewRejectionCode.INPUT_MISMATCH,
            "Controlled review inputs do not exactly cover semantic units in order.",
        )
    if any(item.source_semantic_unit_ids != (item.semantic_unit_id,) for item in items):
        _reject(
            ControlledSemanticUnitReviewRejectionCode.UNSUPPORTED_LINEAGE,
            "Controlled review migration currently requires one prediction per unit.",
        )
    if any(item.is_continuation_fragment for item in decisions):
        _reject(
            ControlledSemanticUnitReviewRejectionCode.UNSUPPORTED_LINEAGE,
            "Controlled review migration does not infer continuation ownership.",
        )

    included_unit_ids = tuple(
        item.semantic_unit_id
        for item in items
        if item.status is RequirementAssemblyStatus.CANDIDATE and item.include_in_view
    )
    view_nodes = downstream.requirement_view.nodes
    if tuple(item.semantic_unit_id for item in view_nodes) != included_unit_ids:
        _reject(
            ControlledSemanticUnitReviewRejectionCode.INPUT_MISMATCH,
            "Controlled RequirementView does not exactly cover included units.",
        )

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    units_by_id = {item.semantic_unit_id: item for item in plan.units}
    view_by_unit = {item.semantic_unit_id: item for item in view_nodes}
    prediction_nodes: list[LlmRequirementPredictionNode] = []
    try:
        for unit, request, decision, annotation, item in zip(
            plan.units,
            requests,
            decisions,
            annotations,
            items,
            strict=True,
        ):
            owner = nodes_by_id.get(unit.owner_structure_node_id)
            owner_source_refs = (
                owner.effective_semantic_source_refs
                if owner is not None
                else ()
            )
            if (
                owner is None
                or not owner_source_refs
                or owner.source_order != unit.source_order
                or annotation.owner_structure_node_id != unit.owner_structure_node_id
                or item.owner_structure_node_id != unit.owner_structure_node_id
                or item.source_owner_structure_node_ids
                != (unit.owner_structure_node_id,)
                or item.unit_order != unit.unit_order
                or item.source_order != unit.source_order
                or request.source_ir_id != source_ir.source_ir_id
                or request.source_sha256 != source_ir.source_sha256
                or request.structure_snapshot_id != tree.structure_snapshot_id
                or decision.owner_structure_node_id != unit.owner_structure_node_id
                or decision.semantic_class is not annotation.semantic_class
                or decision.requirement_subtype is not annotation.requirement_subtype
                or decision.is_continuation_fragment
                != annotation.is_continuation_fragment
            ):
                _reject(
                    ControlledSemanticUnitReviewRejectionCode.INPUT_MISMATCH,
                    "Controlled unit ownership no longer matches Structure or downstream.",
                )
            if (
                tuple(evidence.grounding_span for evidence in request.evidence)
                != unit.source_spans
            ):
                _reject(
                    ControlledSemanticUnitReviewRejectionCode.INVALID_GROUNDING,
                    "Controlled review text does not exactly match unit source spans.",
                )
            source_refs = tuple(
                dict.fromkeys(
                    evidence.grounding_span.source_ref for evidence in request.evidence
                )
            )
            if not source_refs or any(
                source_ref not in owner_source_refs for source_ref in source_refs
            ):
                _reject(
                    ControlledSemanticUnitReviewRejectionCode.INVALID_GROUNDING,
                    "Controlled review source refs escape Structure ownership.",
                )

            view_node = view_by_unit.get(unit.semantic_unit_id)
            included = (
                item.status is RequirementAssemblyStatus.CANDIDATE
                and item.include_in_view
            )
            if included != (view_node is not None):
                _reject(
                    ControlledSemanticUnitReviewRejectionCode.INPUT_MISMATCH,
                    "Controlled review inclusion does not match RequirementView.",
                )
            if view_node is not None and (
                view_node.owner_structure_node_id != unit.owner_structure_node_id
                or view_node.unit_order != unit.unit_order
                or view_node.source_order != unit.source_order
                or view_node.semantic_class is not annotation.semantic_class
                or view_node.requirement_subtype is not annotation.requirement_subtype
                or view_node.role is not item.role
                or view_node.source_semantic_unit_ids != (unit.semantic_unit_id,)
            ):
                _reject(
                    ControlledSemanticUnitReviewRejectionCode.INPUT_MISMATCH,
                    "Controlled RequirementView changed the unit decision binding.",
                )

            parent_unit_id = item.parent_semantic_unit_id
            if parent_unit_id is not None:
                parent = units_by_id.get(parent_unit_id)
                if (
                    parent is None
                    or item.parent_owner_structure_node_id
                    != parent.owner_structure_node_id
                ):
                    _reject(
                        ControlledSemanticUnitReviewRejectionCode.INPUT_MISMATCH,
                        "Controlled review parent grounding is invalid.",
                    )

            review_text = "".join(evidence.text for evidence in request.evidence)
            prediction_nodes.append(
                LlmRequirementPredictionNode(
                    semantic_unit_id=unit.semantic_unit_id,
                    source_semantic_unit_ids=(unit.semantic_unit_id,),
                    owner_structure_node_id=unit.owner_structure_node_id,
                    source_owner_structure_node_ids=(unit.owner_structure_node_id,),
                    unit_order=unit.unit_order,
                    source_order=unit.source_order,
                    semantic_class=annotation.semantic_class,
                    requirement_subtype=annotation.requirement_subtype,
                    include_in_view=included,
                    role=view_node.role if view_node is not None else None,
                    parent_semantic_unit_id=(
                        parent_unit_id if view_node is not None else None
                    ),
                    parent_owner_structure_node_id=(
                        item.parent_owner_structure_node_id
                        if view_node is not None
                        else None
                    ),
                    decision_mode=(
                        view_node.decision_mode
                        if view_node is not None
                        else RequirementDecisionMode.NONE
                    ),
                    source_refs=source_refs,
                    source_binding_sha256=source_binding_sha256(
                        source_ir_id=source_ir.source_ir_id,
                        semantic_unit_id=unit.semantic_unit_id,
                        source_semantic_unit_ids=(unit.semantic_unit_id,),
                        owner_structure_node_id=unit.owner_structure_node_id,
                        source_owner_structure_node_ids=(unit.owner_structure_node_id,),
                        source_refs=source_refs,
                    ),
                    review_text=review_text,
                    review_text_sha256=hashlib.sha256(
                        review_text.encode("utf-8")
                    ).hexdigest(),
                    reason_codes=tuple(
                        sorted(
                            {
                                *annotation.reason_codes,
                                *item.reason_codes,
                            }
                        )
                    ),
                )
            )
    except ControlledSemanticUnitReviewRejected:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlledSemanticUnitReviewRejected(
            ControlledSemanticUnitReviewRejectionCode.INVALID_PREDICTION,
            "Controlled multi-unit prediction violated the review contract.",
        ) from exc

    ordered_nodes = tuple(
        sorted(
            prediction_nodes,
            key=lambda item: (
                item.source_order,
                item.unit_order,
                item.semantic_unit_id,
            ),
        )
    )
    provider_run_ids = tuple(
        dict.fromkeys(
            (
                *(item.provider_run_id for item in annotations),
                downstream.assembly.provider_run_id,
            )
        )
    )
    prediction_kwargs = {
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "structure_snapshot_id": tree.structure_snapshot_id,
        "scope_locator_sha256": _controlled_scope_locator_sha256(
            source_ir,
            tree,
            plan.semantic_unit_plan_id,
            plan.scope_owner_structure_node_ids,
        ),
        "provider_id": decision_execution.provider_id,
        "provider_version": decision_execution.provider_version,
        "model_id": decision_execution.model_id,
        "classification_prompt_version": SEMANTIC_UNIT_DECISION_SCHEMA_VERSION,
        "assembly_prompt_version": (CONTROLLED_SEMANTIC_UNIT_ASSEMBLY_CONTRACT_VERSION),
        "semantic_unit_plan_ids": (plan.semantic_unit_plan_id,),
        "semantic_unit_ids": tuple(
            unit_id
            for node in ordered_nodes
            for unit_id in node.source_semantic_unit_ids
        ),
        "provider_run_ids": provider_run_ids,
        "nodes": ordered_nodes,
    }
    try:
        return LlmRequirementModelPrediction(
            prediction_id=stable_llm_requirement_prediction_id(**prediction_kwargs),
            **prediction_kwargs,
        )
    except (TypeError, ValueError) as exc:
        raise ControlledSemanticUnitReviewRejected(
            ControlledSemanticUnitReviewRejectionCode.INVALID_PREDICTION,
            "Controlled multi-unit prediction violated the review contract.",
        ) from exc


def _controlled_scope_locator_sha256(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_unit_plan_id: str,
    owner_ids: tuple[str, ...],
) -> str:
    payload = {
        "adapter_version": CONTROLLED_SEMANTIC_UNIT_REVIEW_ADAPTER_VERSION,
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "structure_snapshot_id": tree.structure_snapshot_id,
        "semantic_unit_plan_id": semantic_unit_plan_id,
        "owner_structure_node_ids": owner_ids,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _reject(
    code: ControlledSemanticUnitReviewRejectionCode,
    message: str,
) -> None:
    raise ControlledSemanticUnitReviewRejected(code, message)


__all__ = [
    "CONTROLLED_SEMANTIC_UNIT_ASSEMBLY_CONTRACT_VERSION",
    "CONTROLLED_SEMANTIC_UNIT_REVIEW_ADAPTER_VERSION",
    "ControlledSemanticUnitReviewRejected",
    "ControlledSemanticUnitReviewRejectionCode",
    "build_controlled_semantic_unit_review_prediction",
]
