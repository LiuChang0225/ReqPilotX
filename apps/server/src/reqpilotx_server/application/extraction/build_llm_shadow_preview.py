from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from enum import Enum
from os import PathLike
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Callable

try:
    import pymupdf
except ImportError:  # pragma: no cover - exercised through the rejection boundary
    pymupdf = None

from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from reqpilotx_server.adapters.documents.pdf_visual_text_runtime import (
    validate_pdf_visual_text_capture,
)
from reqpilotx_server.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx_server.application.extraction.build_shadow_preview import (
    ShadowPreviewRejected,
    ShadowPreviewRejectionCode,
    _VISUAL_TEXT_TERMINAL_FAILURE_DIAGNOSTIC_CODES,
    _context_blocks,
    _display_label,
    _findings_by_candidate,
    _join_context_block_text,
    _join_ref_text,
    _pages_from_context_blocks,
    _serialize_candidate,
    _serialize_finding,
    _serialize_requirement_groups,
    _serialize_review_groups,
    _resolve_structure_node_refs,
    _resolve_structure_subtree_refs,
    _resolve_refs,
    _structure_ancestor_path,
    _view_depths,
    build_document_view,
)
from reqpilotx_server.application.extraction.build_semantic_unit_plan import (
    build_semantic_unit_plan,
)
from reqpilotx_server.application.extraction.agent_native_document import (
    AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION,
    AgentNativeDocumentResult,
    AgentNativeDocumentStatus,
    extract_agent_native_document_with_provider,
    serialize_agent_native_document_result,
)
from reqpilotx_server.application.extraction.document_parent_boundaries import (
    DocumentParentBoundaryProviderPort,
)
from reqpilotx_server.application.extraction.classify_content import classify_content
from reqpilotx_server.application.extraction.audit_structure import (
    StructureAuditResult,
)
from reqpilotx_server.application.extraction.build_source_ir import (
    SourceIRBuildRejected,
    build_source_ir,
)
from reqpilotx_server.application.extraction.llm_provision_batch import (
    LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
    LlmProvisionBatchRejected,
    LlmProvisionExtractionOutcome,
    LlmProvisionExtractionStatus,
    LlmRequirementBatchResult,
    LlmRequirementDocumentCheckpoint,
    LlmRequirementDocumentProgress,
    LlmRequirementDocumentResult,
    extract_requirement_document_with_provider,
    extract_requirement_provisions_with_provider,
)
from reqpilotx_server.application.extraction.requirement_tree import (
    REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.llm_semantics import (
    MODEL_REQUIREMENT_ASSEMBLER_VERSION,
    MODEL_SEMANTIC_CLASSIFIER_VERSION,
    SEMANTIC_PLANNING_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.locate_llm_scope import (
    LlmExtractionScopeLocator,
    LlmScopeLocationRejected,
    LlmSectionScopeSelector,
    LocatedLlmExtractionScope,
    locate_llm_document_scope,
    locate_llm_extraction_scope,
)
from reqpilotx_server.application.extraction.llm_requirement_review import (
    build_llm_requirement_model_prediction,
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
)
from reqpilotx_server.application.extraction.orchestrate_structure_agent import (
    DocumentRequirementAgentStage,
    DocumentRequirementAgentStageStatus,
    StructureRepairResult,
    build_agent_stage_states,
    initialize_structure_repair_without_provider,
    run_structure_repair_with_provider,
)
from reqpilotx_server.application.extraction.resolve_structure import (
    StructureResolutionRejected,
    build_structure_tree,
    visual_text_authority_pages,
)
from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LLM_REQUIREMENT_REVIEW_SESSION_SCHEMA_VERSION,
)
from reqpilotx_server.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from reqpilotx_server.domain.extraction.semantics import SemanticClass
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind
from reqpilotx_server.domain.extraction.visual_text import VisualTextPageInput, VisualTextSnapshot
from reqpilotx_server.domain.extraction.structure import (
    StructureFinding,
    StructureFindingSeverity,
    StructureNode,
    StructureNodeKind,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from reqpilotx_server.domain.extraction.structure_revision import StructureRevisionV1
from reqpilotx_server.ports.semantic_provider import (
    PreflightSemanticProviderPort,
    SemanticImageInput,
    SemanticProviderRequestRejected,
)
from reqpilotx_server.ports.structure_audit_provider import StructureAuditProviderPort
from reqpilotx_server.ports.visual_text_provider import (
    VisualTextCaptureCancelled,
    VisualTextProviderPort,
)
from reqpilotx_server.services.extraction_visual_routes import (
    AuthorizedPdfVisualRuntime,
    extraction_visual_strategy_identity,
)
from reqpilotx_server.services.visual_text_snapshot_cache import (
    VisualTextSnapshotCacheBinding,
)


LLM_SHADOW_PREVIEW_SCHEMA_VERSION = "extraction-llm-shadow-preview/v5"
MAX_LLM_SHADOW_SEMANTIC_IMAGES = 12
_SEMANTIC_IMAGE_PIXEL_BUDGET = 2_750_000
_MAX_SEMANTIC_IMAGE_SCALE = 1.5
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SCOPED_VISUAL_INCOMPLETE_CODES = _VISUAL_TEXT_TERMINAL_FAILURE_DIAGNOSTIC_CODES
logger = logging.getLogger(__name__)


class LlmShadowPreviewProgressCancelled(VisualTextCaptureCancelled):
    """Stop preview work when its caller invalidates the active job binding."""


class _VisualCaptureProgressProvider:
    """Observe completed remote page calls without changing provider identity."""

    def __init__(
        self,
        delegate: VisualTextProviderPort,
        *,
        authorized_upper_bound: int,
        callback: Callable[[int, int, int], None],
    ) -> None:
        self.provider_id = delegate.provider_id
        self.provider_version = delegate.provider_version
        self._delegate = delegate
        self._authorized_upper_bound = authorized_upper_bound
        self._callback = callback
        self._completed = 0
        self._lock = Lock()

    def provider_version_for_page(self, page_number: int) -> str:
        version_for_page = getattr(self._delegate, "provider_version_for_page", None)
        if callable(version_for_page):
            return str(version_for_page(page_number))
        return self.provider_version

    def recognize_page(
        self,
        page_input: VisualTextPageInput,
    ) -> VisualTextSnapshot:
        # Let the adapter's existing provider rejection/diagnostic boundary see
        # the delegate exception unchanged. A failed remote call is still a
        # completed call for progress purposes, while the existing adapter gate
        # remains solely responsible for its business outcome.
        try:
            return self._delegate.recognize_page(page_input)
        finally:
            self.report_completed_page(page_input.page_number)

    def report_completed_page(self, page_number: int) -> None:
        with self._lock:
            self._completed += 1
            completed = self._completed
            try:
                self._callback(
                    completed,
                    self._authorized_upper_bound,
                    page_number,
                )
            except LlmShadowPreviewProgressCancelled:
                raise
            except Exception:
                logger.warning(
                    "PDF visual capture progress callback failed after page %s.",
                    page_number,
                    exc_info=True,
                )


class LlmShadowPreviewRejectionCode(str, Enum):
    INVALID_REQUEST = "llm_shadow_preview_invalid_request"
    UNSUPPORTED_DOCUMENT_FORMAT = "llm_shadow_preview_unsupported_document_format"
    SOURCE_HASH_MISMATCH = "llm_shadow_preview_source_hash_mismatch"
    SOURCE_IR_REJECTED = "llm_shadow_preview_source_ir_rejected"
    VISUAL_CAPTURE_REJECTED = "llm_shadow_preview_visual_capture_rejected"
    STRUCTURE_REJECTED = "llm_shadow_preview_structure_rejected"
    SCOPE_REJECTED = "llm_shadow_preview_scope_rejected"
    SCOPED_VISUAL_INCOMPLETE = "llm_shadow_preview_scoped_visual_incomplete"
    SEMANTIC_IMAGE_LIMIT_EXCEEDED = "llm_shadow_preview_semantic_image_limit_exceeded"
    SEMANTIC_IMAGE_RENDER_REJECTED = "llm_shadow_preview_semantic_image_render_rejected"
    BATCH_REJECTED = "llm_shadow_preview_batch_rejected"
    PRESENTER_REJECTED = "llm_shadow_preview_presenter_rejected"


class LlmShadowPreviewRejected(RuntimeError):
    """Stable, source-text-free rejection at the read-only product boundary."""

    def __init__(
        self,
        code: LlmShadowPreviewRejectionCode,
        *,
        stage: str,
        cause_code: str = "",
    ) -> None:
        self.code = code
        self.stage = stage
        self.cause_code = cause_code
        super().__init__(code.value)


def _emit_preparation_progress(
    callback: Callable[[str], None] | None,
    stage: str,
) -> None:
    if callback is not None:
        callback(stage)


def build_llm_requirement_batch_preview(
    source_path: str | PathLike[str],
    *,
    expected_source_sha256: str,
    section_paths: tuple[tuple[str, ...], ...],
    provider: PreflightSemanticProviderPort,
) -> dict[str, Any]:
    """Build a source-bound, read-only LLM batch preview for selected PDF clauses.

    The use case never invokes visual-text recognition and never writes a
    Requirement Set.  Page images are optional semantic evidence rendered only
    for selected native TABLE/IMAGE/FIGURE facts; they are not OCR inputs.
    """

    return _build_llm_requirement_preview(
        source_path,
        expected_source_sha256=expected_source_sha256,
        section_paths=section_paths,
        provider=provider,
        automatic_document=False,
    )


def build_llm_requirement_document_preview(
    source_path: str | PathLike[str],
    *,
    expected_source_sha256: str,
    provider: PreflightSemanticProviderPort,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
    structure_audit_enabled: bool = False,
    max_concurrent_chunks: int = 1,
    progress_callback: Callable[[LlmRequirementDocumentProgress], None] | None = None,
    preparation_progress_callback: Callable[[str], None] | None = None,
    visual_capture_progress_callback: Callable[[int, int, int], None] | None = None,
    partial_result_callback: Callable[[dict[str, Any]], None] | None = None,
    authorized_visual_runtime: AuthorizedPdfVisualRuntime | None = None,
    visual_text_snapshot_cache: VisualTextSnapshotCacheBinding | None = None,
    include_source_read_model: bool = False,
) -> dict[str, Any]:
    """Automatically produce one reviewable requirement draft for a whole PDF."""

    return _build_llm_requirement_preview(
        source_path,
        expected_source_sha256=expected_source_sha256,
        section_paths=(),
        provider=provider,
        automatic_document=True,
        structure_revisions=structure_revisions,
        structure_audit_enabled=structure_audit_enabled,
        max_concurrent_chunks=max_concurrent_chunks,
        progress_callback=progress_callback,
        preparation_progress_callback=preparation_progress_callback,
        visual_capture_progress_callback=visual_capture_progress_callback,
        partial_result_callback=partial_result_callback,
        authorized_visual_runtime=authorized_visual_runtime,
        visual_text_snapshot_cache=visual_text_snapshot_cache,
        include_source_read_model=include_source_read_model,
    )


def _build_llm_requirement_preview(
    source_path: str | PathLike[str],
    *,
    expected_source_sha256: str,
    section_paths: tuple[tuple[str, ...], ...],
    provider: PreflightSemanticProviderPort,
    automatic_document: bool,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
    structure_audit_enabled: bool = False,
    max_concurrent_chunks: int = 1,
    progress_callback: Callable[[LlmRequirementDocumentProgress], None] | None = None,
    preparation_progress_callback: Callable[[str], None] | None = None,
    visual_capture_progress_callback: Callable[[int, int, int], None] | None = None,
    partial_result_callback: Callable[[dict[str, Any]], None] | None = None,
    authorized_visual_runtime: AuthorizedPdfVisualRuntime | None = None,
    visual_text_snapshot_cache: VisualTextSnapshotCacheBinding | None = None,
    include_source_read_model: bool = False,
) -> dict[str, Any]:
    if partial_result_callback is not None and not callable(partial_result_callback):
        raise TypeError("partial_result_callback must be callable")
    if preparation_progress_callback is not None and not callable(
        preparation_progress_callback
    ):
        raise TypeError("preparation_progress_callback must be callable")
    if visual_capture_progress_callback is not None and not callable(
        visual_capture_progress_callback
    ):
        raise TypeError("visual_capture_progress_callback must be callable")
    expected_hash, normalized_paths = (
        _validate_document_request(
            expected_source_sha256=expected_source_sha256,
            provider=provider,
        )
        if automatic_document
        else _validate_request(
            expected_source_sha256=expected_source_sha256,
            section_paths=section_paths,
            provider=provider,
        )
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
    visual_strategy = extraction_visual_strategy_identity(authorized_visual_runtime)
    _emit_preparation_progress(
        preparation_progress_callback,
        "visual_authorization",
    )
    if authorized_visual_runtime is not None and (
        authorized_visual_runtime.route.source_sha256 != expected_hash
        or authorized_visual_runtime.runtime.route.source_sha256 != expected_hash
    ):
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.VISUAL_CAPTURE_REJECTED,
            stage="visual_authorization",
            cause_code="pdf_visual_text_capture_source_mismatch",
        )

    _emit_preparation_progress(preparation_progress_callback, "source_ir")
    started = perf_counter()
    try:
        from reqpilotx_server.application.extraction.source_asset_objects import (
            runtime_source_asset_publisher,
        )

        source_ir_adapter = _source_ir_adapter_with_visual_capture_progress(
            authorized_visual_runtime,
            visual_capture_progress_callback,
            visual_text_snapshot_cache=visual_text_snapshot_cache,
            source_asset_publisher=runtime_source_asset_publisher(),
        )
        source_ir = build_source_ir(
            path,
            preflight_report=report,
            adapters=(source_ir_adapter,),
        )
    except SourceIRBuildRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.SOURCE_IR_REJECTED,
            "source_ir",
            exc,
        ) from exc
    _emit_preparation_progress(
        preparation_progress_callback,
        "visual_validation",
    )
    if authorized_visual_runtime is not None:
        try:
            validate_pdf_visual_text_capture(
                source_ir,
                authorized_visual_runtime.runtime,
            )
        except RuntimeError as exc:
            cause_code = _visual_capture_rejection_cause_code(
                source_ir,
                authorized_visual_runtime,
                str(exc or "pdf_visual_text_capture_invalid"),
            )
            if re.fullmatch(r"[a-z][a-z0-9_]{0,119}", cause_code) is None:
                cause_code = "pdf_visual_text_capture_invalid"
            raise LlmShadowPreviewRejected(
                LlmShadowPreviewRejectionCode.VISUAL_CAPTURE_REJECTED,
                stage="visual_capture_validation",
                cause_code=cause_code,
            ) from exc
    timings["source_ir"] = _elapsed_ms(started)

    _emit_preparation_progress(preparation_progress_callback, "structure_build")
    started = perf_counter()
    try:
        tree = build_structure_tree(source_ir)
    except StructureResolutionRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.STRUCTURE_REJECTED,
            "structure",
            exc,
        ) from exc
    timings["structure"] = _elapsed_ms(started)

    _emit_preparation_progress(preparation_progress_callback, "structure_audit")
    started = perf_counter()
    structure_audit: StructureAuditResult | None = None
    structure_audit_error_code: str | None = None
    if (
        automatic_document
        and structure_audit_enabled
        and isinstance(provider, StructureAuditProviderPort)
    ):
        structure_repair = run_structure_repair_with_provider(
            source_ir,
            tree,
            provider,
            revisions=structure_revisions,
            max_concurrency=max_concurrent_chunks,
        )
        structure_audit = structure_repair.audit
        structure_audit_error_code = structure_repair.audit_error_code
    else:
        structure_audit_error_code = (
            (
                "structure_audit_provider_not_supported"
                if structure_audit_enabled
                else "structure_audit_not_requested"
            )
            if automatic_document
            else None
        )
        structure_repair = initialize_structure_repair_without_provider(
            source_ir,
            tree,
            error_code=(structure_audit_error_code or "structure_audit_not_requested"),
            revisions=structure_revisions,
        )
    timings["structure_audit"] = _elapsed_ms(started)
    tree = structure_repair.effective_structure

    agent_native_document = (
        automatic_document
        and isinstance(provider, DocumentParentBoundaryProviderPort)
    )
    if agent_native_document:
        _emit_preparation_progress(
            preparation_progress_callback,
            "agent_source_planning",
        )
        all_page_numbers = tuple(range(1, report.page_count + 1))
        scoped_visual_cause_code = _scoped_visual_incompleteness_cause_code(
            source_ir,
            all_page_numbers,
            structure_tree=tree,
        )
        if scoped_visual_cause_code:
            raise LlmShadowPreviewRejected(
                LlmShadowPreviewRejectionCode.SCOPED_VISUAL_INCOMPLETE,
                stage="agent_source_gate",
                cause_code=scoped_visual_cause_code,
            )
        _emit_preparation_progress(
            preparation_progress_callback,
            "agent_page_evidence",
        )
        started = perf_counter()
        images = _render_semantic_images(
            path,
            source_sha256=expected_hash,
            page_numbers=all_page_numbers,
        )
        timings["semantic_images"] = _elapsed_ms(started)
        _emit_preparation_progress(
            preparation_progress_callback,
            "agent_document_understanding",
        )
        started = perf_counter()
        try:
            agent_result = extract_agent_native_document_with_provider(
                source_ir,
                provider,
                images=images,
                progress_callback=progress_callback,
            )
        except (SemanticProviderRequestRejected, TypeError, ValueError) as exc:
            raise LlmShadowPreviewRejected(
                LlmShadowPreviewRejectionCode.BATCH_REJECTED,
                stage="agent_native_document",
                cause_code=(
                    getattr(getattr(exc, "code", None), "value", None)
                    or "agent_native_document_invalid"
                ),
            ) from exc
        timings["llm_batch"] = _elapsed_ms(started)
        payload = _build_agent_native_preview_payload(
            source_ir=source_ir,
            tree=tree,
            result=agent_result,
            report=report,
            timings=timings,
            structure_repair=structure_repair,
            visual_strategy=visual_strategy,
        )
        if include_source_read_model:
            from reqpilotx_server.application.extraction.structure_presentation import (
                build_structure_presentation_index,
            )
            from reqpilotx_server.application.extraction.source_page_facts import (
                SOURCE_PAGE_PREVIEW_KEY,
                build_source_page_preview,
            )

            payload["structure_presentation_index"] = (
                build_structure_presentation_index(source_ir, structure=tree)
            )
            page_preview = build_source_page_preview(
                path,
                source_ir=source_ir,
                structure=tree,
            )
            if page_preview is not None:
                payload[SOURCE_PAGE_PREVIEW_KEY] = page_preview
        return payload

    _emit_preparation_progress(preparation_progress_callback, "scope_planning")
    try:
        locator = (
            None
            if automatic_document
            else LlmExtractionScopeLocator(
                source_sha256=expected_hash,
                section_selectors=tuple(
                    LlmSectionScopeSelector(normalized_section_path=section_path)
                    for section_path in normalized_paths
                ),
            )
        )
    except ValueError as exc:
        raise LlmShadowPreviewRejected(
            (
                LlmShadowPreviewRejectionCode.SCOPE_REJECTED
                if automatic_document
                else LlmShadowPreviewRejectionCode.INVALID_REQUEST
            ),
            stage="scope_planning" if automatic_document else "request",
            cause_code=(
                "no_locatable_body_clauses"
                if automatic_document
                else "invalid_section_paths"
            ),
        ) from exc

    started = perf_counter()
    try:
        located_scope = (
            locate_llm_document_scope(source_ir, tree)
            if automatic_document
            else locate_llm_extraction_scope(source_ir, tree, locator)
        )
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

    _emit_preparation_progress(
        preparation_progress_callback,
        "semantic_image_planning",
    )
    semantic_page_numbers = _semantic_image_page_numbers(
        source_ir,
        tree=tree,
        scope_structure_node_ids=located_scope.scope_structure_node_ids,
    )
    _emit_preparation_progress(preparation_progress_callback, "batch_planning")
    started = perf_counter()
    images: tuple[SemanticImageInput, ...] = ()
    if not automatic_document:
        if len(semantic_page_numbers) > MAX_LLM_SHADOW_SEMANTIC_IMAGES:
            raise LlmShadowPreviewRejected(
                LlmShadowPreviewRejectionCode.SEMANTIC_IMAGE_LIMIT_EXCEEDED,
                stage="semantic_images",
            )
        images = _render_semantic_images(
            path,
            source_sha256=expected_hash,
            page_numbers=semantic_page_numbers,
        )
    timings["semantic_images"] = _elapsed_ms(started)

    started = perf_counter()
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
        partial_result_callback(
            _build_partial_preview_payload(
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
        )

    try:
        batch = (
            extract_requirement_document_with_provider(
                source_ir,
                tree,
                provider,
                located_scope,
                image_loader=lambda page_numbers: _render_semantic_images(
                    path,
                    source_sha256=expected_hash,
                    page_numbers=page_numbers,
                ),
                max_images_per_classification_request=(MAX_LLM_SHADOW_SEMANTIC_IMAGES),
                max_concurrent_chunks=max_concurrent_chunks,
                progress_callback=progress_callback,
                checkpoint_callback=(
                    publish_partial_result
                    if partial_result_callback is not None
                    else None
                ),
            )
            if automatic_document
            else extract_requirement_provisions_with_provider(
                source_ir,
                tree,
                provider,
                locator,
                images=images,
                max_images_per_classification_request=(MAX_LLM_SHADOW_SEMANTIC_IMAGES),
            )
        )
    except LlmProvisionBatchRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.BATCH_REJECTED,
            "llm_batch",
            exc,
        ) from exc
    timings["llm_batch"] = _elapsed_ms(started)
    if automatic_document:
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
            structure_audit=structure_audit,
            structure_audit_error_code=structure_audit_error_code,
            structure_repair=structure_repair,
            visual_strategy=visual_strategy,
            max_concurrent_chunks=max_concurrent_chunks,
            collection=(
                "llm_requirement_document"
                if automatic_document
                else "llm_requirement_batch"
            ),
        )
    except ShadowPreviewRejected as exc:
        raise _known_rejection(
            LlmShadowPreviewRejectionCode.PRESENTER_REJECTED,
            "presenter",
            exc,
        ) from exc
    payload["pipeline"]["requirement_view"]["duration_ms"] = _elapsed_ms(started)
    if include_source_read_model:
        from reqpilotx_server.application.extraction.structure_presentation import (
            build_structure_presentation_index,
        )
        from reqpilotx_server.application.extraction.source_page_facts import (
            SOURCE_PAGE_PREVIEW_KEY,
            build_source_page_preview,
        )

        payload["structure_presentation_index"] = build_structure_presentation_index(
            source_ir,
            structure=tree,
        )
        page_preview = build_source_page_preview(
            path,
            source_ir=source_ir,
            structure=tree,
        )
        if page_preview is not None:
            payload[SOURCE_PAGE_PREVIEW_KEY] = page_preview
    return payload


def _validate_request(
    *,
    expected_source_sha256: str,
    section_paths: tuple[tuple[str, ...], ...],
    provider: PreflightSemanticProviderPort,
) -> tuple[str, tuple[tuple[str, ...], ...]]:
    if not isinstance(expected_source_sha256, str) or not _SHA256_PATTERN.fullmatch(
        expected_source_sha256
    ):
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.INVALID_REQUEST,
            stage="request",
            cause_code="invalid_expected_source_sha256",
        )
    if not isinstance(section_paths, tuple) or not section_paths:
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.INVALID_REQUEST,
            stage="request",
            cause_code="empty_section_paths",
        )
    normalized_paths: list[tuple[str, ...]] = []
    for path in section_paths:
        if (
            not isinstance(path, tuple)
            or not path
            or any(not isinstance(item, str) or not item.strip() for item in path)
        ):
            raise LlmShadowPreviewRejected(
                LlmShadowPreviewRejectionCode.INVALID_REQUEST,
                stage="request",
                cause_code="invalid_section_paths",
            )
        normalized_paths.append(tuple(item.strip() for item in path))
    if len(normalized_paths) != len(set(normalized_paths)):
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.INVALID_REQUEST,
            stage="request",
            cause_code="duplicate_section_paths",
        )
    if not isinstance(provider, PreflightSemanticProviderPort):
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.INVALID_REQUEST,
            stage="request",
            cause_code="invalid_semantic_provider",
        )
    return expected_source_sha256, tuple(normalized_paths)


def _validate_document_request(
    *,
    expected_source_sha256: str,
    provider: PreflightSemanticProviderPort,
) -> tuple[str, tuple[tuple[str, ...], ...]]:
    if not isinstance(expected_source_sha256, str) or not _SHA256_PATTERN.fullmatch(
        expected_source_sha256
    ):
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.INVALID_REQUEST,
            stage="request",
            cause_code="invalid_expected_source_sha256",
        )
    if not isinstance(provider, PreflightSemanticProviderPort):
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.INVALID_REQUEST,
            stage="request",
            cause_code="invalid_semantic_provider",
        )
    return expected_source_sha256, ()


def _serialize_provision_outcomes(
    *,
    tree: StructureTreeSnapshot,
    outcomes: tuple[LlmProvisionExtractionOutcome, ...],
    document_view: dict[str, Any],
) -> dict[str, Any]:
    """Serialize terminal outcomes once for final and progressive projections."""

    source_catalog = document_view["source_catalog"]
    all_tables = document_view["tables"]
    tables_by_id = {item["id"]: item for item in all_tables}
    completed_target_ids = tuple(
        target_id
        for outcome in outcomes
        for target_id in outcome.scope_structure_node_ids
    )
    scoped_findings = _scoped_structure_findings(tree, completed_target_ids)
    items: list[dict[str, Any]] = []
    requirement_groups: list[dict[str, Any]] = []
    review_groups: list[dict[str, Any]] = []
    synthetic_findings: list[dict[str, Any]] = []
    for outcome in outcomes:
        scope_nodes = _scope_structure_nodes_with_ancestors(
            tree,
            outcome.scope_structure_node_ids,
        )
        if outcome.extraction is not None:
            (
                local_items,
                local_requirement_groups,
                local_review_groups,
                local_findings,
            ) = _serialize_successful_provision(
                outcome,
                scope_nodes=scope_nodes,
                source_catalog=source_catalog,
                tables_by_id=tables_by_id,
                findings=scoped_findings,
            )
            items.extend(local_items)
            requirement_groups.extend(local_requirement_groups)
            review_groups.extend(local_review_groups)
            synthetic_findings.extend(local_findings)
            continue
        failure_groups, failure_findings = _serialize_failed_provision(
            outcome,
            scope_nodes=scope_nodes,
            source_catalog=source_catalog,
            tables_by_id=tables_by_id,
        )
        review_groups.extend(failure_groups)
        synthetic_findings.extend(failure_findings)

    ordered_items = _ordered_unique(items)
    ordered_requirement_groups = _ordered_unique(requirement_groups)
    ordered_review_groups = _ordered_unique(review_groups)
    finding_payloads = [
        _serialize_finding(finding, source_catalog) for finding in scoped_findings
    ]
    finding_payloads.extend(synthetic_findings)
    ordered_findings = _ordered_unique_findings(finding_payloads)
    referenced_table_ids = {
        table_id
        for collection in (
            ordered_items,
            ordered_requirement_groups,
            ordered_review_groups,
        )
        for item in collection
        for table_id in item.get("table_ids", ())
    }
    semantic_annotations = tuple(
        annotation
        for outcome in outcomes
        if outcome.extraction is not None
        for annotation in outcome.extraction.semantics.annotations
    )
    assembly_items = tuple(
        item
        for outcome in outcomes
        if outcome.extraction is not None
        for item in outcome.extraction.assembly.items
    )
    semantic_terminal_receipts = [
        receipt.model_dump(mode="json")
        for outcome in outcomes
        if outcome.extraction is not None
        for receipt in outcome.extraction.semantic_terminal_receipts
    ]
    return {
        "items": ordered_items,
        "requirement_groups": ordered_requirement_groups,
        "review_groups": ordered_review_groups,
        "findings": ordered_findings,
        "tables": [
            table for table in all_tables if table["id"] in referenced_table_ids
        ],
        "scoped_findings": scoped_findings,
        "semantic_annotations": semantic_annotations,
        "assembly_items": assembly_items,
        "semantic_terminal_receipts": semantic_terminal_receipts,
    }


def _semantic_coverage_summary(
    *,
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    outcomes: tuple[LlmProvisionExtractionOutcome, ...],
) -> dict[str, Any]:
    """Freeze the semantic-unit denominator independently of provider success."""

    total_units = 0
    blocked_unit_count = 0
    terminal_receipts = []
    for outcome in outcomes:
        grounding_scaffold = classify_content(
            source_ir,
            tree,
            scope_structure_node_ids=outcome.scope_structure_node_ids,
        )
        plan = build_semantic_unit_plan(source_ir, tree, grounding_scaffold)
        planned_unit_ids = tuple(item.semantic_unit_id for item in plan.units)
        total_units += len(planned_unit_ids)
        if outcome.extraction is None:
            blocked_unit_count += len(planned_unit_ids)
            continue
        receipts = outcome.extraction.semantic_terminal_receipts
        if tuple(item.semantic_unit_id for item in receipts) != planned_unit_ids:
            raise ValueError(
                "semantic terminal receipts do not match the deterministic unit plan"
            )
        terminal_receipts.extend(item.model_dump(mode="json") for item in receipts)
    if len(terminal_receipts) + blocked_unit_count != total_units:
        raise ValueError("semantic coverage denominator is inconsistent")
    return {
        "total_units": total_units,
        "blocked_unit_count": blocked_unit_count,
        "terminal_receipt_count": len(terminal_receipts),
        "unresolved_unit_count": sum(
            item.get("status") == "UNRESOLVED" for item in terminal_receipts
        ),
        "planning_runtime_version": SEMANTIC_PLANNING_RUNTIME_VERSION,
        "classifier_runtime_version": MODEL_SEMANTIC_CLASSIFIER_VERSION,
        "assembler_runtime_version": MODEL_REQUIREMENT_ASSEMBLER_VERSION,
        "document_runtime_version": LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
    }


def _serialize_structure_agent(
    repair: StructureRepairResult,
    *,
    editable: bool,
    final: bool,
    stale_structure_node_ids: tuple[str, ...] = (),
    reusable_structure_node_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    effective = repair.effective_structure
    stages = list(
        build_agent_stage_states(
            active_stage=(
                DocumentRequirementAgentStage.READY
                if final
                else DocumentRequirementAgentStage.REQUIREMENT_CLASSIFICATION
            ),
            active_status=(
                DocumentRequirementAgentStageStatus.COMPLETE
                if final
                else DocumentRequirementAgentStageStatus.RUNNING
            ),
            stale_structure_node_ids=stale_structure_node_ids,
        )
    )
    repair_stage_status = (
        DocumentRequirementAgentStageStatus.SKIPPED
        if repair.audit_error_code == "structure_audit_not_requested"
        else {
            "COMPLETE": DocumentRequirementAgentStageStatus.COMPLETE,
            "PARTIAL": DocumentRequirementAgentStageStatus.PARTIAL,
            "UNRESOLVED": DocumentRequirementAgentStageStatus.PARTIAL,
        }[repair.status.value]
    )
    stages = [
        item.model_copy(update={"status": repair_stage_status})
        if item.stage is DocumentRequirementAgentStage.STRUCTURE_REPAIR
        else item.model_copy(
            update={"status": DocumentRequirementAgentStageStatus.SKIPPED}
        )
        if item.stage
        in {
            DocumentRequirementAgentStage.OPTIONAL_ATOMIZATION,
            DocumentRequirementAgentStage.OPTIONAL_REFINEMENT,
        }
        else item
        for item in stages
    ]
    current_revision = repair.revisions[-1] if repair.revisions else None
    return {
        "schema_version": "document-requirement-agent/v0",
        "workflow": [item.value for item in DocumentRequirementAgentStage],
        "stages": [item.model_dump(mode="json") for item in stages],
        "editable": editable,
        "read_only_reason": (None if editable else "partial_result_is_read_only"),
        "base_structure_identity": {
            "source_ir_id": effective.source_ir_id,
            "source_sha256": effective.source_sha256,
            "snapshot_id": effective.base_structure_snapshot_id,
            "canonical_sha256": effective.base_structure_canonical_sha256,
            "resolver_id": effective.resolver_id,
            "resolver_version": effective.resolver_version,
        },
        "effective_structure_identity": {
            "snapshot_id": effective.effective_snapshot_id,
            "tree_sha256": effective.effective_tree_sha256,
            "revision_chain_sha256": effective.revision_chain_sha256,
            "revision_number": effective.revision_number,
            "head_revision_id": effective.head_revision_id,
        },
        "current_revision": (
            _serialize_structure_revision_summary(current_revision)
            if current_revision is not None
            else None
        ),
        "proposal_summaries": [
            {
                "finding_id": item.finding_id,
                "provider_run_id": item.provider_run_id,
                "status": item.disposition.value,
                "proposal_id": (
                    item.proposal.proposal_id if item.proposal is not None else None
                ),
                "patch_id": item.patch.patch_id if item.patch is not None else None,
                "revision_id": (
                    item.revision.revision_id if item.revision is not None else None
                ),
                "operations": (
                    [operation.operation for operation in item.proposal.operations]
                    if item.proposal is not None
                    else []
                ),
                "rejection_code": item.rejection_code,
            }
            for item in repair.proposal_outcomes
        ],
        "patch_summaries": [
            {
                "patch_id": item.patch.patch_id,
                "accepted_proposal_id": item.patch.accepted_proposal_id,
                "expected_revision_number": item.patch.expected_revision_number,
                "operations": [
                    operation.operation for operation in item.patch.operations
                ],
            }
            for item in repair.proposal_outcomes
            if item.patch is not None
        ],
        "revision_summaries": [
            _serialize_structure_revision_summary(revision)
            for revision in repair.revisions
        ],
        "revision_chain": [
            revision.model_dump(mode="json") for revision in repair.revisions
        ],
        "effective_nodes": [node.model_dump(mode="json") for node in effective.nodes],
        "effective_relations": [
            relation.model_dump(mode="json") for relation in effective.relations
        ],
        "stale_scope": {
            "structure_node_ids": list(stale_structure_node_ids),
            "reusable_structure_node_ids": list(reusable_structure_node_ids),
            "affected_section_node_ids": list(
                repair.new_impact.affected_section_node_ids
            ),
            "changed_relation_ids": list(repair.new_impact.changed_relation_ids),
        },
        "audit": {
            "status": repair.audit.status.value
            if repair.audit is not None
            else (
                "NOT_RUN"
                if repair.audit_error_code == "structure_audit_not_requested"
                else "UNRESOLVED"
            ),
            "error_code": repair.audit_error_code,
            "finding_count": (
                len(repair.audit.findings) if repair.audit is not None else 0
            ),
        },
    }


def _serialize_structure_revision_summary(revision: Any) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "parent_revision_id": revision.parent_revision_id,
        "action": revision.action.value,
        "patch_id": revision.patch.patch_id if revision.patch is not None else None,
        "undo_target_revision_id": revision.undo_target_revision_id,
        "effective_snapshot_id": revision.effective_snapshot_id,
        "impact": revision.impact.model_dump(mode="json"),
    }


def _build_partial_preview_payload(
    *,
    tree: Any,
    checkpoint: LlmRequirementDocumentCheckpoint,
    completed_by_chunk_index: dict[int, LlmProvisionExtractionOutcome],
    document_view: dict[str, Any],
    report: Any,
    provider: PreflightSemanticProviderPort,
    structure_repair: StructureRepairResult,
    visual_strategy: dict[str, Any],
    visual_observation_count: int,
) -> dict[str, Any]:
    outcomes = tuple(
        sorted(
            completed_by_chunk_index.values(),
            key=lambda item: (item.source_order, item.root_section_structure_node_id),
        )
    )
    serialized = _serialize_provision_outcomes(
        tree=tree,
        outcomes=outcomes,
        document_view=document_view,
    )
    review_counts = Counter(
        item["review_status"] for item in serialized["review_groups"]
    )
    progress = checkpoint.progress
    completed_indexes = sorted(completed_by_chunk_index)
    return {
        "schema_version": "extraction-llm-document-partial/v1",
        "mode": "READ_ONLY_LLM_PARTIAL",
        "status": "PROCESSING",
        "editable": False,
        "edit_reason_code": "llm_document_extraction_running",
        "serving_impact": "NONE",
        "requirement_set_id": None,
        "requirement_set_created": False,
        "requirement_set_activated": False,
        "source_identity_match": True,
        "source_identity": {
            "status": "MATCHED",
            "source_sha256": report.source_sha256,
            "stored_hash_available": True,
            "match": True,
        },
        "visual_strategy": dict(visual_strategy),
        "document": {
            "format": report.identity.detected_format.value,
            "page_count": report.page_count,
            "max_page_number": report.page_count,
        },
        "revision": checkpoint.sequence,
        "coverage": {
            "total_clauses": progress.total_clauses,
            "total_chunks": progress.total_chunks,
            "completed_chunks": progress.completed_chunks,
            "pending_chunks": progress.total_chunks - progress.completed_chunks,
            "completed_chunk_indexes": completed_indexes,
            "total_targets": progress.total_targets,
            "completed_targets": progress.completed_targets,
            "successful_chunks": progress.successful_chunks,
            "unresolved_chunks": progress.unresolved_chunks,
            "blocked_chunks": progress.blocked_chunks,
        },
        "counts": {
            "recognized_requirement_groups": len(serialized["requirement_groups"]),
            "requirement": review_counts.get("REQUIREMENT", 0),
            "needs_classification": review_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_counts.get("INFORMATION", 0),
            "requirement_groups": len(serialized["requirement_groups"]),
            "review_groups": len(serialized["review_groups"]),
            "items": len(serialized["items"]),
            "tables": len(serialized["tables"]),
            "successful_provisions": progress.successful_chunks,
            "unresolved_provisions": progress.unresolved_chunks,
            "blocked_provisions": progress.blocked_chunks,
        },
        "review_counts": {
            "total": len(serialized["review_groups"]),
            "recognized_requirement_groups": len(serialized["requirement_groups"]),
            "requirement": review_counts.get("REQUIREMENT", 0),
            "needs_classification": review_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_counts.get("INFORMATION", 0),
        },
        "tables": serialized["tables"],
        "items": serialized["items"],
        "requirement_groups": serialized["requirement_groups"],
        "review_groups": serialized["review_groups"],
        "findings": serialized["findings"],
        "semantic_terminal_receipts": serialized["semantic_terminal_receipts"],
        "structure_agent": _serialize_structure_agent(
            structure_repair,
            editable=False,
            final=False,
        ),
        "ocr": {
            "mode": visual_strategy["mode"],
            "provider": (
                None
                if visual_strategy["mode"] == "OFF"
                else visual_strategy["provider_name"]
            ),
            "observation_count": visual_observation_count,
            "local_fallback": False,
        },
        "llm_batch": {
            "strategy": "DOCUMENT_AUTO_PROGRESSIVE",
            "provider": {
                "id": provider.provider_id,
                "version": provider.provider_version,
                "model_id": provider.model_id,
            },
            "provisions": [_safe_provision_summary(outcome) for outcome in outcomes],
            "terminal_chunks": [
                {
                    "chunk_index": chunk_index,
                    **_safe_provision_summary(completed_by_chunk_index[chunk_index]),
                }
                for chunk_index in completed_indexes
            ],
        },
    }


def _build_agent_native_preview_payload(
    *,
    source_ir: SourceIRSnapshot,
    tree: Any,
    result: AgentNativeDocumentResult,
    report: Any,
    timings: dict[str, float],
    structure_repair: StructureRepairResult,
    visual_strategy: dict[str, Any],
) -> dict[str, Any]:
    serialized = serialize_agent_native_document_result(result)
    items = list(serialized["items"])
    review_groups = list(serialized["review_groups"])
    requirement_groups = list(serialized["requirement_groups"])
    decomposition = list(serialized["requirement_decomposition"])
    review_counts = Counter(
        str(item.get("review_status") or "NEEDS_CLASSIFICATION")
        for item in review_groups
    )
    top_status = (
        "READY"
        if result.status is AgentNativeDocumentStatus.COMPLETE
        and source_ir.status.value == "COMPLETE"
        else "PARTIAL"
    )
    provider_id, provider_version, model_id = result.provider_binding
    unresolved_parent_count = sum(
        item.get("review_status") == "NEEDS_CLASSIFICATION" for item in review_groups
    )
    parent_run_receipts = [
        {
            "provider_run_id": item.run.provider_run_id,
            "status": item.run.status.value,
            "error_code": item.run.error_code,
            "latency_ms": item.run.latency_ms,
            "request_count": item.run.request_count,
        }
        for item in result.parent_boundary_results
    ]
    decomposition_run_receipts = [
        {
            "provider_run_id": item.run.provider_run_id,
            "status": item.run.status.value,
            "error_code": item.run.error_code,
            "latency_ms": item.run.latency_ms,
            "request_count": item.run.request_count,
        }
        for item in result.decomposition_results
    ]
    return {
        "schema_version": LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
        "requirement_tree_projection_runtime_version": (
            REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
        ),
        "mode": "EDITABLE_AGENT_NATIVE_REVIEW",
        "collection": "agent_native_requirement_document",
        "status": top_status,
        "editable": True,
        "serving_impact": "NONE",
        "source_sha256": source_ir.source_sha256,
        "requirement_set_id": None,
        "requirement_set_created": False,
        "requirement_set_activated": False,
        "source_identity_match": True,
        "source_identity": {
            "status": "MATCHED",
            "source_sha256": source_ir.source_sha256,
            "stored_hash_available": True,
            "match": True,
        },
        "visual_strategy": dict(visual_strategy),
        "document": {
            "format": report.identity.detected_format.value,
            "page_count": report.page_count,
            "max_page_number": report.page_count,
        },
        "presentation": {
            "default_collection": "review_groups",
            "recognized_requirement_collection": "requirement_groups",
            "diagnostic_collection": "review_groups",
            "response_collection": "agent_native_requirement_document",
            "omitted_collections": ["legacy_node_semantics"],
        },
        "pipeline": {
            "preflight": {
                "label": "Preflight",
                "status": "COMPLETE",
                "duration_ms": timings["preflight"],
            },
            "source_ir": {
                "label": "SourceIR evidence tool",
                "status": source_ir.status.value,
                "adapter_id": source_ir.adapter_id,
                "version": source_ir.adapter_version,
                "used_as_agent_boundary": False,
                "duration_ms": timings["source_ir"],
                "counts": {
                    "nodes": len(source_ir.nodes),
                    "assets": len(source_ir.assets),
                    "diagnostics": len(source_ir.diagnostics),
                },
            },
            "structure": {
                "label": "Structure resolver tool",
                "status": "TOOL_READY",
                "version": getattr(tree, "resolver_version", ""),
                "used_as_agent_boundary": False,
                "duration_ms": timings["structure"],
                "counts": {
                    "nodes": len(tree.nodes),
                    "relations": len(tree.relations),
                    "findings": len(tree.findings),
                },
            },
            "semantic": {
                "label": "Agent source-span document understanding",
                "status": result.status.value,
                "version": AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION,
                "legacy_node_lane": "BYPASSED",
                "duration_ms": timings["llm_batch"],
                "counts": {
                    "pages": len(result.page_numbers),
                    "parent_provisions": result.parent_count,
                    "requirement_parents": result.requirement_parent_count,
                    "unresolved_parents": unresolved_parent_count,
                    "atomic_requirements": result.obligation_count,
                },
            },
            "requirement_view": {
                "label": "Agent-native requirement read model",
                "status": (
                    "AGENT_NATIVE_READY"
                    if top_status == "READY"
                    else "AGENT_NATIVE_PARTIAL"
                ),
                "version": AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION,
                "duration_ms": 0.0,
                "counts": {
                    "nodes": len(items),
                    "requirement_groups": len(requirement_groups),
                    "review_groups": len(review_groups),
                },
            },
        },
        "counts": {
            "recognized_requirement_groups": len(requirement_groups),
            "requirement": review_counts.get("REQUIREMENT", 0),
            "needs_classification": review_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_counts.get("INFORMATION", 0),
            "requirement_groups": len(requirement_groups),
            "review_groups": len(review_groups),
            "items": len(items),
            "tables": 0,
            "provisions": result.parent_count,
            "successful_provisions": (
                result.parent_count
                - unresolved_parent_count
                - result.partial_decomposition_parent_count
            ),
            "unresolved_provisions": (
                unresolved_parent_count + result.partial_decomposition_parent_count
            ),
            "blocked_provisions": 0,
            "analyzed_pages": len(result.page_numbers),
            "atomic_requirements": result.obligation_count,
            "legacy_scope_nodes": 0,
        },
        "ocr": {
            "mode": visual_strategy["mode"],
            "provider": (
                None
                if visual_strategy["mode"] == "OFF"
                else visual_strategy["provider_name"]
            ),
            "observation_count": len(source_ir.visual_text_snapshots),
            "local_fallback": False,
        },
        "pagination": _all_results_pagination(items),
        "requirement_group_pagination": _all_results_pagination(requirement_groups),
        "review_group_pagination": _all_results_pagination(review_groups),
        "review_counts": {
            "total": len(review_groups),
            "recognized_requirement_groups": len(requirement_groups),
            "requirement": review_counts.get("REQUIREMENT", 0),
            "needs_classification": review_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_counts.get("INFORMATION", 0),
        },
        "tables": [],
        "items": items,
        "requirement_groups": requirement_groups,
        "review_groups": review_groups,
        "requirement_decomposition": decomposition,
        "baseline": serialized["baseline"],
        "atomization": serialized["atomization"],
        "findings": [],
        "semantic_terminal_receipts": [],
        "semantic_coverage": {
            "schema_version": "agent-source-span-coverage/v1",
            "basis": "PAGE_SOURCE_SEGMENTS",
            "page_count": len(result.page_numbers),
            "parent_provision_count": result.parent_count,
            "requirement_parent_count": result.requirement_parent_count,
            "atomic_requirement_count": result.obligation_count,
            "unresolved_parent_count": unresolved_parent_count,
            "partial_decomposition_parent_count": (
                result.partial_decomposition_parent_count
            ),
            "legacy_structure_exact_partition": False,
        },
        "structure_agent": _serialize_structure_agent(
            structure_repair,
            editable=False,
            final=True,
        ),
        "llm_structure_audit": {
            "status": "TOOL_ONLY",
            "error_code": None,
            "finding_count": 0,
            "used_as_agent_boundary": False,
        },
        "llm_batch": {
            "status": result.status.value,
            "strategy": "AGENT_NATIVE_DOCUMENT",
            "runtime_version": result.runtime_version,
            "provider": {
                "id": provider_id,
                "version": provider_version,
                "model_id": model_id,
            },
            "agent_contract": {
                "authority": "PAGE_AND_SOURCE_SPANS",
                "parser_role": "READ_ONLY_FACT_TOOL",
                "resolver_role": "READ_ONLY_SUGGESTION_TOOL",
                "legacy_scope_used": False,
                "legacy_scope_node_count": 0,
            },
            "page_numbers": list(result.page_numbers),
            "parent_boundary": {
                "status": (
                    "PARTIAL"
                    if any(
                        item.reference.status.value == "PARTIAL"
                        for item in result.parent_boundary_results
                    )
                    else "OBSERVED"
                ),
                "scope_count": len(result.parent_boundary_results),
                "provision_count": result.parent_count,
                "run_receipts": parent_run_receipts,
            },
            "decomposition_parent_count": sum(
                len(request.items) for request in result.decomposition_requests
            ),
            "decomposition_obligation_count": result.obligation_count,
            "decomposition_run_receipts": decomposition_run_receipts,
            "workflow_mode": result.workflow_mode.value,
        },
    }


def _build_preview_payload(
    *,
    source_ir: SourceIRSnapshot,
    tree: Any,
    located_scope: LocatedLlmExtractionScope,
    batch: LlmRequirementBatchResult | LlmRequirementDocumentResult,
    report: Any,
    semantic_page_numbers: tuple[int, ...],
    timings: dict[str, float],
    structure_repair: StructureRepairResult,
    visual_strategy: dict[str, Any],
    structure_audit: StructureAuditResult | None = None,
    structure_audit_error_code: str | None = None,
    max_concurrent_chunks: int = 1,
    collection: str = "llm_requirement_batch",
) -> dict[str, Any]:
    document_view = build_document_view(source_ir, tree)
    serialized = _serialize_provision_outcomes(
        tree=tree,
        outcomes=batch.provisions,
        document_view=document_view,
    )
    items = serialized["items"]
    requirement_groups = serialized["requirement_groups"]
    review_groups = serialized["review_groups"]
    audit_finding_payloads = _serialize_structure_audit_findings(
        structure_audit,
        tree=tree,
        review_groups=review_groups,
        source_catalog=document_view["source_catalog"],
    )
    unmapped_audit_finding_count = sum(
        item.get("review_group_id") is None for item in audit_finding_payloads
    )
    finding_payloads = _ordered_unique_findings(
        [*serialized["findings"], *audit_finding_payloads]
    )
    scoped_tables = serialized["tables"]
    scoped_findings = serialized["scoped_findings"]
    review_relevant_structure_findings = tuple(
        finding
        for finding in scoped_findings
        if finding.blocking or finding.severity is not StructureFindingSeverity.INFO
    )
    review_counts = Counter(item["review_status"] for item in review_groups)
    semantic_annotations = serialized["semantic_annotations"]
    assembly_items = serialized["assembly_items"]
    structure_audit_incomplete = (
        structure_audit is not None and structure_audit.unresolved_window_count > 0
    ) or structure_audit_error_code == "structure_audit_unresolved"
    upstream_review_required = (
        source_ir.status.value != "COMPLETE"
        or bool(review_relevant_structure_findings)
        or bool(audit_finding_payloads)
        or structure_audit_incomplete
    )
    top_status = (
        "READY"
        if batch.status.value == "COMPLETE" and not upstream_review_required
        else "REVIEW_REQUIRED"
    )
    provider_id, provider_version, model_id = batch.provider_binding
    model_prediction = build_llm_requirement_model_prediction(
        source_ir,
        tree,
        located_scope,
        batch,
    )
    reviewed_result = initial_llm_requirement_review(model_prediction)
    review_delta = evaluate_llm_requirement_review(
        model_prediction,
        reviewed_result,
    )
    decomposition = [
        {
            "parent_requirement_view_node_id": item.requirement_view_node_id,
            "request_sha256": item.request_sha256,
            "status": item.result.run.status.value,
            "error_code": item.result.run.error_code,
            "provider_run_id": item.result.run.provider_run_id,
            "obligations": [
                obligation.model_dump(mode="json")
                for obligation in item.result.obligations
            ],
            "context_ranges": [
                context.model_dump(mode="json")
                for context in item.result.context_ranges
            ],
        }
        for outcome in batch.provisions
        if outcome.extraction is not None
        for item in outcome.extraction.atomization_results
    ]

    return {
        "schema_version": LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
        # Freeze the downstream read-model projection together with the
        # semantic result.  Resolver compatibility alone cannot prove that a
        # cached result will produce the same review candidates.
        "requirement_tree_projection_runtime_version": (
            REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
        ),
        "mode": "EDITABLE_LLM_REVIEW",
        "collection": collection,
        "status": top_status,
        "serving_impact": "NONE",
        "requirement_set_id": None,
        "requirement_set_created": False,
        "requirement_set_activated": False,
        "source_identity_match": True,
        "source_identity": {
            "status": "MATCHED",
            "source_sha256": source_ir.source_sha256,
            "stored_hash_available": True,
            "match": True,
        },
        "visual_strategy": dict(visual_strategy),
        "document": {
            "format": report.identity.detected_format.value,
            "page_count": report.page_count,
            "max_page_number": report.page_count,
        },
        "presentation": {
            "default_collection": (
                "requirement_groups"
                if collection == "llm_requirement_document"
                else "review_groups"
            ),
            "recognized_requirement_collection": "requirement_groups",
            "diagnostic_collection": "items",
            "response_collection": collection,
            "omitted_collections": [],
        },
        "pipeline": {
            "preflight": {
                "label": "Preflight",
                "status": "COMPLETE",
                "duration_ms": timings["preflight"],
            },
            "source_ir": {
                "label": "SourceIR",
                "status": source_ir.status.value,
                "adapter_id": source_ir.adapter_id,
                "version": source_ir.adapter_version,
                "duration_ms": timings["source_ir"],
                "counts": {
                    "nodes": len(source_ir.nodes),
                    "assets": len(source_ir.assets),
                    "diagnostics": len(source_ir.diagnostics),
                    "visual_observations": len(source_ir.visual_text_snapshots),
                },
            },
            "structure": {
                "label": "Structure",
                "status": (
                    "REVIEW_REQUIRED"
                    if (
                        review_relevant_structure_findings
                        or audit_finding_payloads
                        or structure_audit_incomplete
                    )
                    else "COMPLETE"
                ),
                "version": tree.resolver_version,
                "duration_ms": (
                    timings["structure"] + timings.get("structure_audit", 0.0)
                ),
                "counts": {
                    "nodes": len(located_scope.scope_structure_node_ids),
                    "relations": sum(
                        relation.source_structure_node_id
                        in set(located_scope.scope_structure_node_ids)
                        and relation.target_structure_node_id
                        in set(located_scope.scope_structure_node_ids)
                        for relation in tree.relations
                    ),
                    "findings": len(scoped_findings) + len(audit_finding_payloads),
                    "llm_audit_findings": len(audit_finding_payloads),
                    "llm_audit_unresolved_windows": (
                        structure_audit.unresolved_window_count
                        if structure_audit is not None
                        else int(structure_audit_incomplete)
                    ),
                },
            },
            "semantic": {
                "label": "Semantic",
                "status": (
                    "COMPLETE"
                    if batch.status.value == "COMPLETE"
                    else "REVIEW_REQUIRED"
                ),
                "version": provider_version,
                "planning_runtime_version": SEMANTIC_PLANNING_RUNTIME_VERSION,
                "classifier_runtime_version": MODEL_SEMANTIC_CLASSIFIER_VERSION,
                "assembler_runtime_version": MODEL_REQUIREMENT_ASSEMBLER_VERSION,
                "document_runtime_version": (LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION),
                "duration_ms": timings["llm_batch"],
                "counts": {
                    "annotations": len(semantic_annotations),
                    "unresolved": sum(
                        item.semantic_class is SemanticClass.UNRESOLVED
                        or item.status.value == "UNRESOLVED"
                        for item in semantic_annotations
                    ),
                    "semantic_page_images": len(semantic_page_numbers),
                },
            },
            "requirement_view": {
                "label": "RequirementView",
                "status": "SHADOW_READY" if top_status == "READY" else top_status,
                "version": LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
                "duration_ms": 0.0,
                "counts": {
                    "nodes": len(items),
                    "requirement_groups": len(requirement_groups),
                    "review_groups": len(review_groups),
                },
            },
        },
        "counts": {
            "recognized_requirement_groups": len(requirement_groups),
            "requirement": review_counts.get("REQUIREMENT", 0),
            "needs_classification": review_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_counts.get("INFORMATION", 0),
            "requirement_groups": len(requirement_groups),
            "review_groups": len(review_groups),
            "items": len(items),
            "tables": len(scoped_tables),
            "provisions": len(batch.provisions),
            "successful_provisions": batch.successful_provision_count,
            "unresolved_provisions": batch.unresolved_provision_count,
            "blocked_provisions": batch.blocked_provision_count,
            "analyzed_clauses": len(located_scope.root_section_structure_node_ids),
            "semantic_chunks": (
                len(batch.chunks)
                if isinstance(batch, LlmRequirementDocumentResult)
                else len(batch.provisions)
            ),
            "semantic_annotations": len(semantic_annotations),
            "assembly_items": len(assembly_items),
            "unresolved_assembly_items": sum(
                item.status is RequirementAssemblyStatus.UNRESOLVED
                for item in assembly_items
            ),
            "structure_audit_findings": len(audit_finding_payloads),
            "structure_audit_unresolved_windows": (
                structure_audit.unresolved_window_count
                if structure_audit is not None
                else 0
            ),
            "structure_audit_rejected_findings": (
                structure_audit.rejected_finding_count
                if structure_audit is not None
                else 0
            ),
            "structure_audit_failed_windows": (
                structure_audit.failed_window_count
                if structure_audit is not None
                else 0
            ),
        },
        "ocr": {
            "mode": visual_strategy["mode"],
            "provider": (
                None
                if visual_strategy["mode"] == "OFF"
                else visual_strategy["provider_name"]
            ),
            "observation_count": len(source_ir.visual_text_snapshots),
            "local_fallback": False,
        },
        "pagination": _all_results_pagination(items),
        "requirement_group_pagination": _all_results_pagination(requirement_groups),
        "review_group_pagination": _all_results_pagination(review_groups),
        "review_counts": {
            "total": len(review_groups),
            "recognized_requirement_groups": len(requirement_groups),
            "requirement": review_counts.get("REQUIREMENT", 0),
            "needs_classification": review_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_counts.get("INFORMATION", 0),
        },
        "tables": scoped_tables,
        "items": items,
        "requirement_groups": requirement_groups,
        "review_groups": review_groups,
        "requirement_decomposition": decomposition,
        "findings": finding_payloads,
        "semantic_terminal_receipts": serialized["semantic_terminal_receipts"],
        "semantic_coverage": _semantic_coverage_summary(
            source_ir=source_ir,
            tree=tree,
            outcomes=batch.provisions,
        ),
        "structure_agent": _serialize_structure_agent(
            structure_repair,
            editable=True,
            final=True,
        ),
        "llm_structure_audit": _structure_audit_summary(
            structure_audit,
            error_code=structure_audit_error_code,
            duration_ms=timings.get("structure_audit", 0.0),
            unmapped_finding_count=unmapped_audit_finding_count,
        ),
        "llm_review": {
            "schema_version": LLM_REQUIREMENT_REVIEW_SESSION_SCHEMA_VERSION,
            "persistence": {
                "status": "UNBOUND",
                "session_id": None,
            },
            "model_prediction": model_prediction.model_dump(mode="json"),
            "reviewed_result": reviewed_result.model_dump(mode="json"),
            "delta": review_delta.model_dump(mode="json"),
        },
        "llm_batch": {
            "status": batch.status.value,
            "strategy": (
                "DOCUMENT_AUTO"
                if isinstance(batch, LlmRequirementDocumentResult)
                else "SELECTED_PROVISIONS"
            ),
            "provider": {
                "id": provider_id,
                "version": provider_version,
                "model_id": model_id,
            },
            "locator": {
                "version": located_scope.locator_version,
                "sha256": located_scope.locator_sha256,
                "root_count": len(located_scope.root_section_structure_node_ids),
                "scope_node_count": len(located_scope.scope_structure_node_ids),
            },
            "semantic_image_page_numbers": list(semantic_page_numbers),
            "parent_boundary": (
                {
                    "status": (
                        "PARTIAL"
                        if any(
                            item.reference.status.value == "PARTIAL"
                            for item in batch.parent_boundary_results
                        )
                        else "OBSERVED"
                    ),
                    "scope_count": len(batch.parent_boundary_results),
                    "provision_count": sum(
                        item.validation_receipt.provision_count
                        for item in batch.parent_boundary_results
                    ),
                    "context_range_count": sum(
                        item.validation_receipt.context_range_count
                        for item in batch.parent_boundary_results
                    ),
                    "run_receipts": [
                        {
                            "provider_run_id": item.run.provider_run_id,
                            "status": item.run.status.value,
                            "error_code": item.run.error_code,
                            "latency_ms": item.run.latency_ms,
                            "request_count": item.run.request_count,
                        }
                        for item in batch.parent_boundary_results
                    ],
                }
                if isinstance(batch, LlmRequirementDocumentResult)
                and batch.parent_boundary_results
                else None
            ),
            "successful_provision_count": batch.successful_provision_count,
            "unresolved_provision_count": batch.unresolved_provision_count,
            "blocked_provision_count": batch.blocked_provision_count,
            "analyzed_clause_count": len(located_scope.root_section_structure_node_ids),
            "semantic_chunk_count": (
                len(batch.chunks)
                if isinstance(batch, LlmRequirementDocumentResult)
                else len(batch.provisions)
            ),
            "max_concurrent_chunks": (
                max_concurrent_chunks
                if isinstance(batch, LlmRequirementDocumentResult)
                else 1
            ),
            "view_node_ref_count": len(batch.requirement_view_node_refs),
            "decomposition_parent_count": len(decomposition),
            "decomposition_obligation_count": sum(
                len(item["obligations"]) for item in decomposition
            ),
            "provisions": [
                _safe_provision_summary(outcome) for outcome in batch.provisions
            ],
        },
    }


def _serialize_successful_provision(
    outcome: LlmProvisionExtractionOutcome,
    *,
    scope_nodes: tuple[StructureNode, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
    findings: tuple[StructureFinding, ...],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    extraction = outcome.extraction
    assert extraction is not None
    structure_nodes = {item.structure_node_id: item for item in scope_nodes}
    annotations = {
        item.semantic_annotation_id: item for item in extraction.semantics.annotations
    }
    view_nodes = {
        item.requirement_view_node_id: item
        for item in extraction.requirement_view.nodes
    }
    depths = _view_depths(view_nodes)
    findings_by_candidate = _findings_by_candidate(
        extraction.requirement_view.nodes,
        findings,
    )
    items = [
        _serialize_candidate(
            node,
            depth=depths[node.requirement_view_node_id],
            source_catalog=source_catalog,
            structure_nodes=structure_nodes,
            annotations=annotations,
            findings=findings_by_candidate[node.requirement_view_node_id],
        )
        for node in extraction.requirement_view.nodes
    ]
    requirement_groups = _serialize_requirement_groups(
        extraction.requirement_view.nodes,
        serialized_items={item["id"]: item for item in items},
        structure_nodes=structure_nodes,
        semantic_annotations=extraction.semantics.annotations,
        source_catalog=source_catalog,
        tables_by_id=tables_by_id,
    )
    _enrich_llm_assessment_points(
        requirement_groups,
        serialized_items={item["id"]: item for item in items},
    )
    review_groups = _serialize_review_groups(
        requirement_groups=requirement_groups,
        structure_nodes=scope_nodes,
        semantic_annotations=extraction.semantics.annotations,
        projected_structure_node_ids={
            item.target_structure_node_id for item in extraction.requirement_view.nodes
        },
        source_catalog=source_catalog,
        tables_by_id=tables_by_id,
    )
    visible_status = (
        "SUCCESS"
        if outcome.status is LlmProvisionExtractionStatus.SUCCESS
        else "PARTIAL"
    )
    for group in (*requirement_groups, *review_groups):
        group["llm_provision_status"] = visible_status
        group["llm_error_code"] = None
    issue_groups, issue_findings = _serialize_unresolved_semantic_units(
        outcome,
        scope_nodes=scope_nodes,
        source_catalog=source_catalog,
        tables_by_id=tables_by_id,
    )
    review_groups.extend(issue_groups)
    if outcome.status is LlmProvisionExtractionStatus.UNRESOLVED and not issue_groups:
        fallback_groups, fallback_findings = _serialize_failed_provision(
            outcome,
            scope_nodes=scope_nodes,
            source_catalog=source_catalog,
            tables_by_id=tables_by_id,
        )
        review_groups.extend(fallback_groups)
        issue_findings.extend(fallback_findings)
    return items, requirement_groups, review_groups, issue_findings


def _serialize_unresolved_semantic_units(
    outcome: LlmProvisionExtractionOutcome,
    *,
    scope_nodes: tuple[StructureNode, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expose only terminally unresolved units beside successful peer results."""

    extraction = outcome.extraction
    assert extraction is not None
    assembly_by_unit = {
        item.semantic_unit_id: item for item in extraction.assembly.items
    }
    unresolved_receipts = tuple(
        receipt
        for receipt in extraction.semantic_terminal_receipts
        if receipt.status.value == "UNRESOLVED"
        or (
            receipt.semantic_unit_id in assembly_by_unit
            and assembly_by_unit[receipt.semantic_unit_id].status
            is RequirementAssemblyStatus.UNRESOLVED
        )
    )
    if not unresolved_receipts:
        return [], []

    structure_nodes = {item.structure_node_id: item for item in scope_nodes}
    templates = _serialize_review_groups(
        requirement_groups=[],
        structure_nodes=scope_nodes,
        semantic_annotations=extraction.semantics.annotations,
        projected_structure_node_ids=set(),
        source_catalog=source_catalog,
        tables_by_id=tables_by_id,
    )
    templates_by_boundary = {
        str(item["boundary_structure_node_id"]): item
        for item in templates
        if item.get("boundary_structure_node_id")
    }
    provision_summary = _safe_provision_summary(outcome)
    groups: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for receipt in unresolved_receipts:
        assembly_item = assembly_by_unit.get(receipt.semantic_unit_id)
        owner = structure_nodes[receipt.owner_structure_node_id]
        boundary = _nearest_explicit_section(owner, structure_nodes=structure_nodes)
        template = templates_by_boundary.get(
            boundary.structure_node_id if boundary is not None else ""
        )
        if template is None:
            root = structure_nodes.get(outcome.root_section_structure_node_id)
            if root is None or root.kind is StructureNodeKind.DOCUMENT:
                raise ShadowPreviewRejected(
                    code=ShadowPreviewRejectionCode.UNRESOLVED_SOURCE_REFERENCE,
                    message="The unresolved semantic unit has no review boundary.",
                    stage="presenter",
                )
            template = _root_branch_review_template(
                root,
                scope_nodes=scope_nodes,
                source_catalog=source_catalog,
                tables_by_id=tables_by_id,
            )
        semantic_failed = receipt.status.value == "UNRESOLVED"
        reason_codes = (
            receipt.reason_codes
            if semantic_failed
            else (
                assembly_item.reason_codes
                if assembly_item is not None
                else ("provider_assembly_unresolved",)
            )
        )
        error_code = reason_codes[0]
        finding_code = (
            "llm_semantic_unit_unresolved"
            if semantic_failed
            else "llm_assembly_unit_unresolved"
        )
        finding_id = (
            "llm-finding-"
            + hashlib.sha256(
                (f"{receipt.terminal_receipt_id}|{finding_code}|{error_code}").encode(
                    "utf-8"
                )
            ).hexdigest()[:32]
        )
        owner_refs = _resolve_refs(owner.source_refs, source_catalog)
        owner_text = _join_ref_text(owner_refs)
        group = dict(template)
        group.update(
            {
                "id": receipt.semantic_unit_id,
                "kind": "SEMANTIC_UNIT_REVIEW",
                "source_order": owner.source_order,
                "boundary_structure_node_id": owner.structure_node_id,
                "outline_label": owner.explicit_label or template["outline_label"],
                "title": (
                    next(
                        (
                            line.strip()
                            for line in owner_text.splitlines()
                            if line.strip()
                        ),
                        "",
                    )
                    or template["title"]
                ),
                "text": owner_text or template["text"],
                "semantic_class": "UNRESOLVED",
                "review_status": "NEEDS_CLASSIFICATION",
                "contains_recognized_requirement": False,
                "assessment_points": [],
                "item_ids": [],
                "finding_codes": [finding_code],
                "finding_ids": [finding_id],
                "semantic_unit_id": receipt.semantic_unit_id,
                "owner_structure_node_id": receipt.owner_structure_node_id,
                "terminal_receipt": receipt.model_dump(mode="json"),
                "reason_codes": list(reason_codes),
                "llm_provision_status": "UNRESOLVED",
                "llm_parent_provision_status": outcome.status.value,
                "llm_error_code": error_code,
                "llm_provision_summary": provision_summary,
            }
        )
        groups.append(group)
        findings.append(
            {
                "id": finding_id,
                "code": finding_code,
                "severity": "error",
                "blocking": True,
                "message_key": finding_code,
                "structure_node_id": owner.structure_node_id,
                "semantic_unit_id": receipt.semantic_unit_id,
                "terminal_receipt_id": receipt.terminal_receipt_id,
                "page_start": group["page_start"],
                "page_end": group["page_end"],
                "source_refs": owner_refs,
                "reason_codes": list(reason_codes),
                "llm_provision_status": "UNRESOLVED",
                "llm_parent_provision_status": outcome.status.value,
                "llm_error_code": error_code,
                "llm_provision_summary": provision_summary,
            }
        )
    return groups, findings


def _nearest_explicit_section(
    node: StructureNode,
    *,
    structure_nodes: dict[str, StructureNode],
) -> StructureNode | None:
    current: StructureNode | None = node
    visited: set[str] = set()
    while current is not None:
        if (
            current.kind is StructureNodeKind.SECTION
            and current.explicit_label is not None
        ):
            return current
        parent_id = current.parent_structure_node_id
        if parent_id is None or parent_id in visited:
            return None
        visited.add(parent_id)
        current = structure_nodes.get(parent_id)
    return None


def _root_branch_review_template(
    root: StructureNode,
    *,
    scope_nodes: tuple[StructureNode, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if root.kind is StructureNodeKind.DOCUMENT:
        raise ValueError("document root cannot become a branch review boundary")
    nodes_by_id = {node.structure_node_id: node for node in scope_nodes}
    if root.structure_node_id not in nodes_by_id:
        raise ValueError("branch review root is outside its scope")
    context_refs = _resolve_structure_subtree_refs(
        root.structure_node_id,
        structure_nodes=nodes_by_id,
        source_catalog=source_catalog,
    )
    context_blocks = _context_blocks(
        context_refs,
        tables_by_id=tables_by_id,
    )
    own_text = _join_ref_text(
        _resolve_structure_node_refs(
            root,
            source_catalog=source_catalog,
        )
    )
    pages = _pages_from_context_blocks(context_blocks)
    return {
        "id": root.structure_node_id,
        "kind": "BRANCH_REVIEW",
        "source_order": root.source_order,
        "boundary_structure_node_id": root.structure_node_id,
        "outline_label": root.explicit_label,
        "title": _display_label(root, own_text),
        "text": own_text,
        "semantic_class": "UNRESOLVED",
        "review_status": "NEEDS_CLASSIFICATION",
        "contains_recognized_requirement": False,
        "ancestor_path": _structure_ancestor_path(
            root,
            structure_nodes=nodes_by_id,
            source_catalog=source_catalog,
        ),
        "context_text": _join_context_block_text(context_blocks),
        "context_blocks": context_blocks,
        "table_ids": [
            block["table_id"] for block in context_blocks if block["kind"] == "table"
        ],
        "page_start": pages[0] if pages else None,
        "page_end": pages[-1] if pages else None,
        "assessment_points": [],
        "item_ids": [],
        "finding_codes": [],
        "finding_ids": [],
    }


def _enrich_llm_assessment_points(
    groups: list[dict[str, Any]],
    *,
    serialized_items: dict[str, dict[str, Any]],
) -> None:
    """Keep the model's parent/child decision hierarchy visible to reviewers."""

    for group in groups:
        for point in group.get("assessment_points", ()):
            item = serialized_items.get(str(point.get("id") or ""))
            if item is None:
                raise ValueError("assessment point has no serialized view node")
            point.update(
                {
                    "parent_id": item["parent_id"],
                    "role": item["role"],
                    "decision_mode": item["decision_mode"],
                    "semantic_class": item["semantic_class"],
                    "requirement_subtype": item["requirement_subtype"],
                    "source_refs": item["source_refs"],
                }
            )


def _serialize_failed_provision(
    outcome: LlmProvisionExtractionOutcome,
    *,
    scope_nodes: tuple[StructureNode, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups = _serialize_review_groups(
        requirement_groups=[],
        structure_nodes=scope_nodes,
        semantic_annotations=(),
        projected_structure_node_ids=set(),
        source_catalog=source_catalog,
        tables_by_id=tables_by_id,
    )
    scoped_section_ids = {
        node.structure_node_id
        for node in scope_nodes
        if node.kind.value == "section"
        and node.structure_node_id in set(outcome.scope_structure_node_ids)
    }
    root_groups = [
        group
        for group in groups
        if group["boundary_structure_node_id"] in scoped_section_ids
    ]
    if not root_groups:
        root = next(
            (
                node
                for node in scope_nodes
                if node.structure_node_id == outcome.root_section_structure_node_id
            ),
            None,
        )
        if (
            root is None
            or root.kind is StructureNodeKind.DOCUMENT
            or root.structure_node_id not in set(outcome.scope_structure_node_ids)
        ):
            raise ShadowPreviewRejected(
                code=ShadowPreviewRejectionCode.UNRESOLVED_SOURCE_REFERENCE,
                message="The semantic chunk could not be serialized for review.",
                stage="presenter",
            )
        root_groups = [
            _root_branch_review_template(
                root,
                scope_nodes=scope_nodes,
                source_catalog=source_catalog,
                tables_by_id=tables_by_id,
            )
        ]
    finding_code = (
        "llm_provision_blocked_input"
        if outcome.status is LlmProvisionExtractionStatus.BLOCKED_INPUT
        else "llm_provision_unresolved"
    )
    provision_summary = _safe_provision_summary(outcome)
    findings: list[dict[str, Any]] = []
    for group in root_groups:
        boundary_id = str(group["boundary_structure_node_id"])
        finding_id = (
            "llm-finding-"
            + hashlib.sha256(
                (
                    f"{boundary_id}|{outcome.status.value}|{outcome.error_code or ''}"
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
        group.update(
            {
                "semantic_class": "UNRESOLVED",
                "review_status": "NEEDS_CLASSIFICATION",
                "contains_recognized_requirement": False,
                "assessment_points": [],
                "item_ids": [],
                "finding_codes": [finding_code],
                "finding_ids": [finding_id],
                "llm_provision_status": outcome.status.value,
                "llm_error_code": outcome.error_code,
                "llm_provision_summary": provision_summary,
            }
        )
        findings.append(
            {
                "id": finding_id,
                "code": finding_code,
                "severity": "error",
                "blocking": True,
                "message_key": finding_code,
                "structure_node_id": boundary_id,
                "page_start": group["page_start"],
                "page_end": group["page_end"],
                "source_refs": [],
                "llm_provision_status": outcome.status.value,
                "llm_error_code": outcome.error_code,
                "llm_provision_summary": provision_summary,
            }
        )
    return root_groups, findings


def _safe_provision_summary(
    outcome: LlmProvisionExtractionOutcome,
) -> dict[str, Any]:
    extraction = outcome.extraction
    return {
        "root_section_structure_node_id": outcome.root_section_structure_node_id,
        "source_order": outcome.source_order,
        "scope_node_count": len(outcome.scope_structure_node_ids),
        "status": outcome.status.value,
        "error_code": outcome.error_code,
        "completed_provider_run_count": (
            len(extraction.provider_runs)
            if extraction is not None
            else len(outcome.completed_provider_runs)
        ),
        "semantic_annotation_count": (
            len(extraction.semantics.annotations) if extraction is not None else 0
        ),
        "unresolved_semantic_count": (
            sum(
                item.semantic_class is SemanticClass.UNRESOLVED
                or item.status.value == "UNRESOLVED"
                for item in extraction.semantics.annotations
            )
            if extraction is not None
            else 0
        ),
        "assembly_item_count": (
            len(extraction.assembly.items) if extraction is not None else 0
        ),
        "unresolved_assembly_count": (
            sum(
                item.status is RequirementAssemblyStatus.UNRESOLVED
                for item in extraction.assembly.items
            )
            if extraction is not None
            else 0
        ),
        "requirement_view_node_count": (
            len(extraction.requirement_view.nodes) if extraction is not None else 0
        ),
        "semantic_terminal_receipts": (
            [
                receipt.model_dump(mode="json")
                for receipt in extraction.semantic_terminal_receipts
            ]
            if extraction is not None
            else []
        ),
    }


def _scope_structure_nodes_with_ancestors(
    tree: StructureTreeSnapshot,
    scope_structure_node_ids: tuple[str, ...],
) -> tuple[StructureNode, ...]:
    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    included = set(scope_structure_node_ids)
    for target_id in scope_structure_node_ids:
        parent_id = nodes_by_id[target_id].parent_structure_node_id
        while parent_id is not None:
            included.add(parent_id)
            parent_id = nodes_by_id[parent_id].parent_structure_node_id
    return tuple(item for item in tree.nodes if item.structure_node_id in included)


def _scoped_structure_findings(
    tree: StructureTreeSnapshot,
    scope_structure_node_ids: tuple[str, ...],
) -> tuple[StructureFinding, ...]:
    scoped_ids = set(scope_structure_node_ids)
    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    ancestor_ids: set[str] = set()
    source_refs = {
        source_ref
        for target_id in scope_structure_node_ids
        for source_ref in nodes_by_id[target_id].source_refs
    }
    for target_id in scope_structure_node_ids:
        parent_id = nodes_by_id[target_id].parent_structure_node_id
        while parent_id is not None:
            ancestor_ids.add(parent_id)
            parent_id = nodes_by_id[parent_id].parent_structure_node_id
    return tuple(
        finding
        for finding in tree.findings
        if finding.structure_node_id in scoped_ids | ancestor_ids
        or any(source_ref in source_refs for source_ref in finding.source_refs)
    )


def _scope_source_page_numbers(
    source_ir: SourceIRSnapshot,
    *,
    tree: StructureTreeSnapshot,
    scope_structure_node_ids: tuple[str, ...],
) -> tuple[int, ...]:
    refs = _scoped_source_refs(tree, scope_structure_node_ids)
    source_pages = {
        item.source_node_id: item.native_locator.page_number for item in source_ir.nodes
    }
    cell_pages = {
        cell.cell_id: cell.native_locator.page_number
        for node in source_ir.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
    }
    visual_pages = {
        observation.observation_id: observation.page_number
        for snapshot in source_ir.visual_text_snapshots
        for observation in snapshot.observations
    }
    pages = {
        page
        for source_ref in refs
        for page in (
            source_pages.get(source_ref.source_id)
            if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
            else cell_pages.get(source_ref.source_id)
            if source_ref.kind is StructureSourceRefKind.TABLE_CELL
            else visual_pages.get(source_ref.source_id),
        )
        if page is not None
    }
    return tuple(sorted(pages))


def _semantic_image_page_numbers(
    source_ir: SourceIRSnapshot,
    *,
    tree: StructureTreeSnapshot,
    scope_structure_node_ids: tuple[str, ...],
) -> tuple[int, ...]:
    refs = _scoped_source_refs(tree, scope_structure_node_ids)
    source_node_ids = {
        item.source_id
        for item in refs
        if item.kind is StructureSourceRefKind.SOURCE_NODE
    }
    table_cell_ids = {
        item.source_id
        for item in refs
        if item.kind is StructureSourceRefKind.TABLE_CELL
    }
    pages = {
        node.native_locator.page_number
        for node in source_ir.nodes
        if node.source_node_id in source_node_ids
        and node.kind
        in {SourceNodeKind.TABLE, SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        and node.native_locator.page_number is not None
    }
    pages.update(
        cell.native_locator.page_number
        for node in source_ir.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
        if cell.cell_id in table_cell_ids
        and cell.native_locator.page_number is not None
    )
    return tuple(sorted(pages))


def _scoped_source_refs(
    tree: StructureTreeSnapshot,
    scope_structure_node_ids: tuple[str, ...],
) -> set[Any]:
    scoped_ids = set(scope_structure_node_ids)
    return {
        source_ref
        for node in tree.nodes
        if node.structure_node_id in scoped_ids
        for source_ref in node.source_refs
    }


def _has_scoped_visual_incompleteness(
    source_ir: SourceIRSnapshot,
    scope_page_numbers: tuple[int, ...],
) -> bool:
    return bool(
        _scoped_visual_incompleteness_cause_code(
            source_ir,
            scope_page_numbers,
        )
    )


def _source_ir_adapter_with_visual_capture_progress(
    authorized_visual_runtime: AuthorizedPdfVisualRuntime | None,
    progress_callback: Callable[[int, int, int], None] | None,
    *,
    visual_text_snapshot_cache: VisualTextSnapshotCacheBinding | None = None,
    source_asset_publisher=None,
) -> PdfSourceIRAdapter:
    if authorized_visual_runtime is None:
        return PdfSourceIRAdapter(
            source_asset_publisher=source_asset_publisher,
        )
    runtime = authorized_visual_runtime.runtime
    provider = runtime.visual_text_provider
    if provider is None:
        return runtime.pdf_adapter
    progress_provider: _VisualCaptureProgressProvider | None = None
    wrapped_provider: VisualTextProviderPort = provider
    if progress_callback is not None:
        progress_provider = _VisualCaptureProgressProvider(
            provider,
            authorized_upper_bound=len(runtime.route.page_numbers),
            callback=progress_callback,
        )
        wrapped_provider = progress_provider
    if visual_text_snapshot_cache is not None:
        wrapped_provider = visual_text_snapshot_cache.wrap(
            wrapped_provider,
            cache_hit_callback=(
                progress_provider.report_completed_page
                if progress_provider is not None
                else None
            ),
        )
    if wrapped_provider is provider and source_asset_publisher is None:
        return runtime.pdf_adapter
    adapter = PdfSourceIRAdapter(
        visual_text_provider=wrapped_provider,
        visual_text_page_numbers=runtime.visual_text_page_numbers,
        force_visual_text_page_numbers=getattr(
            runtime.pdf_adapter,
            "_force_visual_text_page_numbers",
            (),
        ),
        visual_table_provider=runtime.visual_table_provider,
        visual_table_page_numbers=runtime.visual_table_page_numbers,
        source_asset_publisher=source_asset_publisher,
    )
    if adapter.adapter_version != runtime.pdf_adapter.adapter_version:
        raise RuntimeError("visual_capture_progress_adapter_identity_mismatch")
    return adapter


def _scoped_visual_incompleteness_cause_code(
    source_ir: SourceIRSnapshot,
    scope_page_numbers: tuple[int, ...],
    *,
    structure_tree: StructureTreeSnapshot | None = None,
) -> str:
    scoped_pages = set(scope_page_numbers)
    for diagnostic in source_ir.diagnostics:
        if (
            diagnostic.code not in _SCOPED_VISUAL_INCOMPLETE_CODES
            or diagnostic.native_locator is None
            or diagnostic.native_locator.page_number not in scoped_pages
        ):
            continue
        return diagnostic.code
    if structure_tree is not None:
        nonempty_visual_text_pages = {
            snapshot.page_number
            for snapshot in source_ir.visual_text_snapshots
            if "TEXT" in snapshot.evaluated_content_types
            and any(
                observation.content_type == "TEXT" and observation.raw_text.strip()
                for observation in snapshot.observations
            )
        }
        unresolved_text_pages = (
            visual_text_authority_pages(source_ir) - nonempty_visual_text_pages
        )
        source_page_by_id: dict[str, int | None] = {}
        for node in source_ir.nodes:
            source_page_by_id[node.source_node_id] = node.native_locator.page_number
            if node.table_payload is None:
                continue
            for cell in node.table_payload.cells:
                source_page_by_id[cell.cell_id] = (
                    cell.native_locator.page_number or node.native_locator.page_number
                )
        for finding in structure_tree.findings:
            if not finding.blocking:
                continue
            if finding.code == "pdf_ocr_authority_text_unresolved":
                if unresolved_text_pages & scoped_pages:
                    return finding.code
                continue
            if finding.code == "pdf_ocr_authority_table_text_unresolved" and any(
                source_page_by_id.get(ref.source_id) in scoped_pages
                for ref in finding.source_refs
            ):
                return finding.code
    return ""


def _visual_capture_rejection_cause_code(
    source_ir: SourceIRSnapshot,
    authorized_visual_runtime: AuthorizedPdfVisualRuntime,
    validator_cause_code: str,
) -> str:
    if validator_cause_code != "pdf_visual_text_capture_page_scope_mismatch":
        return validator_cause_code
    selected_pages = tuple(
        sorted(authorized_visual_runtime.runtime.visual_text_page_numbers)
    )
    return (
        _scoped_visual_incompleteness_cause_code(source_ir, selected_pages)
        or validator_cause_code
    )


def _render_semantic_images(
    path: Path,
    *,
    source_sha256: str,
    page_numbers: tuple[int, ...],
) -> tuple[SemanticImageInput, ...]:
    if not page_numbers:
        return ()
    if pymupdf is None:
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.SEMANTIC_IMAGE_RENDER_REJECTED,
            stage="semantic_images",
            cause_code="pymupdf_unavailable",
        )
    source_bytes = path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
        raise LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
            stage="semantic_images",
        )
    images: list[SemanticImageInput] = []
    with pymupdf.open(stream=source_bytes, filetype="pdf") as document:
        if page_numbers[-1] > document.page_count:
            raise LlmShadowPreviewRejected(
                LlmShadowPreviewRejectionCode.SEMANTIC_IMAGE_RENDER_REJECTED,
                stage="semantic_images",
                cause_code="page_number_out_of_range",
            )
        pages = tuple(
            document.load_page(page_number - 1) for page_number in page_numbers
        )
        scale = _semantic_render_scale(pages)
        for page_number, page in zip(page_numbers, pages, strict=True):
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale),
                alpha=False,
            )
            image_bytes = pixmap.tobytes("png")
            images.append(
                SemanticImageInput(
                    source_sha256=source_sha256,
                    page_number=page_number,
                    width_px=pixmap.width,
                    height_px=pixmap.height,
                    image_bytes=image_bytes,
                    image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                )
            )
    return tuple(images)


def _semantic_render_scale(pages: tuple[Any, ...]) -> float:
    total_page_points = sum(
        max(float(page.rect.width), 1.0) * max(float(page.rect.height), 1.0)
        for page in pages
    )
    if total_page_points <= 0.0:
        return _MAX_SEMANTIC_IMAGE_SCALE
    budget_scale = math.sqrt(_SEMANTIC_IMAGE_PIXEL_BUDGET / total_page_points)
    return min(_MAX_SEMANTIC_IMAGE_SCALE, budget_scale)


def _ordered_unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item["id"])
        if item_id in by_id:
            raise ValueError("duplicate presenter item identity")
        by_id[item_id] = item
    return sorted(
        by_id.values(),
        key=lambda item: (item.get("source_order", 0), item["id"]),
    )


def _ordered_unique_findings(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item["id"])
        if item_id in by_id:
            raise ValueError("duplicate presenter finding identity")
        by_id[item_id] = item
    return sorted(
        by_id.values(),
        key=lambda item: (
            item.get("page_start") or 0,
            item.get("structure_node_id") or "",
            item["id"],
        ),
    )


def _serialize_structure_audit_findings(
    audit: StructureAuditResult | None,
    *,
    tree: StructureTreeSnapshot,
    review_groups: list[dict[str, Any]],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    if audit is None:
        return []
    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    groups_by_boundary = {
        str(group.get("boundary_structure_node_id") or ""): group
        for group in review_groups
        if str(group.get("boundary_structure_node_id") or "")
    }
    for group in review_groups:
        group.setdefault("structure_audit_findings", [])
    payloads: list[dict[str, Any]] = []
    for finding in audit.findings:
        code = f"llm_structure_audit_{finding.category.value}"
        resolved_refs = _resolve_refs(finding.evidence_source_refs, source_catalog)
        pages = sorted(
            {
                int(location["page_number"])
                for source_ref in resolved_refs
                if isinstance((location := source_ref.get("location")), dict)
                and location.get("page_number") is not None
            }
        )
        group_mapping = _review_group_mapping_for_structure_node(
            finding.target_structure_node_id,
            nodes_by_id=nodes_by_id,
            groups_by_boundary=groups_by_boundary,
            mapping_prefix="TARGET",
        )
        if group_mapping is None:
            for related_id in finding.related_structure_node_ids:
                group_mapping = _review_group_mapping_for_structure_node(
                    related_id,
                    nodes_by_id=nodes_by_id,
                    groups_by_boundary=groups_by_boundary,
                    mapping_prefix="RELATED",
                )
                if group_mapping is not None:
                    break
        if group_mapping is None:
            fallback_group = _nearest_review_group_by_source_order(
                finding.target_structure_node_id,
                nodes_by_id=nodes_by_id,
                review_groups=review_groups,
            )
            if fallback_group is not None:
                group_mapping = (
                    fallback_group,
                    "NEAREST_SOURCE_ORDER_NAVIGATION_ONLY",
                )
        group = group_mapping[0] if group_mapping is not None else None
        mapping_basis = group_mapping[1] if group_mapping is not None else "UNMAPPED"
        related_review_groups = _related_review_group_metadata(
            finding.related_structure_node_ids,
            nodes_by_id=nodes_by_id,
            groups_by_boundary=groups_by_boundary,
        )
        payload = {
            "id": finding.finding_id,
            "code": code,
            "severity": "warning",
            "blocking": False,
            "message_key": code,
            "origin": "LLM_STRUCTURE_AUDIT",
            "structure_audit_status": "SUSPECT",
            "structure_node_id": finding.target_structure_node_id,
            "related_structure_node_ids": list(finding.related_structure_node_ids),
            "page_start": pages[0] if pages else None,
            "page_end": pages[-1] if pages else None,
            "source_refs": resolved_refs,
            "reason_codes": list(finding.reason_codes),
            "provider_run_id": finding.provider_run_id,
            "review_group_id": group.get("id") if group is not None else None,
            "mapping_basis": mapping_basis,
            "related_review_groups": related_review_groups,
        }
        payloads.append(payload)
        if group is None:
            continue
        group["finding_codes"] = list(
            dict.fromkeys([*(group.get("finding_codes") or ()), code])
        )
        group["finding_ids"] = list(
            dict.fromkeys([*(group.get("finding_ids") or ()), finding.finding_id])
        )
        group["structure_audit_findings"].append(
            {
                "id": finding.finding_id,
                "code": code,
                "structure_audit_status": "SUSPECT",
                "structure_node_id": finding.target_structure_node_id,
                "related_structure_node_ids": list(finding.related_structure_node_ids),
                "page_start": pages[0] if pages else None,
                "page_end": pages[-1] if pages else None,
                "source_refs": resolved_refs,
                "reason_codes": list(finding.reason_codes),
                "provider_run_id": finding.provider_run_id,
                "review_group_id": group["id"],
                "mapping_basis": mapping_basis,
                "related_review_groups": related_review_groups,
            }
        )
    return payloads


def _review_group_mapping_for_structure_node(
    structure_node_id: str,
    *,
    nodes_by_id: dict[str, StructureNode],
    groups_by_boundary: dict[str, dict[str, Any]],
    mapping_prefix: str,
) -> tuple[dict[str, Any], str] | None:
    current_id: str | None = structure_node_id
    visited: set[str] = set()
    depth = 0
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        group = groups_by_boundary.get(current_id)
        if group is not None:
            suffix = "BOUNDARY" if depth == 0 else "ANCESTOR"
            return group, f"{mapping_prefix}_{suffix}"
        node = nodes_by_id.get(current_id)
        current_id = node.parent_structure_node_id if node is not None else None
        depth += 1
    return None


def _nearest_review_group_by_source_order(
    structure_node_id: str,
    *,
    nodes_by_id: dict[str, StructureNode],
    review_groups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    node = nodes_by_id.get(structure_node_id)
    if node is None:
        return None
    candidates = [
        group
        for group in review_groups
        if isinstance(group.get("source_order"), int)
        and not isinstance(group.get("source_order"), bool)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda group: (
            abs(int(group["source_order"]) - node.source_order),
            int(group["source_order"]) > node.source_order,
            int(group["source_order"]),
            str(group.get("id") or ""),
        ),
    )


def _related_review_group_metadata(
    related_structure_node_ids: tuple[str, ...],
    *,
    nodes_by_id: dict[str, StructureNode],
    groups_by_boundary: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    seen_group_ids: set[str] = set()
    for related_id in related_structure_node_ids:
        mapping = _review_group_mapping_for_structure_node(
            related_id,
            nodes_by_id=nodes_by_id,
            groups_by_boundary=groups_by_boundary,
            mapping_prefix="RELATED",
        )
        if mapping is None:
            continue
        group, mapping_basis = mapping
        group_id = str(group.get("id") or "")
        if not group_id or group_id in seen_group_ids:
            continue
        seen_group_ids.add(group_id)
        metadata.append(
            {
                "review_group_id": group_id,
                "boundary_structure_node_id": group.get("boundary_structure_node_id"),
                "outline_label": group.get("outline_label"),
                "title": group.get("title"),
                "page_start": group.get("page_start"),
                "page_end": group.get("page_end"),
                "mapping_basis": mapping_basis,
            }
        )
    return metadata


def _structure_audit_summary(
    audit: StructureAuditResult | None,
    *,
    error_code: str | None,
    duration_ms: float,
    unmapped_finding_count: int = 0,
) -> dict[str, Any]:
    if audit is None:
        return {
            "status": (
                "NOT_RUN"
                if error_code
                in {
                    "structure_audit_not_requested",
                    "structure_audit_provider_not_supported",
                }
                else "UNRESOLVED"
            ),
            "error_code": error_code,
            "duration_ms": duration_ms,
            "finding_count": 0,
            "unmapped_finding_count": 0,
            "rejected_finding_count": 0,
            "failed_window_count": 0,
            "interpretation": "NO_STRUCTURE_CORRECTNESS_CLAIM",
            "run_receipts": [],
        }
    first_run = audit.provider_runs[0] if audit.provider_runs else None
    return {
        "status": audit.status.value,
        "error_code": error_code,
        "duration_ms": duration_ms,
        "finding_count": len(audit.findings),
        "unmapped_finding_count": unmapped_finding_count,
        "rejected_finding_count": audit.rejected_finding_count,
        "failed_window_count": audit.failed_window_count,
        "interpretation": (
            "SUSPICIONS_REQUIRE_REVIEW"
            if audit.findings
            else "NO_HIGH_CONFIDENCE_SUSPICION_IN_BOUNDED_AUDIT"
        ),
        "provider": (
            {
                "id": first_run.provider_id,
                "version": first_run.provider_version,
                "model_id": first_run.model_id,
                "prompt_version": first_run.prompt_version,
            }
            if first_run is not None
            else None
        ),
        "resolved_window_count": audit.resolved_window_count,
        "unresolved_window_count": audit.unresolved_window_count,
        "run_receipts": [
            {
                "provider_run_id": run.provider_run_id,
                "status": run.status.value,
                "error_code": run.error_code,
                "latency_ms": run.latency_ms,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
            }
            for run in audit.provider_runs
        ],
    }


def _all_results_pagination(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "offset": 0,
        "limit": len(items),
        "returned": len(items),
        "total": len(items),
        "has_more": False,
    }


def _known_rejection(
    code: LlmShadowPreviewRejectionCode,
    stage: str,
    exc: Exception,
) -> LlmShadowPreviewRejected:
    cause = getattr(exc, "code", "")
    cause_code = cause.value if isinstance(cause, Enum) else str(cause or "")
    return LlmShadowPreviewRejected(code, stage=stage, cause_code=cause_code)


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1_000, 1)


__all__ = [
    "LLM_SHADOW_PREVIEW_SCHEMA_VERSION",
    "LlmShadowPreviewRejected",
    "LlmShadowPreviewRejectionCode",
    "MAX_LLM_SHADOW_SEMANTIC_IMAGES",
    "build_llm_requirement_batch_preview",
    "build_llm_requirement_document_preview",
]
