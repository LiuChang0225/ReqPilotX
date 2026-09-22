from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from reqpilotx_server.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx_server.application.extraction.build_llm_shadow_preview import (
    LlmShadowPreviewRejected,
    LlmShadowPreviewRejectionCode,
    _build_partial_preview_payload,
    _build_preview_payload,
    _elapsed_ms,
    _emit_preparation_progress,
    _known_rejection,
    _render_semantic_images,
    _scope_source_page_numbers,
    _scoped_visual_incompleteness_cause_code,
    _semantic_image_page_numbers,
    _validate_document_request,
)
from reqpilotx_server.application.extraction.build_shadow_preview import (
    ShadowPreviewRejected,
    build_document_view,
)
from reqpilotx_server.application.extraction.llm_provision_batch import (
    MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
    LlmProvisionBatchRejected,
    LlmProvisionExtractionOutcome,
    LlmRequirementDocumentCheckpoint,
    LlmRequirementDocumentProgress,
    LlmRequirementDocumentResult,
    extract_requirement_document_with_provider,
)
from reqpilotx_server.application.extraction.locate_llm_scope import (
    LlmScopeLocationRejected,
    locate_llm_document_scope,
)
from reqpilotx_server.application.extraction.orchestrate_structure_agent import (
    initialize_structure_repair_without_provider,
)
from reqpilotx_server.application.extraction.processing_pipeline import (
    build_source_ir_and_receipts_for_plan,
    processing_plan_visual_strategy,
)
from reqpilotx_server.application.extraction.resolve_structure import (
    StructureResolutionRejected,
    build_structure_tree,
)
from reqpilotx_server.domain.extraction.processing_profile import ResolvedProcessingPlanV1
from reqpilotx_server.domain.extraction.structure_revision import StructureRevisionV1
from reqpilotx_server.ports.semantic_provider import PreflightSemanticProviderPort
from reqpilotx_server.services.visual_text_snapshot_cache import (
    VisualTextProviderTelemetryCallback,
    VisualTextSnapshotCacheBinding,
)


def build_processing_plan_document_preview(
    source_path: Path,
    *,
    plan: ResolvedProcessingPlanV1,
    provider: PreflightSemanticProviderPort,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
    max_concurrent_chunks: int = MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
    progress_callback: Callable[[LlmRequirementDocumentProgress], None] | None = None,
    preparation_progress_callback: Callable[[str], None] | None = None,
    page_progress_callback: Callable[[int, int, int], None] | None = None,
    page_staging_callback: Callable[[dict[str, Any]], None] | None = None,
    visual_text_snapshot_cache: VisualTextSnapshotCacheBinding | None = None,
    provider_telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
    partial_result_callback: Callable[[dict[str, Any]], None] | None = None,
    include_source_read_model: bool = False,
) -> dict[str, Any]:
    """Run the existing semantic/presenter core over a plan-resolved SourceIR."""

    if partial_result_callback is not None and not callable(partial_result_callback):
        raise TypeError("partial_result_callback must be callable")
    expected_hash, _ = _validate_document_request(
        expected_source_sha256=plan.source_sha256,
        provider=provider,
    )
    path = Path(source_path)
    timings: dict[str, float] = {}

    _emit_preparation_progress(preparation_progress_callback, "preflight")
    started = perf_counter()
    report = LocalDocumentPreflight().inspect(path)
    timings["preflight"] = _elapsed_ms(started)
    if report.identity.detected_format.value != "pdf":
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.UNSUPPORTED_DOCUMENT_FORMAT,
            stage="preflight",
        )
    if report.source_sha256 != expected_hash:
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
            stage="preflight",
        )

    _emit_preparation_progress(preparation_progress_callback, "source_ir")
    started = perf_counter()
    try:
        source_ir, page_receipts = build_source_ir_and_receipts_for_plan(
            path,
            plan=plan,
            page_progress_callback=page_progress_callback,
            page_staging_callback=page_staging_callback,
            visual_text_snapshot_cache=visual_text_snapshot_cache,
            provider_telemetry_callback=provider_telemetry_callback,
        )
    except Exception as exc:
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.SOURCE_IR_REJECTED,
            stage="source_ir",
            cause_code=_safe_source_ir_failure_code(exc),
        ) from exc
    timings["source_ir"] = _elapsed_ms(started)
    visual_strategy = processing_plan_visual_strategy(plan)

    _emit_preparation_progress(preparation_progress_callback, "structure_build")
    started = perf_counter()
    try:
        base_tree = build_structure_tree(source_ir)
    except StructureResolutionRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.STRUCTURE_REJECTED,
            "structure",
            exc,
        ) from exc
    timings["structure"] = _elapsed_ms(started)
    structure_repair = initialize_structure_repair_without_provider(
        source_ir,
        base_tree,
        error_code="structure_audit_not_requested",
        revisions=structure_revisions,
    )
    tree = structure_repair.effective_structure
    timings["structure_audit"] = 0.0

    _emit_preparation_progress(preparation_progress_callback, "scope_planning")
    started = perf_counter()
    try:
        located_scope = locate_llm_document_scope(source_ir, tree)
    except LlmScopeLocationRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.SCOPE_REJECTED,
            "scope_location",
            exc,
        ) from exc
    timings["scope_location"] = _elapsed_ms(started)
    scope_page_numbers = _scope_source_page_numbers(
        source_ir,
        tree=tree,
        scope_structure_node_ids=located_scope.scope_structure_node_ids,
    )
    _emit_preparation_progress(
        preparation_progress_callback,
        "scoped_visual_validation",
    )
    scoped_visual_cause_code = _scoped_visual_incompleteness_cause_code(
        source_ir,
        scope_page_numbers,
        structure_tree=tree,
    )
    if scoped_visual_cause_code:
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.SCOPED_VISUAL_INCOMPLETE,
            stage="scoped_visual_gate",
            cause_code=scoped_visual_cause_code,
        )
    semantic_page_numbers = _semantic_image_page_numbers(
        source_ir,
        tree=tree,
        scope_structure_node_ids=located_scope.scope_structure_node_ids,
    )
    timings["semantic_images"] = 0.0

    completed_by_chunk_index: dict[int, LlmProvisionExtractionOutcome] = {}
    partial_document_view: dict[str, Any] | None = None

    def publish_partial_result(
        checkpoint: LlmRequirementDocumentCheckpoint,
    ) -> None:
        nonlocal partial_document_view
        if partial_result_callback is None:
            return
        if checkpoint.sequence != len(completed_by_chunk_index) + 1:
            raise ValueError("document checkpoint sequence is not monotonic")
        if checkpoint.chunk_index in completed_by_chunk_index:
            raise ValueError("document checkpoint chunk was already published")
        completed_by_chunk_index[checkpoint.chunk_index] = checkpoint.outcome
        if partial_document_view is None:
            partial_document_view = build_document_view(source_ir, tree)
        partial = _build_partial_preview_payload(
            tree=tree,
            checkpoint=checkpoint,
            completed_by_chunk_index=completed_by_chunk_index,
            document_view=partial_document_view,
            report=report,
            provider=provider,
            structure_repair=structure_repair,
            visual_strategy=visual_strategy,
            visual_observation_count=len(source_ir.visual_text_snapshots),
        )
        partial["processing_plan"] = plan.model_dump(mode="json")
        partial_result_callback(partial)

    started = perf_counter()
    try:
        batch = extract_requirement_document_with_provider(
            source_ir,
            tree,
            provider,
            located_scope,
            image_loader=lambda page_numbers: _render_semantic_images(
                path,
                source_sha256=expected_hash,
                page_numbers=page_numbers,
            ),
            max_images_per_classification_request=12,
            max_concurrent_chunks=max_concurrent_chunks,
            progress_callback=progress_callback,
            checkpoint_callback=(
                publish_partial_result
                if partial_result_callback is not None
                else None
            ),
        )
    except LlmProvisionBatchRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.BATCH_REJECTED,
            "llm_batch",
            exc,
        ) from exc
    timings["llm_batch"] = _elapsed_ms(started)
    assert isinstance(batch, LlmRequirementDocumentResult)
    semantic_page_numbers = batch.semantic_image_page_numbers

    started = perf_counter()
    try:
        payload = _build_preview_payload(
            source_ir=source_ir,
            tree=tree,
            located_scope=located_scope,
            batch=batch,
            report=report,
            semantic_page_numbers=semantic_page_numbers,
            timings=timings,
            structure_audit=None,
            structure_audit_error_code="structure_audit_not_requested",
            structure_repair=structure_repair,
            visual_strategy=visual_strategy,
            max_concurrent_chunks=max_concurrent_chunks,
            collection="llm_requirement_document",
        )
    except ShadowPreviewRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.PRESENTER_REJECTED,
            "presenter",
            exc,
        ) from exc
    payload["pipeline"]["requirement_view"]["duration_ms"] = _elapsed_ms(started)
    payload["processing_plan"] = plan.model_dump(mode="json")
    payload["page_processing_receipts"] = [
        receipt.as_dict() for receipt in page_receipts
    ]
    payload["degraded_page_numbers"] = [
        receipt.page_number
        for receipt in page_receipts
        if receipt.state == "DEGRADED"
    ]
    if include_source_read_model:
        from reqpilotx_server.application.extraction.structure_presentation import (
            build_structure_presentation_index,
        )
        from reqpilotx_server.application.extraction.source_page_facts import (
            SOURCE_PAGE_PREVIEW_KEY,
            build_source_page_preview,
        )

        payload["structure_presentation_index"] = (
            build_structure_presentation_index(
                source_ir,
                structure=tree,
            )
        )
        page_preview = build_source_page_preview(
            path,
            source_ir=source_ir,
            structure=tree,
        )
        if page_preview is not None:
            payload[SOURCE_PAGE_PREVIEW_KEY] = page_preview
    return payload


def _safe_source_ir_failure_code(exc: Exception) -> str:
    value = str(exc or "").strip()
    if value and all(
        character.islower() or character.isdigit() or character == "_"
        for character in value
    ) and len(value) <= 119:
        return value
    return "processing_plan_source_ir_failed"


__all__ = ["build_processing_plan_document_preview"]
