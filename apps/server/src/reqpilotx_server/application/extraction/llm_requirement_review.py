from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from reqpilotx_server.adapters.documents.bailian_semantic_provider import (
    REQUIREMENT_ASSEMBLY_PROMPT_VERSION,
    SEMANTIC_CLASSIFICATION_PROMPT_VERSION,
)
from reqpilotx_server.application.extraction.llm_provision_batch import (
    LlmProvisionExtractionStatus,
    LlmRequirementBatchResult,
    LlmRequirementDocumentResult,
)
from reqpilotx_server.application.extraction.classify_content import classify_content
from reqpilotx_server.application.extraction.locate_llm_scope import LocatedLlmExtractionScope
from reqpilotx_server.application.extraction.structure_snapshot import structure_snapshot_id
from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LlmRequirementModelPrediction,
    LlmRequirementPredictionNode,
    LlmRequirementReviewDelta,
    LlmRequirementReviewMetrics,
    LlmRequirementReviewOperation,
    LlmRequirementReviewOperationType,
    LlmRequirementReviewReferenceCandidate,
    LlmRequirementReviewReferenceCandidateStatus,
    LlmRequirementReviewSession,
    LlmRequirementReviewStatus,
    LlmRequirementReviewedResult,
    LlmReviewSourceSpan,
    LlmReviewedRequirementNode,
    source_binding_sha256,
    stable_llm_requirement_prediction_id,
)
from reqpilotx_server.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from reqpilotx_server.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
)
from reqpilotx_server.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotation,
    SemanticClass,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure import (
    StructureNode,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)


def build_llm_requirement_model_prediction(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    located_scope: LocatedLlmExtractionScope,
    batch: LlmRequirementBatchResult | LlmRequirementDocumentResult,
) -> LlmRequirementModelPrediction:
    """Freeze the provider's one best answer as a source-grounded prediction."""

    if tree.source_ir_id != source_ir.source_ir_id:
        raise ValueError("review prediction inputs are not bound to one SourceIR")
    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    prediction_nodes: list[LlmRequirementPredictionNode] = []
    provider_run_ids: list[str] = []
    semantic_unit_plan_ids: list[str] = []
    seen_targets: set[str] = set()
    unit_order_by_owner = {
        owner_id: unit_order
        for unit_order, owner_id in enumerate(located_scope.scope_structure_node_ids)
    }

    for outcome in batch.provisions:
        extraction = outcome.extraction
        runs = (
            extraction.provider_runs
            if extraction is not None
            else outcome.completed_provider_runs
        )
        provider_run_ids.extend(item.provider_run_id for item in runs)
        if (
            outcome.status is LlmProvisionExtractionStatus.SUCCESS
            and extraction is not None
        ):
            semantic_unit_plan_ids.append(extraction.semantics.semantic_unit_plan_id)
            annotations_by_target: dict[str, list[SemanticAnnotation]] = defaultdict(
                list
            )
            annotations_by_id: dict[str, SemanticAnnotation] = {}
            for annotation in extraction.semantics.annotations:
                annotations_by_target[annotation.target_structure_node_id].append(
                    annotation
                )
                annotations_by_id[annotation.semantic_annotation_id] = annotation
            assembly_by_target = {
                item.target_structure_node_id: item
                for item in extraction.assembly.items
            }
            view_by_target = {
                item.target_structure_node_id: item
                for item in extraction.requirement_view.nodes
            }
            view_target_by_id = {
                item.requirement_view_node_id: item.target_structure_node_id
                for item in extraction.requirement_view.nodes
            }
            review_owner_by_target = {
                target_id: target_id for target_id in outcome.scope_structure_node_ids
            }
            for projected in extraction.requirement_view.nodes:
                owner_target_id = projected.target_structure_node_id
                for annotation_id in projected.semantic_annotation_ids:
                    projected_annotation = annotations_by_id.get(annotation_id)
                    if projected_annotation is None:
                        raise ValueError("projected review source annotation is absent")
                    source_target_id = projected_annotation.target_structure_node_id
                    if source_target_id == owner_target_id:
                        continue
                    if not projected_annotation.is_continuation_fragment:
                        continue
                    if (
                        source_target_id not in review_owner_by_target
                        or source_target_id in view_by_target
                    ):
                        raise ValueError(
                            "projected review source has no continuation ownership"
                        )
                    existing_owner_id = review_owner_by_target[source_target_id]
                    if existing_owner_id != source_target_id:
                        raise ValueError(
                            "projected continuation has multiple review owners"
                        )
                    review_owner_by_target[source_target_id] = owner_target_id
            review_owner_by_target = _retain_contiguous_review_ownership(
                review_owner_by_target,
                nodes_by_id=nodes_by_id,
            )
            for target_id in outcome.scope_structure_node_ids:
                if target_id in seen_targets:
                    raise ValueError("LLM review prediction scope contains duplicates")
                seen_targets.add(target_id)
                structure_node = _require_grounded_structure_node(
                    nodes_by_id,
                    target_id,
                )
                annotations = annotations_by_target.get(target_id, [])
                annotation = annotations[0] if len(annotations) == 1 else None
                assembly = assembly_by_target.get(target_id)
                view_node = view_by_target.get(target_id)
                semantic_class = (
                    annotation.semantic_class
                    if annotation is not None
                    else SemanticClass.UNRESOLVED
                )
                subtype = (
                    annotation.requirement_subtype if annotation is not None else None
                )
                projection_promotes_excluded_continuation = bool(
                    assembly is not None
                    and assembly.status is RequirementAssemblyStatus.CANDIDATE
                    and not assembly.include_in_view
                    and view_node is not None
                    and annotation is not None
                    and annotation.is_continuation_fragment
                )
                include_in_view = bool(
                    assembly is not None
                    and assembly.status is RequirementAssemblyStatus.CANDIDATE
                    and view_node is not None
                    and (
                        assembly.include_in_view
                        or projection_promotes_excluded_continuation
                    )
                )
                role = view_node.role if include_in_view and view_node else None
                parent_target_id = None
                if (
                    include_in_view
                    and view_node is not None
                    and view_node.parent_requirement_view_node_id is not None
                ):
                    parent_target_id = view_target_by_id.get(
                        view_node.parent_requirement_view_node_id
                    )
                    if parent_target_id is None:
                        raise ValueError(
                            "review prediction parent is absent from the model view"
                        )
                decision_mode = (
                    view_node.decision_mode
                    if include_in_view and view_node is not None
                    else RequirementDecisionMode.NONE
                )
                reason_codes = sorted(
                    {
                        *(
                            annotation.reason_codes
                            if annotation is not None
                            else ("semantic_prediction_missing",)
                        ),
                        *(
                            assembly.reason_codes
                            if assembly is not None
                            else ("assembly_prediction_missing",)
                        ),
                        *(
                            ("rollup_residual_projection",)
                            if projection_promotes_excluded_continuation
                            else ()
                        ),
                    }
                )
                prediction_nodes.append(
                    _prediction_node(
                        source_ir=source_ir,
                        structure_node=structure_node,
                        semantic_unit_id=annotation.semantic_unit_id,
                        unit_order=unit_order_by_owner[target_id],
                        review_owner_semantic_unit_id=(
                            annotations_by_target[review_owner_by_target[target_id]][
                                0
                            ].semantic_unit_id
                            if review_owner_by_target[target_id] != target_id
                            else None
                        ),
                        review_owner_target_structure_node_id=(
                            review_owner_by_target[target_id]
                            if review_owner_by_target[target_id] != target_id
                            else None
                        ),
                        semantic_class=semantic_class,
                        requirement_subtype=subtype,
                        include_in_view=include_in_view,
                        role=role,
                        parent_semantic_unit_id=(
                            annotations_by_target[parent_target_id][0].semantic_unit_id
                            if parent_target_id is not None
                            else None
                        ),
                        parent_target_structure_node_id=parent_target_id,
                        decision_mode=decision_mode,
                        reason_codes=tuple(reason_codes),
                    )
                )
            continue

        fallback_semantics = classify_content(
            source_ir,
            tree,
            scope_structure_node_ids=outcome.scope_structure_node_ids,
        )
        semantic_unit_plan_ids.append(fallback_semantics.semantic_unit_plan_id)
        fallback_annotations_by_owner = {
            annotation.owner_structure_node_id: annotation
            for annotation in fallback_semantics.annotations
        }
        for target_id in outcome.scope_structure_node_ids:
            if target_id in seen_targets:
                raise ValueError("LLM review prediction scope contains duplicates")
            seen_targets.add(target_id)
            structure_node = _require_grounded_structure_node(nodes_by_id, target_id)
            annotation = fallback_annotations_by_owner[target_id]
            prediction_nodes.append(
                _prediction_node(
                    source_ir=source_ir,
                    structure_node=structure_node,
                    semantic_unit_id=annotation.semantic_unit_id,
                    unit_order=unit_order_by_owner[target_id],
                    semantic_class=SemanticClass.UNRESOLVED,
                    requirement_subtype=None,
                    include_in_view=False,
                    role=None,
                    parent_semantic_unit_id=None,
                    parent_target_structure_node_id=None,
                    decision_mode=RequirementDecisionMode.NONE,
                    reason_codes=(
                        outcome.error_code
                        or (
                            "llm_provision_blocked_input"
                            if outcome.status
                            is LlmProvisionExtractionStatus.BLOCKED_INPUT
                            else "llm_provision_unresolved"
                        ),
                    ),
                )
            )

    expected_targets = set(located_scope.scope_structure_node_ids)
    if seen_targets != expected_targets:
        raise ValueError("LLM review prediction does not cover the located scope")
    ordered_nodes = tuple(sorted(prediction_nodes, key=_prediction_node_sort_key))
    provider_id, provider_version, model_id = batch.provider_binding
    unique_run_ids = tuple(dict.fromkeys(provider_run_ids))
    prediction_id = stable_llm_requirement_prediction_id(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        structure_snapshot_id=structure_snapshot_id(tree),
        scope_locator_sha256=located_scope.locator_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        model_id=model_id,
        classification_prompt_version=SEMANTIC_CLASSIFICATION_PROMPT_VERSION,
        assembly_prompt_version=REQUIREMENT_ASSEMBLY_PROMPT_VERSION,
        semantic_unit_plan_ids=tuple(dict.fromkeys(semantic_unit_plan_ids)),
        semantic_unit_ids=tuple(node.semantic_unit_id for node in ordered_nodes),
        provider_run_ids=unique_run_ids,
        nodes=ordered_nodes,
    )
    return LlmRequirementModelPrediction(
        prediction_id=prediction_id,
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        structure_snapshot_id=structure_snapshot_id(tree),
        scope_locator_sha256=located_scope.locator_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        model_id=model_id,
        classification_prompt_version=SEMANTIC_CLASSIFICATION_PROMPT_VERSION,
        assembly_prompt_version=REQUIREMENT_ASSEMBLY_PROMPT_VERSION,
        semantic_unit_plan_ids=tuple(dict.fromkeys(semantic_unit_plan_ids)),
        semantic_unit_ids=tuple(node.semantic_unit_id for node in ordered_nodes),
        provider_run_ids=unique_run_ids,
        nodes=ordered_nodes,
    )


def _retain_contiguous_review_ownership(
    review_owner_by_target: dict[str, str],
    *,
    nodes_by_id: dict[str, StructureNode],
) -> dict[str, str]:
    """Keep automatic rollups only when one review unit stays source-contiguous."""

    normalized = dict(review_owner_by_target)
    ordered_targets = sorted(
        normalized,
        key=lambda target_id: (
            nodes_by_id[target_id].source_order,
            target_id,
        ),
    )
    closed_owners: set[str] = set()
    interleaved_owners: set[str] = set()
    previous_owner: str | None = None
    for target_id in ordered_targets:
        owner_id = normalized[target_id]
        if owner_id == previous_owner:
            continue
        if owner_id in closed_owners:
            interleaved_owners.add(owner_id)
        if previous_owner is not None:
            closed_owners.add(previous_owner)
        previous_owner = owner_id

    for target_id, owner_id in tuple(normalized.items()):
        if owner_id in interleaved_owners and target_id != owner_id:
            normalized[target_id] = target_id
    return normalized


def initial_llm_requirement_review(
    prediction: LlmRequirementModelPrediction,
) -> LlmRequirementReviewedResult:
    prediction_by_unit = {item.semantic_unit_id: item for item in prediction.nodes}
    source_nodes_by_owner: dict[str, list[LlmRequirementPredictionNode]] = defaultdict(
        list
    )
    for item in prediction.nodes:
        source_nodes_by_owner[_review_owner_semantic_unit_id(item)].append(item)

    reviewed_nodes: list[LlmReviewedRequirementNode] = []
    for owner_id, source_nodes in sorted(
        source_nodes_by_owner.items(),
        key=lambda entry: (
            min((item.source_order, item.unit_order) for item in entry[1]),
            entry[0],
        ),
    ):
        owner = prediction_by_unit[owner_id]
        ordered_sources = tuple(
            sorted(
                source_nodes,
                key=_prediction_node_sort_key,
            )
        )
        reviewed_nodes.append(
            LlmReviewedRequirementNode(
                review_node_id=owner.semantic_unit_id,
                source_semantic_unit_ids=tuple(
                    item.semantic_unit_id for item in ordered_sources
                ),
                source_owner_structure_node_ids=tuple(
                    item.owner_structure_node_id for item in ordered_sources
                ),
                source_spans=tuple(
                    LlmReviewSourceSpan(
                        semantic_unit_id=item.semantic_unit_id,
                        owner_structure_node_id=item.owner_structure_node_id,
                        start_offset=0,
                        end_offset=len(item.review_text),
                    )
                    for item in ordered_sources
                ),
                source_order=ordered_sources[0].source_order,
                semantic_class=owner.semantic_class,
                requirement_subtype=owner.requirement_subtype,
                include_in_view=owner.include_in_view,
                role=owner.role,
                parent_review_node_id=(
                    _review_owner_semantic_unit_id(
                        prediction_by_unit[owner.parent_semantic_unit_id]
                    )
                    if owner.parent_semantic_unit_id is not None
                    else None
                ),
                decision_mode=owner.decision_mode,
                reason_codes=(),
            )
        )
    nodes = tuple(reviewed_nodes)
    result = LlmRequirementReviewedResult(
        prediction_id=prediction.prediction_id,
        revision=0,
        status=LlmRequirementReviewStatus.DRAFT,
        reviewer_label="",
        nodes=nodes,
    )
    validate_llm_requirement_review(prediction, result)
    return result


def validate_llm_requirement_review(
    prediction: LlmRequirementModelPrediction,
    reviewed: LlmRequirementReviewedResult,
) -> None:
    if reviewed.prediction_id != prediction.prediction_id:
        raise ValueError("reviewed result belongs to a different model prediction")
    if (
        reviewed.status is not LlmRequirementReviewStatus.DRAFT
        and not reviewed.reviewer_label.strip()
    ):
        raise ValueError("a completed review requires a reviewer label")

    prediction_by_unit = {item.semantic_unit_id: item for item in prediction.nodes}
    spans_by_unit: dict[
        str, list[tuple[LlmReviewSourceSpan, LlmReviewedRequirementNode]]
    ] = defaultdict(list)
    for node in reviewed.nodes:
        for span in node.source_spans:
            predicted = prediction_by_unit.get(span.semantic_unit_id)
            if predicted is None:
                raise ValueError("review result introduced an unknown source target")
            if span.owner_structure_node_id != predicted.owner_structure_node_id:
                raise ValueError("review source span owner grounding is invalid")
            text_length = len(predicted.review_text)
            if span.end_offset > text_length:
                raise ValueError("review source span exceeds immutable target text")
            if text_length and span.end_offset == span.start_offset:
                raise ValueError("non-empty review targets require non-empty spans")
            if not text_length and (span.start_offset, span.end_offset) != (0, 0):
                raise ValueError("empty review targets require the exact empty span")
            spans_by_unit[span.semantic_unit_id].append((span, node))

        expected_spans = tuple(
            sorted(
                node.source_spans,
                key=lambda span: (
                    prediction_by_unit[span.semantic_unit_id].source_order,
                    prediction_by_unit[span.semantic_unit_id].unit_order,
                    span.start_offset,
                    span.end_offset,
                    span.semantic_unit_id,
                ),
            )
        )
        if node.source_spans != expected_spans:
            raise ValueError("review source spans must retain source order")
        for previous_span, current_span in zip(
            node.source_spans,
            node.source_spans[1:],
            strict=False,
        ):
            if (
                previous_span.semantic_unit_id == current_span.semantic_unit_id
                and previous_span.end_offset == current_span.start_offset
            ):
                raise ValueError(
                    "adjacent source spans in one review node must be coalesced"
                )
        expected_source_order = min(
            prediction_by_unit[span.semantic_unit_id].source_order
            for span in node.source_spans
        )
        if node.source_order != expected_source_order:
            raise ValueError("review source order must come from its earliest target")

    if set(spans_by_unit) != set(prediction_by_unit):
        raise ValueError("review result must cover every model target")
    for unit_id, predicted in prediction_by_unit.items():
        ordered_spans = sorted(
            (span for span, _node in spans_by_unit[unit_id]),
            key=lambda span: (span.start_offset, span.end_offset),
        )
        if not predicted.review_text and len(ordered_spans) != 1:
            raise ValueError("empty review targets require one exact empty span")
        cursor = 0
        for span in ordered_spans:
            if span.start_offset != cursor:
                raise ValueError(
                    "review source spans must exactly partition each model target"
                )
            cursor = span.end_offset
        if cursor != len(predicted.review_text):
            raise ValueError(
                "review source spans must exactly partition each model target"
            )

    ordered_span_owners = sorted(
        (
            (
                prediction_by_unit[span.semantic_unit_id].source_order,
                prediction_by_unit[span.semantic_unit_id].unit_order,
                span.start_offset,
                span.end_offset,
                span.semantic_unit_id,
                node.review_node_id,
            )
            for node in reviewed.nodes
            for span in node.source_spans
        )
    )
    closed_review_nodes: set[str] = set()
    previous_owner: str | None = None
    for *_span_key, owner in ordered_span_owners:
        if owner == previous_owner:
            continue
        if owner in closed_review_nodes:
            raise ValueError("review nodes cannot interleave source spans")
        if previous_owner is not None:
            closed_review_nodes.add(previous_owner)
        previous_owner = owner

    expected_node_order = tuple(
        sorted(
            reviewed.nodes,
            key=lambda node: _review_node_sort_key(node, prediction_by_unit),
        )
    )
    if reviewed.nodes != expected_node_order:
        raise ValueError("review nodes must retain exact source-span order")

    reviewed_by_id = {item.review_node_id: item for item in reviewed.nodes}
    for node in reviewed.nodes:
        parent_id = node.parent_review_node_id
        if parent_id is None:
            continue
        parent = reviewed_by_id.get(parent_id)
        if parent is None:
            raise ValueError("review parent must be another reviewed node")
        if not parent.include_in_view or parent.role in {
            RequirementProjectionRole.CONTENT,
            RequirementProjectionRole.REFERENCE,
        }:
            raise ValueError("review children require an included requirement parent")
        if _review_node_sort_key(parent, prediction_by_unit) >= _review_node_sort_key(
            node, prediction_by_unit
        ):
            raise ValueError("review parent must precede its child in source order")

    for node in reviewed.nodes:
        visited = {node.review_node_id}
        parent_id = node.parent_review_node_id
        while parent_id is not None:
            if parent_id in visited:
                raise ValueError("review parent graph must be acyclic")
            visited.add(parent_id)
            parent_id = reviewed_by_id[parent_id].parent_review_node_id

    direct_children: dict[str, int] = defaultdict(int)
    for node in reviewed.nodes:
        if (
            node.parent_review_node_id is not None
            and node.include_in_view
            and node.decision_mode is RequirementDecisionMode.DIRECT
        ):
            direct_children[node.parent_review_node_id] += 1
    for node in reviewed.nodes:
        if (
            node.include_in_view
            and node.decision_mode is RequirementDecisionMode.ROLLUP
            and direct_children[node.review_node_id] == 0
        ):
            raise ValueError("ROLLUP review nodes require a direct child target")


def evaluate_llm_requirement_review(
    prediction: LlmRequirementModelPrediction,
    reviewed: LlmRequirementReviewedResult,
) -> LlmRequirementReviewDelta:
    validate_llm_requirement_review(prediction, reviewed)
    prediction_by_unit = {item.semantic_unit_id: item for item in prediction.nodes}
    source_units_by_owner: dict[str, list[str]] = defaultdict(list)
    for predicted in prediction.nodes:
        source_units_by_owner[_review_owner_semantic_unit_id(predicted)].append(
            predicted.semantic_unit_id
        )
    baseline_source_units_by_owner = {
        owner_id: tuple(
            sorted(
                unit_ids,
                key=lambda unit_id: _prediction_node_sort_key(
                    prediction_by_unit[unit_id]
                ),
            )
        )
        for owner_id, unit_ids in source_units_by_owner.items()
    }
    decision_predictions = tuple(
        predicted
        for predicted in prediction.nodes
        if _review_owner_semantic_unit_id(predicted) == predicted.semantic_unit_id
    )
    reviewed_by_id = {item.review_node_id: item for item in reviewed.nodes}
    unit_to_reviews: dict[str, list[LlmReviewedRequirementNode]] = defaultdict(list)
    unit_span_counts: dict[str, int] = defaultdict(int)
    for node in reviewed.nodes:
        for unit_id in node.source_semantic_unit_ids:
            unit_to_reviews[unit_id].append(node)
        for span in node.source_spans:
            unit_span_counts[span.semantic_unit_id] += 1

    operations: list[LlmRequirementReviewOperation] = []
    confirmed_targets = 0
    semantic_matches = 0
    subtype_matches = 0
    inclusion_matches = 0
    role_matches = 0
    parent_matches = 0
    decision_matches = 0
    subtype_compared_targets = 0
    projection_compared_targets = 0
    split_owner_targets: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for predicted in decision_predictions:
        owner_target_id = predicted.semantic_unit_id
        baseline_targets = baseline_source_units_by_owner[owner_target_id]
        review_ids = tuple(
            dict.fromkeys(
                current.review_node_id
                for source_target_id in baseline_targets
                for current in unit_to_reviews[source_target_id]
            )
        )
        if len(review_ids) > 1 or any(
            unit_span_counts[source_target_id] > 1
            for source_target_id in baseline_targets
        ):
            split_owner_targets[owner_target_id] = (baseline_targets, review_ids)
    promoted_targets: set[str] = set()

    merged_review_nodes = tuple(
        node
        for node in reviewed.nodes
        if len(
            {
                _review_owner_semantic_unit_id(prediction_by_unit[unit_id])
                for unit_id in node.source_semantic_unit_ids
            }
        )
        > 1
    )
    for node in merged_review_nodes:
        merged_owner_count = len(
            {
                _review_owner_semantic_unit_id(prediction_by_unit[unit_id])
                for unit_id in node.source_semantic_unit_ids
            }
        )
        if merged_owner_count > 1:
            operations.append(
                LlmRequirementReviewOperation(
                    operation=LlmRequirementReviewOperationType.MERGED,
                    review_node_id=node.review_node_id,
                    source_semantic_unit_ids=node.source_semantic_unit_ids,
                    source_owner_structure_node_ids=(
                        node.source_owner_structure_node_ids
                    ),
                    field="source_semantic_unit_ids",
                    before=merged_owner_count,
                    after=1,
                )
            )

    for owner_target_id in sorted(
        split_owner_targets,
        key=lambda value: _prediction_node_sort_key(prediction_by_unit[value]),
    ):
        baseline_targets, review_ids = split_owner_targets[owner_target_id]
        operations.append(
            LlmRequirementReviewOperation(
                operation=LlmRequirementReviewOperationType.SPLIT,
                review_node_id=review_ids[0],
                source_semantic_unit_ids=baseline_targets,
                source_owner_structure_node_ids=tuple(
                    prediction_by_unit[unit_id].owner_structure_node_id
                    for unit_id in baseline_targets
                ),
                field="source_spans",
                before=1,
                after=len(review_ids),
            )
        )

    for predicted in decision_predictions:
        target_id = predicted.semantic_unit_id
        baseline_source_targets = baseline_source_units_by_owner[target_id]
        current_nodes = list(
            dict.fromkeys(
                current.review_node_id
                for source_target_id in baseline_source_targets
                for current in unit_to_reviews[source_target_id]
            )
        )
        current_nodes = [reviewed_by_id[review_id] for review_id in current_nodes]

        def parent_matches_target(current: LlmReviewedRequirementNode) -> bool:
            current_parent = (
                reviewed_by_id[current.parent_review_node_id]
                if current.parent_review_node_id is not None
                else None
            )
            return (
                predicted.parent_semantic_unit_id is None and current_parent is None
            ) or (
                predicted.parent_semantic_unit_id is not None
                and current_parent is not None
                and predicted.parent_semantic_unit_id
                in current_parent.source_semantic_unit_ids
            )

        field_matches = {
            "semantic_class": all(
                current.semantic_class is predicted.semantic_class
                for current in current_nodes
            ),
            "requirement_subtype": all(
                current.requirement_subtype is predicted.requirement_subtype
                for current in current_nodes
            ),
            "include_in_view": all(
                current.include_in_view == predicted.include_in_view
                for current in current_nodes
            ),
            "role": all(current.role is predicted.role for current in current_nodes),
            "parent": all(parent_matches_target(current) for current in current_nodes),
            "decision_mode": all(
                current.decision_mode is predicted.decision_mode
                for current in current_nodes
            ),
        }
        semantic_matches += int(field_matches["semantic_class"])
        inclusion_matches += int(field_matches["include_in_view"])
        if predicted.semantic_class is SemanticClass.REQUIREMENT or any(
            current.semantic_class is SemanticClass.REQUIREMENT
            for current in current_nodes
        ):
            subtype_compared_targets += 1
            subtype_matches += int(field_matches["requirement_subtype"])
        if predicted.include_in_view or any(
            current.include_in_view for current in current_nodes
        ):
            projection_compared_targets += 1
            role_matches += int(field_matches["role"])
            parent_matches += int(field_matches["parent"])
            decision_matches += int(field_matches["decision_mode"])
        singleton = (
            len(current_nodes) == 1
            and current_nodes[0].source_semantic_unit_ids == baseline_source_targets
            and all(
                unit_span_counts[source_target_id] == 1
                for source_target_id in baseline_source_targets
            )
        )
        if all(field_matches.values()) and singleton:
            current = current_nodes[0]
            confirmed_targets += 1
            operations.append(
                LlmRequirementReviewOperation(
                    operation=LlmRequirementReviewOperationType.CONFIRMED,
                    review_node_id=current.review_node_id,
                    source_semantic_unit_ids=(target_id,),
                    source_owner_structure_node_ids=(
                        predicted.owner_structure_node_id,
                    ),
                )
            )
            continue

        changes = (
            (
                "semantic_class",
                LlmRequirementReviewOperationType.RECLASSIFIED,
                predicted.semantic_class.value,
                _review_field_summary(current_nodes, "semantic_class"),
            ),
            (
                "requirement_subtype",
                LlmRequirementReviewOperationType.SUBTYPE_CHANGED,
                _enum_value(predicted.requirement_subtype),
                _review_field_summary(current_nodes, "requirement_subtype"),
            ),
            (
                "include_in_view",
                (
                    LlmRequirementReviewOperationType.INCLUDED
                    if all(current.include_in_view for current in current_nodes)
                    else LlmRequirementReviewOperationType.EXCLUDED
                ),
                predicted.include_in_view,
                _review_field_summary(current_nodes, "include_in_view"),
            ),
            (
                "role",
                LlmRequirementReviewOperationType.ROLE_CHANGED,
                _enum_value(predicted.role),
                _review_field_summary(current_nodes, "role"),
            ),
            (
                "parent_review_node_id",
                LlmRequirementReviewOperationType.REPARENTED,
                predicted.parent_semantic_unit_id,
                _review_field_summary(current_nodes, "parent_review_node_id"),
            ),
            (
                "decision_mode",
                LlmRequirementReviewOperationType.DECISION_MODE_CHANGED,
                predicted.decision_mode.value,
                _review_field_summary(current_nodes, "decision_mode"),
            ),
        )
        for field, operation_type, before, after in changes:
            matches = (
                field_matches["parent"]
                if field == "parent_review_node_id"
                else field_matches[field]
            )
            if matches:
                continue
            operations.append(
                LlmRequirementReviewOperation(
                    operation=operation_type,
                    review_node_id=current_nodes[0].review_node_id,
                    source_semantic_unit_ids=(target_id,),
                    source_owner_structure_node_ids=(
                        predicted.owner_structure_node_id,
                    ),
                    field=field,
                    before=before,
                    after=after,
                )
            )
        if (
            predicted.include_in_view
            and predicted.parent_semantic_unit_id is not None
            and any(
                current.include_in_view
                and current.role is RequirementProjectionRole.PRIMARY
                for current in current_nodes
            )
        ):
            promoted_targets.add(target_id)
            operations.append(
                LlmRequirementReviewOperation(
                    operation=LlmRequirementReviewOperationType.PROMOTED,
                    review_node_id=current_nodes[0].review_node_id,
                    source_semantic_unit_ids=(target_id,),
                    source_owner_structure_node_ids=(
                        predicted.owner_structure_node_id,
                    ),
                    field="role",
                    before=_enum_value(predicted.role),
                    after=RequirementProjectionRole.PRIMARY.value,
                )
            )

    target_count = len(decision_predictions)
    metrics = LlmRequirementReviewMetrics(
        target_count=target_count,
        reviewed_node_count=len(reviewed.nodes),
        confirmed_target_count=confirmed_targets,
        edited_target_count=target_count - confirmed_targets,
        confirmed_target_rate=confirmed_targets / target_count,
        requirement_subtype_compared_target_count=subtype_compared_targets,
        projection_compared_target_count=projection_compared_targets,
        semantic_class_agreement=semantic_matches / target_count,
        requirement_subtype_agreement=(
            subtype_matches / subtype_compared_targets
            if subtype_compared_targets
            else 1.0
        ),
        inclusion_agreement=inclusion_matches / target_count,
        role_agreement=(
            role_matches / projection_compared_targets
            if projection_compared_targets
            else 1.0
        ),
        parent_edge_agreement=(
            parent_matches / projection_compared_targets
            if projection_compared_targets
            else 1.0
        ),
        decision_mode_agreement=(
            decision_matches / projection_compared_targets
            if projection_compared_targets
            else 1.0
        ),
        merged_review_node_count=len(merged_review_nodes),
        split_target_count=len(split_owner_targets),
        promoted_target_count=len(promoted_targets),
    )
    return LlmRequirementReviewDelta(
        prediction_id=prediction.prediction_id,
        revision=reviewed.revision,
        operations=tuple(operations),
        metrics=metrics,
    )


def build_llm_requirement_review_reference_candidate(
    session: LlmRequirementReviewSession,
) -> LlmRequirementReviewReferenceCandidate:
    """Derive a text-free, non-Gold receipt from one exact review revision."""

    validate_llm_requirement_review(session.prediction, session.reviewed_result)
    expected_delta = evaluate_llm_requirement_review(
        session.prediction,
        session.reviewed_result,
    )
    if expected_delta != session.delta:
        raise ValueError("review reference candidate delta does not match the session")

    prediction_sha256 = _canonical_review_payload_sha256(
        session.prediction.model_dump(mode="json")
    )
    reviewed_result_sha256 = _canonical_review_payload_sha256(
        session.reviewed_result.model_dump(mode="json")
    )
    delta_sha256 = _canonical_review_payload_sha256(
        session.delta.model_dump(mode="json")
    )
    identity_payload = {
        "session_id": session.session_id,
        "prediction_sha256": prediction_sha256,
        "reviewed_result_sha256": reviewed_result_sha256,
        "delta_sha256": delta_sha256,
        "revision": session.reviewed_result.revision,
    }
    identity_sha256 = _canonical_review_payload_sha256(identity_payload)

    unit_counts_by_owner: dict[str, int] = defaultdict(int)
    for node in session.prediction.nodes:
        unit_counts_by_owner[node.owner_structure_node_id] += 1

    captured = (
        session.reviewed_result.status is LlmRequirementReviewStatus.HUMAN_REVIEWED
    )
    return LlmRequirementReviewReferenceCandidate(
        reference_candidate_id=f"llm-review-reference-{identity_sha256[:32]}",
        status=(
            LlmRequirementReviewReferenceCandidateStatus.SELF_ATTESTED_REVIEW
            if captured
            else LlmRequirementReviewReferenceCandidateStatus.NOT_CAPTURED
        ),
        session_id=session.session_id,
        prediction_id=session.prediction.prediction_id,
        source_sha256=session.prediction.source_sha256,
        revision=session.reviewed_result.revision,
        prediction_sha256=prediction_sha256,
        reviewed_result_sha256=reviewed_result_sha256,
        delta_sha256=delta_sha256,
        semantic_unit_count=len(session.prediction.nodes),
        review_node_count=len(session.reviewed_result.nodes),
        review_operation_count=len(session.delta.operations),
        same_owner_multi_unit_group_count=sum(
            count > 1 for count in unit_counts_by_owner.values()
        ),
        edited_target_count=session.delta.metrics.edited_target_count,
        split_target_count=session.delta.metrics.split_target_count,
        merged_review_node_count=session.delta.metrics.merged_review_node_count,
        reviewer_label_present=bool(session.reviewed_result.reviewer_label.strip()),
        recorded_at_utc=session.updated_at_utc if captured else None,
        reason_codes=(
            (
                "self_attestation_not_independent",
                "trusted_reviewer_policy_not_configured",
            )
            if captured
            else (
                "human_review_not_completed",
                "trusted_reviewer_policy_not_configured",
            )
        ),
    )


def _canonical_review_payload_sha256(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _prediction_node(
    *,
    source_ir: SourceIRSnapshot,
    structure_node: StructureNode,
    semantic_unit_id: str,
    unit_order: int,
    review_owner_semantic_unit_id: str | None = None,
    review_owner_target_structure_node_id: str | None = None,
    semantic_class: SemanticClass,
    requirement_subtype: RequirementSubtype | None,
    include_in_view: bool,
    role: RequirementProjectionRole | None,
    parent_semantic_unit_id: str | None,
    parent_target_structure_node_id: str | None,
    decision_mode: RequirementDecisionMode,
    reason_codes: tuple[str, ...],
) -> LlmRequirementPredictionNode:
    semantic_source_refs = structure_node.effective_semantic_source_refs
    review_text = _source_refs_review_text(source_ir, semantic_source_refs)
    return LlmRequirementPredictionNode(
        semantic_unit_id=semantic_unit_id,
        source_semantic_unit_ids=(semantic_unit_id,),
        owner_structure_node_id=structure_node.structure_node_id,
        source_owner_structure_node_ids=(structure_node.structure_node_id,),
        review_owner_semantic_unit_id=review_owner_semantic_unit_id,
        review_owner_structure_node_id=(review_owner_target_structure_node_id),
        unit_order=unit_order,
        source_order=structure_node.source_order,
        semantic_class=semantic_class,
        requirement_subtype=requirement_subtype,
        include_in_view=include_in_view,
        role=role,
        parent_semantic_unit_id=parent_semantic_unit_id,
        parent_owner_structure_node_id=parent_target_structure_node_id,
        decision_mode=decision_mode,
        source_refs=semantic_source_refs,
        source_binding_sha256=source_binding_sha256(
            source_ir_id=source_ir.source_ir_id,
            semantic_unit_id=semantic_unit_id,
            source_semantic_unit_ids=(semantic_unit_id,),
            owner_structure_node_id=structure_node.structure_node_id,
            source_owner_structure_node_ids=(structure_node.structure_node_id,),
            source_refs=semantic_source_refs,
        ),
        review_text=review_text,
        review_text_sha256=hashlib.sha256(review_text.encode("utf-8")).hexdigest(),
        reason_codes=tuple(sorted(set(reason_codes))),
    )


def _review_owner_semantic_unit_id(
    node: LlmRequirementPredictionNode,
) -> str:
    return node.review_owner_semantic_unit_id or node.semantic_unit_id


def _structure_review_text(
    source_ir: SourceIRSnapshot,
    structure_node: StructureNode,
) -> str:
    return _source_refs_review_text(
        source_ir,
        structure_node.effective_semantic_source_refs,
    )


def _source_refs_review_text(
    source_ir: SourceIRSnapshot,
    source_refs: tuple[StructureSourceRef, ...],
) -> str:
    source_nodes = {item.source_node_id: item for item in source_ir.nodes}
    source_cells = {
        cell.cell_id: cell
        for node in source_ir.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
    }
    observations = {
        item.observation_id: item
        for snapshot in source_ir.visual_text_snapshots
        for item in snapshot.observations
    }
    parts: list[str] = []
    seen: set[str] = set()
    for source_ref in source_refs:
        text = _review_text_for_source_ref(
            source_ref,
            source_nodes=source_nodes,
            source_cells=source_cells,
            observations=observations,
        ).strip()
        if not text or text in seen:
            continue
        parts.append(text)
        seen.add(text)
    return "\n".join(parts)


def _review_text_for_source_ref(
    source_ref: StructureSourceRef,
    *,
    source_nodes: dict[str, object],
    source_cells: dict[str, object],
    observations: dict[str, object],
) -> str:
    if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
        source_node = source_nodes.get(source_ref.source_id)
        if source_node is None:
            raise ValueError("review source ref is absent from SourceIR")
        raw_text = str(getattr(source_node, "raw_text", "") or "")
        if raw_text.strip():
            return raw_text
        table = getattr(source_node, "table_payload", None)
        if table is None:
            return ""
        rows: dict[int, list[str]] = defaultdict(list)
        for cell in sorted(table.cells, key=lambda item: (item.row, item.column)):
            cell_text = str(
                cell.raw_text or cell.display_value or cell.formula or ""
            ).strip()
            if cell_text:
                rows[cell.row].append(cell_text)
        return "\n".join(" | ".join(rows[row]) for row in sorted(rows))
    if source_ref.kind is StructureSourceRefKind.TABLE_CELL:
        cell = source_cells.get(source_ref.source_id)
        if cell is None:
            raise ValueError("review table-cell ref is absent from SourceIR")
        return str(
            getattr(cell, "raw_text", "")
            or getattr(cell, "display_value", "")
            or getattr(cell, "formula", "")
            or ""
        )
    observation = observations.get(source_ref.source_id)
    if observation is None:
        raise ValueError("review visual-text ref is absent from SourceIR")
    return str(getattr(observation, "raw_text", "") or "")


def _require_grounded_structure_node(
    nodes_by_id: dict[str, StructureNode],
    target_id: str,
) -> StructureNode:
    node = nodes_by_id.get(target_id)
    if node is None:
        raise ValueError("LLM review target is absent from StructureTree")
    if not node.source_refs:
        raise ValueError("LLM review target has no immutable source grounding")
    return node


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return str(raw) if raw is not None else None


def _review_field_summary(
    nodes: list[LlmReviewedRequirementNode],
    field: str,
) -> str | bool | int | None:
    values: list[str | bool | int | None] = []
    for node in nodes:
        raw = getattr(getattr(node, field), "value", getattr(node, field))
        value = raw if raw is None or isinstance(raw, (str, bool, int)) else str(raw)
        if value not in values:
            values.append(value)
    if len(values) == 1:
        return values[0]
    return "|".join("null" if value is None else str(value) for value in values)


def _review_node_sort_key(
    node: LlmReviewedRequirementNode,
    prediction_by_unit: dict[str, LlmRequirementPredictionNode],
) -> tuple[int, int, int, int, str]:
    first_span = min(
        node.source_spans,
        key=lambda span: (
            prediction_by_unit[span.semantic_unit_id].source_order,
            prediction_by_unit[span.semantic_unit_id].unit_order,
            span.start_offset,
            span.end_offset,
            span.semantic_unit_id,
        ),
    )
    return (
        prediction_by_unit[first_span.semantic_unit_id].source_order,
        prediction_by_unit[first_span.semantic_unit_id].unit_order,
        first_span.start_offset,
        first_span.end_offset,
        node.review_node_id,
    )


def _prediction_node_sort_key(
    node: LlmRequirementPredictionNode,
) -> tuple[int, int, str]:
    return (node.source_order, node.unit_order, node.semantic_unit_id)


__all__ = [
    "build_llm_requirement_model_prediction",
    "build_llm_requirement_review_reference_candidate",
    "evaluate_llm_requirement_review",
    "initial_llm_requirement_review",
    "validate_llm_requirement_review",
]
