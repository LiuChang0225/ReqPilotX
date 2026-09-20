from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.application.extraction.llm_requirement_review import (
    validate_llm_requirement_review,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementReviewSession,
    LlmReviewSourceSpan,
)
from app.domain.extraction.requirement_enrichment import (
    RequirementAtomizationNode,
    RequirementAtomizationNodeKind,
    RequirementAtomizationRun,
    RequirementEnrichmentCoverageReceipt,
    RequirementEnrichmentLineage,
    RequirementPublicationEntry,
    RequirementPublicationEntryKind,
    RequirementPublicationPreview,
    RequirementRefinementBinding,
    RequirementRefinementOperation,
    RequirementRefinementStatus,
    RequirementRefinementSuggestion,
    canonical_model_sha256,
    stable_atomic_node_id,
    stable_atomization_node_id,
    stable_atomization_run_id,
    stable_publication_entry_id,
    stable_publication_key,
    stable_publication_lineage_sha256,
    stable_publication_preview_id,
    stable_refinement_lineage_sha256,
    stable_refinement_suggestion_id,
    stable_requirement_enrichment_lineage_sha256,
)
from app.domain.extraction.requirement_view import RequirementDecisionMode
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.table_evidence import TableEvidenceBindingV1
from app.ports.requirement_refinement_provider import (
    RequirementRefinementProviderPort,
    RequirementRefinementProviderRequest,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderError,
    RequirementAtomizationProviderPort,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
    RequirementAtomizationRange,
    RequirementAtomizationSourceSegment,
    RequirementAtomizationTableContext,
)


REQUIREMENT_ATOMIZATION_ALGORITHM_VERSION = "source-bound-provider-atomization/v3"


def build_requirement_atomization_run(
    session: LlmRequirementReviewSession,
    *,
    coverage: RequirementEnrichmentCoverageReceipt,
    provider: RequirementAtomizationProviderPort,
    max_concurrent_provider_calls: int = 1,
    table_evidence_by_review_node: (Mapping[str, TableEvidenceBindingV1] | None) = None,
) -> RequirementAtomizationRun:
    """Split technical DIRECT nodes only through source-range provider output."""

    if not isinstance(session, LlmRequirementReviewSession):
        raise TypeError("session must be LlmRequirementReviewSession")
    if not isinstance(coverage, RequirementEnrichmentCoverageReceipt):
        raise TypeError("coverage must be RequirementEnrichmentCoverageReceipt")
    if (
        isinstance(max_concurrent_provider_calls, bool)
        or not isinstance(max_concurrent_provider_calls, int)
        or not 1 <= max_concurrent_provider_calls <= 4
    ):
        raise ValueError("max_concurrent_provider_calls must be between 1 and 4")
    if not coverage.eligible:
        raise ValueError("atomization requires complete resolved semantic coverage")
    validate_llm_requirement_review(session.prediction, session.reviewed_result)
    lineage = build_requirement_enrichment_lineage(session, coverage=coverage)
    prediction_by_unit = {
        item.semantic_unit_id: item for item in session.prediction.nodes
    }
    included = tuple(
        item
        for item in session.reviewed_result.nodes
        if item.include_in_view
        and item.semantic_class is SemanticClass.REQUIREMENT
        and item.decision_mode
        in {RequirementDecisionMode.ROLLUP, RequirementDecisionMode.DIRECT}
    )
    direct_review_node_ids = {
        item.review_node_id
        for item in included
        if item.decision_mode is RequirementDecisionMode.DIRECT
    }
    evidence_by_review_node = _validated_table_evidence_mapping(
        table_evidence_by_review_node,
        direct_review_node_ids=direct_review_node_ids,
    )
    rollup_node_id_by_review_id = {
        item.review_node_id: stable_atomization_node_id(
            lineage_sha256=lineage.lineage_sha256,
            review_node_id=item.review_node_id,
            source_spans=item.source_spans,
            decision_mode=item.decision_mode,
        )
        for item in included
        if item.decision_mode is RequirementDecisionMode.ROLLUP
    }
    reviewed_by_id = {item.review_node_id: item for item in included}
    direct_requests: list[tuple[str, RequirementAtomizationProviderRequest]] = []
    for reviewed in included:
        if reviewed.requirement_subtype is None or reviewed.role is None:
            raise ValueError(
                "technical review nodes require subtype and projection role"
            )
        if reviewed.decision_mode is RequirementDecisionMode.ROLLUP:
            continue
        source_segments = _atomization_source_segments(
            reviewed.source_spans,
            prediction_by_unit=prediction_by_unit,
        )
        table_evidence = evidence_by_review_node.get(reviewed.review_node_id)
        if table_evidence is not None:
            _validate_table_evidence_source_boundary(
                reviewed.source_spans,
                table_evidence=table_evidence,
            )
        parent_context = ""
        if reviewed.parent_review_node_id is not None:
            parent_reviewed = reviewed_by_id.get(reviewed.parent_review_node_id)
            if parent_reviewed is not None:
                parent_context = _source_text_for_spans(
                    parent_reviewed.source_spans,
                    prediction_by_unit=prediction_by_unit,
                )
        direct_requests.append(
            (
                reviewed.review_node_id,
                RequirementAtomizationProviderRequest(
                    lineage_sha256=lineage.lineage_sha256,
                    review_node_id=reviewed.review_node_id,
                    requirement_subtype=reviewed.requirement_subtype.value,
                    parent_context_statement=parent_context,
                    source_segments=source_segments,
                    table_context=(
                        _atomization_table_context(table_evidence)
                        if table_evidence is not None
                        else None
                    ),
                ),
            )
        )
    if not direct_requests:
        raise ValueError("atomization requires at least one technical DIRECT node")
    requests = tuple(item[1] for item in direct_requests)
    if max_concurrent_provider_calls == 1:
        results = tuple(provider.atomize(request) for request in requests)
    else:
        with ThreadPoolExecutor(
            max_workers=max_concurrent_provider_calls,
            thread_name_prefix="requirement-atomization",
        ) as executor:
            results = tuple(executor.map(provider.atomize, requests))
    result_by_review_id = {
        review_node_id: result
        for (review_node_id, _request), result in zip(
            direct_requests,
            results,
            strict=True,
        )
    }

    nodes: list[RequirementAtomizationNode] = []
    provider_runs = []
    for reviewed in included:
        if reviewed.requirement_subtype is None or reviewed.role is None:
            raise ValueError(
                "technical review nodes require subtype and projection role"
            )
        if reviewed.decision_mode is RequirementDecisionMode.ROLLUP:
            node_id = rollup_node_id_by_review_id[reviewed.review_node_id]
            nodes.append(
                RequirementAtomizationNode(
                    node_id=node_id,
                    atomic_node_id=None,
                    kind=RequirementAtomizationNodeKind.ROLLUP,
                    parent_node_id=None,
                    review_node_id=reviewed.review_node_id,
                    source_semantic_unit_ids=reviewed.source_semantic_unit_ids,
                    source_owner_structure_node_ids=(
                        reviewed.source_owner_structure_node_ids
                    ),
                    source_spans=reviewed.source_spans,
                    source_order=reviewed.source_order,
                    role=reviewed.role,
                    decision_mode=reviewed.decision_mode,
                    requirement_subtype=reviewed.requirement_subtype,
                    source_text=_source_text_for_spans(
                        reviewed.source_spans,
                        prediction_by_unit=prediction_by_unit,
                    ),
                    atomic_statement=None,
                )
            )
            continue

        source_segments = _atomization_source_segments(
            reviewed.source_spans,
            prediction_by_unit=prediction_by_unit,
        )
        table_evidence = evidence_by_review_node.get(reviewed.review_node_id)
        result = result_by_review_id[reviewed.review_node_id]
        obligation_spans = _validated_atomization_obligation_spans(
            result,
            source_segments=source_segments,
        )
        provider_runs.append(result.run)
        parent_node_id = (
            rollup_node_id_by_review_id.get(reviewed.parent_review_node_id)
            if reviewed.parent_review_node_id is not None
            else None
        )
        for obligation, spans in zip(
            result.obligations,
            obligation_spans,
            strict=True,
        ):
            source_unit_ids = tuple(
                dict.fromkeys(item.semantic_unit_id for item in spans)
            )
            owner_by_unit = {
                item.semantic_unit_id: item.owner_structure_node_id for item in spans
            }
            node_id = stable_atomization_node_id(
                lineage_sha256=lineage.lineage_sha256,
                review_node_id=reviewed.review_node_id,
                source_spans=spans,
                decision_mode=RequirementDecisionMode.DIRECT,
                table_evidence_binding_id=(
                    table_evidence.binding_id if table_evidence is not None else None
                ),
            )
            nodes.append(
                RequirementAtomizationNode(
                    node_id=node_id,
                    atomic_node_id=stable_atomic_node_id(atomization_node_id=node_id),
                    kind=RequirementAtomizationNodeKind.ATOMIC_OBLIGATION,
                    parent_node_id=parent_node_id,
                    review_node_id=reviewed.review_node_id,
                    source_semantic_unit_ids=source_unit_ids,
                    source_owner_structure_node_ids=tuple(
                        owner_by_unit[unit_id] for unit_id in source_unit_ids
                    ),
                    source_spans=spans,
                    source_order=reviewed.source_order,
                    role=reviewed.role,
                    decision_mode=RequirementDecisionMode.DIRECT,
                    requirement_subtype=reviewed.requirement_subtype,
                    source_text=_source_text_for_spans(
                        spans,
                        prediction_by_unit=prediction_by_unit,
                    ),
                    atomic_statement=obligation.atomic_statement,
                    table_evidence_binding_id=(
                        table_evidence.binding_id
                        if table_evidence is not None
                        else None
                    ),
                )
            )
    frozen_nodes = tuple(nodes)
    frozen_runs = tuple(provider_runs)
    run_id = stable_atomization_run_id(
        lineage_sha256=lineage.lineage_sha256,
        revision=0,
        algorithm_version=REQUIREMENT_ATOMIZATION_ALGORITHM_VERSION,
        nodes=frozen_nodes,
        provider_runs=frozen_runs,
    )
    return RequirementAtomizationRun(
        atomization_run_id=run_id,
        revision=0,
        algorithm_version=REQUIREMENT_ATOMIZATION_ALGORITHM_VERSION,
        lineage=lineage,
        nodes=frozen_nodes,
        provider_runs=frozen_runs,
        created_at_utc=session.updated_at_utc,
    )


def build_requirement_enrichment_lineage(
    session: LlmRequirementReviewSession,
    *,
    coverage: RequirementEnrichmentCoverageReceipt,
) -> RequirementEnrichmentLineage:
    if not coverage.eligible:
        raise ValueError("enrichment lineage requires complete resolved coverage")
    reviewed_result_sha256 = canonical_model_sha256(session.reviewed_result)
    values = {
        "project_id": session.project_id,
        "document_id": session.document_id,
        "session_id": session.session_id,
        "prediction_id": session.prediction.prediction_id,
        "review_revision": session.reviewed_result.revision,
        "source_sha256": session.prediction.source_sha256,
        "structure_snapshot_id": session.prediction.structure_snapshot_id,
        "reviewed_result_sha256": reviewed_result_sha256,
        "coverage_sha256": coverage.coverage_sha256,
        "coverage_total_units": coverage.total_units,
        "semantic_planning_runtime_version": coverage.planning_runtime_version,
        "semantic_classifier_runtime_version": coverage.classifier_runtime_version,
        "semantic_assembler_runtime_version": coverage.assembler_runtime_version,
        "semantic_document_runtime_version": coverage.document_runtime_version,
    }
    return RequirementEnrichmentLineage(
        **values,
        lineage_sha256=stable_requirement_enrichment_lineage_sha256(**values),
    )


def build_requirement_refinement_suggestion(
    *,
    run: RequirementAtomizationRun,
    atomic_node_id: str,
    provider: RequirementRefinementProviderPort,
    now: datetime | None = None,
) -> RequirementRefinementSuggestion:
    atomic_node = _require_atomic_node(run, atomic_node_id)
    atomic_statement = _require_atomic_statement(atomic_node)
    same_source_nodes = tuple(
        item
        for item in run.atomic_nodes
        if item.review_node_id == atomic_node.review_node_id
    )
    source_context_statement = (
        "".join(item.source_text for item in same_source_nodes)
        if len(same_source_nodes) > 1
        else atomic_node.source_text
    )
    parent_context_statement = next(
        (
            item.source_text
            for item in run.nodes
            if item.node_id == atomic_node.parent_node_id
        ),
        "",
    )
    provider_result = provider.suggest(
        RequirementRefinementProviderRequest(
            lineage_sha256=run.lineage.lineage_sha256,
            atomization_run_id=run.atomization_run_id,
            atomization_revision=run.revision,
            atomic_node_id=atomic_node_id,
            requirement_subtype=atomic_node.requirement_subtype.value,
            original_statement=atomic_statement,
            source_context_statement=source_context_statement,
            parent_context_statement=parent_context_statement,
        )
    )
    timestamp = now or datetime.now(timezone.utc)
    lineage_sha256 = stable_refinement_lineage_sha256(
        project_id=run.lineage.project_id,
        document_id=run.lineage.document_id,
        session_id=run.lineage.session_id,
        atomization_run_id=run.atomization_run_id,
        atomization_revision=run.revision,
        atomic_node_id=atomic_node_id,
    )
    return RequirementRefinementSuggestion(
        suggestion_id=stable_refinement_suggestion_id(lineage_sha256=lineage_sha256),
        project_id=run.lineage.project_id,
        document_id=run.lineage.document_id,
        session_id=run.lineage.session_id,
        atomization_run_id=run.atomization_run_id,
        atomization_revision=run.revision,
        atomic_node_id=atomic_node_id,
        revision=0,
        status=RequirementRefinementStatus.DRAFT,
        original_statement=atomic_statement,
        suggested_statement=provider_result.suggested_statement.strip(),
        rationale=provider_result.rationale.strip(),
        provider_id=provider_result.run.provider_id,
        provider_version=provider_result.run.provider_version,
        model_id=provider_result.run.model_id,
        prompt_version=provider_result.run.prompt_version,
        provider_run_id=provider_result.run.provider_run_id,
        reason_codes=provider_result.reason_codes,
        lineage_sha256=lineage_sha256,
        created_at_utc=timestamp,
        updated_at_utc=timestamp,
    )


def revise_requirement_refinement_suggestion(
    current: RequirementRefinementSuggestion,
    *,
    expected_revision: int,
    operation: RequirementRefinementOperation,
    suggested_statement: str | None = None,
    rationale: str | None = None,
    decision_note: str | None = None,
    now: datetime | None = None,
) -> RequirementRefinementSuggestion:
    if current.revision != expected_revision:
        raise ValueError("refinement suggestion revision has changed")
    if operation is RequirementRefinementOperation.REVISE:
        if suggested_statement is None or not suggested_statement.strip():
            raise ValueError("REVISE requires suggested_statement")
        next_statement = suggested_statement.strip()
        next_rationale = (
            rationale.strip() if rationale is not None else current.rationale
        )
        next_status = RequirementRefinementStatus.DRAFT
        next_reason_codes = tuple(sorted({*current.reason_codes, "human_revision"}))
    else:
        if suggested_statement is not None or rationale is not None:
            raise ValueError("ACCEPT and REJECT cannot rewrite the provider suggestion")
        next_statement = current.suggested_statement
        next_rationale = current.rationale
        next_status = (
            RequirementRefinementStatus.ACCEPTED
            if operation is RequirementRefinementOperation.ACCEPT
            else RequirementRefinementStatus.REJECTED
        )
        next_reason_codes = current.reason_codes
    return RequirementRefinementSuggestion.model_validate(
        {
            **current.model_dump(mode="json"),
            "revision": current.revision + 1,
            "status": next_status,
            "suggested_statement": next_statement,
            "rationale": next_rationale,
            "decision_note": (
                decision_note.strip() if decision_note is not None else ""
            ),
            "reason_codes": next_reason_codes,
            "updated_at_utc": now or datetime.now(timezone.utc),
        }
    )


def build_requirement_publication_preview(
    *,
    session: LlmRequirementReviewSession,
    run: RequirementAtomizationRun,
    coverage: RequirementEnrichmentCoverageReceipt,
    refinements: tuple[RequirementRefinementSuggestion, ...],
    base_active_requirement_set_id: str,
    now: datetime | None = None,
) -> RequirementPublicationPreview:
    lineage = build_requirement_enrichment_lineage(session, coverage=coverage)
    if run.lineage != lineage:
        raise ValueError("atomization run is stale for the review session")
    if not run.nodes:
        raise ValueError("atomization run has no publishable technical nodes")
    refinements_by_atomic_id: dict[str, RequirementRefinementSuggestion] = {}
    for refinement in refinements:
        if (
            refinement.project_id != session.project_id
            or refinement.document_id != session.document_id
            or refinement.session_id != session.session_id
            or refinement.atomization_run_id != run.atomization_run_id
            or refinement.atomization_revision != run.revision
        ):
            raise ValueError("refinement suggestion is bound to another lineage")
        if refinement.atomic_node_id in refinements_by_atomic_id:
            raise ValueError("an atomic obligation has multiple refinements")
        _require_atomic_node(run, refinement.atomic_node_id)
        refinements_by_atomic_id[refinement.atomic_node_id] = refinement
    refinement_bindings = tuple(
        RequirementRefinementBinding(
            suggestion_id=item.suggestion_id,
            atomic_node_id=item.atomic_node_id,
            revision=item.revision,
            status=item.status,
            lineage_sha256=item.lineage_sha256,
        )
        for item in sorted(
            refinements,
            key=lambda candidate: (
                _require_atomic_node(
                    run,
                    candidate.atomic_node_id,
                ).source_order,
                candidate.atomic_node_id,
            ),
        )
    )
    publication_lineage_sha256 = stable_publication_lineage_sha256(
        project_id=session.project_id,
        document_id=session.document_id,
        session_id=session.session_id,
        atomization_run_id=run.atomization_run_id,
        atomization_revision=run.revision,
        base_active_requirement_set_id=base_active_requirement_set_id,
        refinement_bindings=refinement_bindings,
    )
    entry_id_by_node_id = {
        node.node_id: stable_publication_entry_id(
            publication_lineage_sha256=publication_lineage_sha256,
            atomization_node_id=node.node_id,
        )
        for node in run.nodes
    }
    entries: list[RequirementPublicationEntry] = []
    for index, node in enumerate(run.nodes, start=1):
        refinement = (
            refinements_by_atomic_id.get(node.atomic_node_id)
            if node.atomic_node_id is not None
            else None
        )
        accepted = bool(
            refinement is not None
            and refinement.status is RequirementRefinementStatus.ACCEPTED
        )
        unrefined_statement = (
            _require_atomic_statement(node)
            if node.kind is RequirementAtomizationNodeKind.ATOMIC_OBLIGATION
            else node.source_text
        )
        entries.append(
            RequirementPublicationEntry(
                publication_entry_id=entry_id_by_node_id[node.node_id],
                atomization_node_id=node.node_id,
                atomic_node_id=node.atomic_node_id,
                parent_publication_entry_id=(
                    entry_id_by_node_id.get(node.parent_node_id)
                    if node.parent_node_id is not None
                    else None
                ),
                review_node_id=node.review_node_id,
                entry_kind=(
                    RequirementPublicationEntryKind.HEADING
                    if node.kind is RequirementAtomizationNodeKind.ROLLUP
                    else RequirementPublicationEntryKind.REQUIREMENT
                ),
                req_code=f"AI-{index:04d}",
                requirement_type=node.requirement_subtype.value,
                decision_mode=node.decision_mode,
                original_statement=node.source_text,
                published_statement=(
                    refinement.suggested_statement
                    if accepted and refinement is not None
                    else unrefined_statement
                ),
                source_spans=node.source_spans,
                source_order=node.source_order,
                table_evidence_binding_id=node.table_evidence_binding_id,
                refinement_suggestion_id=(
                    refinement.suggestion_id
                    if accepted and refinement is not None
                    else None
                ),
                refinement_revision=(
                    refinement.revision if accepted and refinement is not None else None
                ),
            )
        )
    frozen_entries = tuple(entries)
    publication_key = stable_publication_key(
        lineage_sha256=publication_lineage_sha256,
        entries=frozen_entries,
    )
    return RequirementPublicationPreview(
        preview_id=stable_publication_preview_id(publication_key=publication_key),
        project_id=session.project_id,
        document_id=session.document_id,
        session_id=session.session_id,
        atomization_run_id=run.atomization_run_id,
        atomization_revision=run.revision,
        base_active_requirement_set_id=base_active_requirement_set_id,
        lineage_sha256=publication_lineage_sha256,
        publication_key=publication_key,
        refinement_bindings=refinement_bindings,
        entries=frozen_entries,
        created_at_utc=now or datetime.now(timezone.utc),
    )


def _atomization_source_segments(
    spans: tuple[LlmReviewSourceSpan, ...],
    *,
    prediction_by_unit: dict[str, object],
) -> tuple[RequirementAtomizationSourceSegment, ...]:
    segments: list[RequirementAtomizationSourceSegment] = []
    for index, span in enumerate(spans):
        prediction = prediction_by_unit.get(span.semantic_unit_id)
        if prediction is None:
            raise ValueError("review source span has no prediction source")
        review_text = str(getattr(prediction, "review_text"))
        if span.end_offset > len(review_text):
            raise ValueError("review source span exceeds prediction text")
        source_text = review_text[span.start_offset : span.end_offset]
        if not source_text:
            raise ValueError("technical DIRECT nodes require non-empty source spans")
        segments.append(
            RequirementAtomizationSourceSegment(
                segment_index=index,
                semantic_unit_id=span.semantic_unit_id,
                owner_structure_node_id=span.owner_structure_node_id,
                source_start_offset=span.start_offset,
                source_end_offset=span.end_offset,
                source_text=source_text,
            )
        )
    return tuple(segments)


def _validated_table_evidence_mapping(
    table_evidence_by_review_node: (Mapping[str, TableEvidenceBindingV1] | None),
    *,
    direct_review_node_ids: set[str],
) -> dict[str, TableEvidenceBindingV1]:
    if table_evidence_by_review_node is None:
        return {}
    if not isinstance(table_evidence_by_review_node, Mapping):
        raise TypeError("table_evidence_by_review_node must be a mapping")
    result: dict[str, TableEvidenceBindingV1] = {}
    for review_node_id, table_evidence in table_evidence_by_review_node.items():
        if not isinstance(review_node_id, str) or not review_node_id.strip():
            raise TypeError("table evidence mapping keys must be review node IDs")
        if not isinstance(table_evidence, TableEvidenceBindingV1):
            raise TypeError(
                "table evidence mapping values must be TableEvidenceBindingV1"
            )
        if review_node_id not in direct_review_node_ids:
            raise ValueError(
                "table evidence can bind only a publishable DIRECT review node"
            )
        result[review_node_id] = table_evidence
    return result


def _validate_table_evidence_source_boundary(
    source_spans: tuple[LlmReviewSourceSpan, ...],
    *,
    table_evidence: TableEvidenceBindingV1,
) -> None:
    if not table_evidence.source_spans:
        raise ValueError(
            "table-backed atomization requires source-bound record evidence"
        )
    evidence_ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for span in table_evidence.source_spans:
        evidence_ranges.setdefault(
            (span.semantic_unit_id, span.owner_structure_node_id),
            [],
        ).append((span.start_offset, span.end_offset))
    for ranges in evidence_ranges.values():
        ranges.sort()

    for source_span in source_spans:
        ranges = evidence_ranges.get(
            (
                source_span.semantic_unit_id,
                source_span.owner_structure_node_id,
            ),
            [],
        )
        cursor = source_span.start_offset
        for start_offset, end_offset in ranges:
            if end_offset <= cursor:
                continue
            if start_offset > cursor:
                break
            cursor = max(cursor, end_offset)
            if cursor >= source_span.end_offset:
                break
        if cursor < source_span.end_offset:
            raise ValueError(
                "atomization request cannot cross table record evidence boundaries"
            )


def _atomization_table_context(
    table_evidence: TableEvidenceBindingV1,
) -> RequirementAtomizationTableContext:
    return RequirementAtomizationTableContext(
        table_evidence_binding_id=table_evidence.binding_id,
        logical_table_id=table_evidence.logical_table_id,
        projection_id=table_evidence.projection_id,
        projection_revision=table_evidence.projection_revision,
        projection_sha256=table_evidence.projection_sha256,
        record_id=table_evidence.record_id,
        header_resolver_refs=tuple(
            dict.fromkeys(item.resolver_key for item in table_evidence.header_refs)
        ),
        shared_value_resolver_refs=tuple(
            dict.fromkeys(
                item.resolver_key for item in table_evidence.shared_value_refs
            )
        ),
        note_resolver_refs=tuple(
            dict.fromkeys(item.resolver_key for item in table_evidence.note_refs)
        ),
        image_resolver_refs=tuple(
            dict.fromkeys(item.resolver_key for item in table_evidence.image_refs)
        ),
    )


def _validated_atomization_obligation_spans(
    result: RequirementAtomizationProviderResult,
    *,
    source_segments: tuple[RequirementAtomizationSourceSegment, ...],
) -> tuple[tuple[LlmReviewSourceSpan, ...], ...]:
    if result.run.status is SemanticProviderRunStatus.SKIPPED:
        _validate_skipped_pass_through_result(
            result,
            source_segments=source_segments,
        )
    elif result.run.status is not SemanticProviderRunStatus.SUCCESS:
        raise RequirementAtomizationProviderError(
            str(result.run.error_code or "atomization_provider_unresolved"),
            "The atomization provider did not resolve this technical requirement.",
        )
    obligation_count = len(result.obligations)
    if any(
        item.split_basis
        != (
            "single_or_descriptive"
            if obligation_count == 1
            else "independent_obligation"
        )
        for item in result.obligations
    ):
        raise ValueError("atomization split basis does not match obligation count")

    if any(item.disposition.value == "UNRESOLVED" for item in result.context_ranges):
        raise RequirementAtomizationProviderError(
            "atomization_context_unresolved",
            "The atomization provider left source-bound context unresolved.",
        )
    coverage_entries: list[tuple[int, int, int, str, int]] = []
    for obligation_index, obligation in enumerate(result.obligations):
        coverage_entries.extend(
            (
                source_range.segment_index,
                source_range.start_offset,
                source_range.end_offset,
                "obligation",
                obligation_index,
            )
            for source_range in obligation.source_ranges
        )
    coverage_entries.extend(
        (
            context.source_range.segment_index,
            context.source_range.start_offset,
            context.source_range.end_offset,
            "context",
            context_index,
        )
        for context_index, context in enumerate(result.context_ranges)
    )
    coverage_entries.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    current_segment_index = 0
    current_offset = 0
    mapped_by_obligation: list[list[LlmReviewSourceSpan]] = [
        [] for _item in result.obligations
    ]
    for segment_index, start_offset, end_offset, kind, owner_index in coverage_entries:
        if segment_index >= len(source_segments):
            raise ValueError(
                "atomization provider referenced an unknown source segment"
            )
        source_segment = source_segments[segment_index]
        if (
            end_offset > len(source_segment.source_text)
            or segment_index != current_segment_index
            or start_offset != current_offset
        ):
            raise ValueError(
                "atomization provider obligations and context must exactly cover source in order"
            )
        if kind == "obligation":
            mapped_by_obligation[owner_index].append(
                LlmReviewSourceSpan(
                    semantic_unit_id=source_segment.semantic_unit_id,
                    owner_structure_node_id=source_segment.owner_structure_node_id,
                    start_offset=source_segment.source_start_offset + start_offset,
                    end_offset=source_segment.source_start_offset + end_offset,
                )
            )
        current_offset = end_offset
        if current_offset == len(source_segment.source_text):
            current_segment_index += 1
            current_offset = 0
    mapped_obligations = [
        _coalesce_review_spans(tuple(mapped)) for mapped in mapped_by_obligation
    ]
    if current_segment_index != len(source_segments) or current_offset != 0:
        raise ValueError("atomization provider omitted immutable source content")
    return tuple(mapped_obligations)


def _validate_skipped_pass_through_result(
    result: RequirementAtomizationProviderResult,
    *,
    source_segments: tuple[RequirementAtomizationSourceSegment, ...],
) -> None:
    expected_ranges = tuple(
        RequirementAtomizationRange(
            segment_index=item.segment_index,
            start_offset=0,
            end_offset=len(item.source_text),
        )
        for item in source_segments
    )
    source_text = "".join(item.source_text for item in source_segments)
    if (
        len(result.obligations) != 1
        or result.context_ranges
        or result.obligations[0].source_ranges != expected_ranges
        or result.obligations[0].atomic_statement != source_text
        or result.obligations[0].split_basis != "single_or_descriptive"
        or result.obligations[0].reason_codes != ("local_pass_through",)
    ):
        raise RequirementAtomizationProviderError(
            "invalid_skipped_atomization",
            "Skipped atomization must be an exact one-to-one source pass-through.",
        )


def _coalesce_review_spans(
    spans: tuple[LlmReviewSourceSpan, ...],
) -> tuple[LlmReviewSourceSpan, ...]:
    coalesced: list[LlmReviewSourceSpan] = []
    for span in spans:
        previous = coalesced[-1] if coalesced else None
        if (
            previous is not None
            and previous.semantic_unit_id == span.semantic_unit_id
            and previous.owner_structure_node_id == span.owner_structure_node_id
            and previous.end_offset == span.start_offset
        ):
            coalesced[-1] = previous.model_copy(update={"end_offset": span.end_offset})
        else:
            coalesced.append(span)
    return tuple(coalesced)


def _source_text_for_spans(
    spans: tuple[LlmReviewSourceSpan, ...],
    *,
    prediction_by_unit: dict[str, object],
) -> str:
    parts: list[str] = []
    for span in spans:
        prediction = prediction_by_unit.get(span.semantic_unit_id)
        if prediction is None:
            raise ValueError("review source span has no prediction source")
        review_text = str(getattr(prediction, "review_text"))
        if span.end_offset > len(review_text):
            raise ValueError("review source span exceeds prediction text")
        parts.append(review_text[span.start_offset : span.end_offset])
    result = "".join(parts)
    if not result.strip():
        raise ValueError("atomization source text must not be blank")
    return result


def _require_atomic_node(
    run: RequirementAtomizationRun,
    atomic_node_id: str,
) -> RequirementAtomizationNode:
    match = next(
        (item for item in run.atomic_nodes if item.atomic_node_id == atomic_node_id),
        None,
    )
    if match is None:
        raise ValueError("atomic_node_id does not belong to the atomization run")
    return match


def _require_atomic_statement(node: RequirementAtomizationNode) -> str:
    if node.atomic_statement is None:
        raise ValueError("atomic obligation has no independent atomic statement")
    return node.atomic_statement


__all__ = [
    "REQUIREMENT_ATOMIZATION_ALGORITHM_VERSION",
    "build_requirement_atomization_run",
    "build_requirement_enrichment_lineage",
    "build_requirement_publication_preview",
    "build_requirement_refinement_suggestion",
    "revise_requirement_refinement_suggestion",
]
