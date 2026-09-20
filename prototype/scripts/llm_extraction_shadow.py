#!/usr/bin/env python3
"""Run the review-only PDF -> qwen semantic extraction pipeline.

The report intentionally contains no source text, OCR text, page images,
provider prompts or raw provider responses.  It cannot create or bind a
Requirement Set.
"""

from __future__ import annotations

# ruff: noqa: E402

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:
    import pymupdf
except ImportError:  # pragma: no cover - guarded by runtime error path
    pymupdf = None

from app.adapters.documents.bailian_semantic_provider import (
    DEFAULT_BAILIAN_EVALUATION_MODEL,
    DEFAULT_BAILIAN_SEMANTIC_MODEL,
    DEFAULT_BAILIAN_SEMANTIC_SEED,
    BailianQwenSemanticProvider,
)
from app.adapters.documents.bailian_visual_text_provider import (
    DEFAULT_BAILIAN_OCR_MODEL,
    BailianVisualTextProvider,
)
from app.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.classify_content import classify_content
from app.application.extraction.llm_semantics import (
    DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST,
    extract_requirements_with_provider,
)
from app.application.extraction.evaluate_llm_repeatability import (
    LlmRequirementRepeatabilityStatus,
    run_llm_requirement_batch_repeatability,
)
from app.application.extraction.locate_llm_scope import (
    LlmExtractionScopeLocator,
    LlmScopeLocationRejectionCode,
    LlmSectionScopeSelector,
    LocatedLlmExtractionScope,
    locate_llm_extraction_scope,
)
from app.application.extraction.project_requirements import project_requirements
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.preflight import DocumentPreflightReport
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.source_ir import SourceNodeKind
from app.domain.extraction.structure import StructureSourceRefKind
from app.ports.semantic_provider import SemanticImageInput
from app.services.ai_copilot import load_ai_copilot_config


REPORT_SCHEMA_VERSION = "llm-extraction-shadow-report/v2"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MAX_SEMANTIC_PAGE_IMAGES = 12
DEFAULT_SEMANTIC_TIMEOUT_SECONDS = 90.0
DEFAULT_SEMANTIC_MAX_RETRIES = 1
DEFAULT_OCR_TIMEOUT_SECONDS = 45.0
DEFAULT_OCR_MAX_RETRIES = 0
MAX_EXPLICIT_OCR_PAGES = 12
MAX_OCR_ATTEMPT_BUDGET_SECONDS = 300.0
_VISUAL_FAILURE_CODES = {
    "pdf_visual_text_unavailable",
    "pdf_visual_text_invalid_input",
    "pdf_visual_text_source_hash_mismatch",
    "pdf_visual_text_page_image_hash_mismatch",
    "pdf_visual_text_provider_output_invalid",
    "pdf_visual_text_provider_failed",
}
_SCOPED_VISUAL_INCOMPLETE_CODES = {
    "pdf_visual_text_page_out_of_scope",
    "pdf_visual_text_provider_not_configured",
}
_SAFE_EXCEPTION_REASON_CODES = frozenset(
    {
        "bailian_not_configured",
        "conflicting_ocr_scope_options",
        "duplicate_ocr_page",
        "invalid_ocr_page_scope",
        "invalid_ocr_runtime_limits",
        "llm_scope_locator_incomplete",
        "llm_scope_locator_invalid",
        "llm_scope_source_hash_mismatch",
        "repeatability_run_count_invalid",
        "repeatability_scope_required",
        "ocr_attempt_budget_exceeded",
        "ocr_page_out_of_range",
        "ocr_pages_required",
        "pdf_page_count_unavailable",
        "pymupdf_unavailable",
        "scoped_ocr_pages_required",
        "scoped_visual_text_incomplete",
        "semantic_page_image_scope_exceeded",
        "semantic_page_number_out_of_range",
        "semantic_provision_image_budget_exceeded",
        "invalid_semantic_runtime_limits",
        "invalid_semantic_seed",
        "visual_text_provider_runtime_failure",
    }
).union(item.value for item in LlmScopeLocationRejectionCode)
_SAFE_EXCEPTION_MESSAGE_CODES = {
    (
        "one semantic provision exceeds the page-image request budget"
    ): "semantic_provision_image_budget_exceeded",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run source-grounded qwen3.7-plus PDF extraction in shadow mode."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--model",
        default=None,
        help="Semantic model override. Default: qwen3.7-plus.",
    )
    parser.add_argument(
        "--evaluation-model",
        action="store_true",
        help="Pin evaluation to qwen3.7-plus-2026-05-26.",
    )
    parser.add_argument(
        "--ocr-model",
        default=None,
        help="Scan-page OCR model override. Default: qwen3.5-ocr.",
    )
    parser.add_argument(
        "--disable-ocr",
        action="store_true",
        help="Disable remote scan-page OCR for born-digital diagnostics only.",
    )
    parser.add_argument(
        "--ocr-page",
        action="append",
        type=int,
        default=[],
        help=(
            "One-based page allowed to use remote OCR. Repeat for multiple pages. "
            "Every run with OCR enabled requires this explicit allowlist."
        ),
    )
    parser.add_argument(
        "--ocr-timeout-seconds",
        type=float,
        default=None,
        help="Per-attempt remote OCR timeout. Default: 45 seconds.",
    )
    parser.add_argument(
        "--ocr-max-retries",
        type=int,
        default=None,
        help="Remote OCR retries after the first attempt. Default: 0.",
    )
    parser.add_argument(
        "--max-semantic-page-images",
        type=int,
        default=DEFAULT_MAX_SEMANTIC_PAGE_IMAGES,
    )
    parser.add_argument(
        "--max-semantic-images-per-request",
        type=int,
        default=DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST,
        help="Maximum source-bound page images in one semantic model call. Default: 2.",
    )
    parser.add_argument(
        "--semantic-timeout-seconds",
        type=float,
        default=None,
        help="Per-attempt semantic model timeout. Default: 90 seconds.",
    )
    parser.add_argument(
        "--semantic-max-retries",
        type=int,
        default=None,
        help="Semantic retries after the first attempt. Default: 1.",
    )
    parser.add_argument(
        "--semantic-seed",
        type=int,
        default=DEFAULT_BAILIAN_SEMANTIC_SEED,
        help="Fixed Qwen sampling seed. Default: 1234.",
    )
    parser.add_argument(
        "--repeatability-runs",
        type=int,
        default=1,
        help=(
            "Run an isolated text-free repeatability gate. Use 3-10; "
            "default 1 keeps the normal shadow report."
        ),
    )
    parser.add_argument(
        "--scope-source-sha256",
        default=None,
        help="Expected source SHA-256 for a replayable section scope.",
    )
    parser.add_argument(
        "--section-path",
        action="append",
        default=[],
        help=(
            "Fully qualified numeric exact-provision path, for example "
            "4/4.12/4.12.1. Repeat for multiple provisions."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = args.source.resolve()
    destination = args.out.resolve()
    if source.suffix.casefold() != ".pdf":
        _safe_error("source_not_pdf")
        return 2
    if destination == source:
        _safe_error("report_would_overwrite_source")
        return 2
    if args.max_semantic_page_images < 0 or args.max_semantic_page_images > 50:
        _safe_error("invalid_semantic_page_image_limit")
        return 2
    if args.max_semantic_images_per_request < 1 or (
        args.max_semantic_page_images > 0
        and args.max_semantic_images_per_request > args.max_semantic_page_images
    ):
        _safe_error("invalid_semantic_request_image_limit")
        return 2
    if args.evaluation_model and args.model:
        _safe_error("conflicting_semantic_model_options")
        return 2
    validation_stage = "input_validation"
    try:
        scope_locator = _scope_locator_from_args(args)
        ocr_page_numbers = _ocr_page_numbers_from_args(
            args,
            scoped=scope_locator is not None,
        )
        if args.disable_ocr:
            ocr_timeout_seconds = DEFAULT_OCR_TIMEOUT_SECONDS
            ocr_max_retries = DEFAULT_OCR_MAX_RETRIES
        else:
            ocr_timeout_seconds, ocr_max_retries = _ocr_runtime_from_args(args)
            _validate_ocr_attempt_budget(
                ocr_page_numbers,
                timeout_seconds=ocr_timeout_seconds,
                max_retries=ocr_max_retries,
            )
        semantic_timeout_seconds, semantic_max_retries = _semantic_runtime_from_args(
            args
        )
        semantic_seed = _semantic_seed_from_args(args)
        repeatability_runs = _repeatability_runs_from_args(args)
        if repeatability_runs > 1 and scope_locator is None:
            raise RuntimeError("repeatability_scope_required")
        preflight_report = None
        if scope_locator is not None:
            preflight_report = _preflight_source(source)
            if preflight_report.identity.source_sha256 != scope_locator.source_sha256:
                raise RuntimeError("llm_scope_source_hash_mismatch")
        _validate_ocr_page_range(source, ocr_page_numbers)
    except Exception as exc:
        _safe_error(_safe_exception_code(exc), stage=validation_stage)
        return 2

    runtime_stage = "configuration"
    try:
        settings = _load_bailian_settings()
        if not settings["api_key"]:
            raise RuntimeError("bailian_not_configured")
        ocr_provider = None
        if not args.disable_ocr:
            ocr_provider = BailianVisualTextProvider(
                api_key=settings["api_key"],
                base_url=settings["base_url"],
                model=(
                    str(args.ocr_model or "").strip()
                    or _runtime_setting("REQPILOT_ALIYUN_VISUAL_TEXT_MODEL")
                    or DEFAULT_BAILIAN_OCR_MODEL
                ),
                timeout_seconds=ocr_timeout_seconds,
                max_retries=ocr_max_retries,
            )
        runtime_stage = "source_ir"
        source_ir = build_source_ir(
            source,
            preflight_report=preflight_report,
            adapters=(
                PdfSourceIRAdapter(
                    visual_text_provider=ocr_provider,
                    visual_text_page_numbers=ocr_page_numbers,
                ),
            ),
        )
        _raise_for_visual_provider_runtime_failure(source_ir)
        runtime_stage = "structure"
        tree = build_structure_tree(source_ir)
        runtime_stage = "scope_location"
        located_scope = (
            locate_llm_extraction_scope(source_ir, tree, scope_locator)
            if scope_locator is not None
            else None
        )
        scope_structure_node_ids = (
            located_scope.scope_structure_node_ids
            if located_scope is not None
            else None
        )
        if scope_structure_node_ids is not None:
            runtime_stage = "scoped_visual_gate"
            scope_source_page_numbers = _scope_source_page_numbers(
                source_ir,
                tree=tree,
                scope_structure_node_ids=scope_structure_node_ids,
            )
            _raise_for_scoped_visual_incompleteness(
                source_ir,
                scope_source_page_numbers,
            )
        runtime_stage = "deterministic_baseline"
        deterministic_semantics = classify_content(
            source_ir,
            tree,
            scope_structure_node_ids=scope_structure_node_ids,
        )
        deterministic_view = project_requirements(
            source_ir,
            tree,
            deterministic_semantics,
        )
        runtime_stage = "semantic_images"
        page_numbers = _semantic_page_numbers(
            source_ir,
            tree=tree,
            scope_structure_node_ids=scope_structure_node_ids,
        )
        if len(page_numbers) > args.max_semantic_page_images:
            raise RuntimeError("semantic_page_image_scope_exceeded")
        images = _render_page_images(source, page_numbers)
        runtime_stage = "semantic_provider"
        semantic_model_id = (
            DEFAULT_BAILIAN_EVALUATION_MODEL
            if args.evaluation_model
            else str(args.model or "").strip()
            or _runtime_setting("REQPILOT_EXTRACTION_QWEN_MODEL")
            or DEFAULT_BAILIAN_SEMANTIC_MODEL
        )

        def semantic_provider_factory() -> BailianQwenSemanticProvider:
            return BailianQwenSemanticProvider(
                api_key=settings["api_key"],
                base_url=settings["base_url"],
                model=semantic_model_id,
                seed=semantic_seed,
                timeout_seconds=semantic_timeout_seconds,
                max_retries=semantic_max_retries,
            )

        if repeatability_runs > 1:
            assert scope_locator is not None
            runtime_stage = "repeatability_gate"
            execution = run_llm_requirement_batch_repeatability(
                source_ir=source_ir,
                structure_tree=tree,
                provider_factory=semantic_provider_factory,
                scope_locator=scope_locator,
                sampling_seed=semantic_seed,
                repetition_count=repeatability_runs,
                images=images,
                max_images_per_classification_request=(
                    args.max_semantic_images_per_request
                ),
            )
            repeatability_payload = execution.report.model_dump(mode="json")
            _write_report_atomic(destination, repeatability_payload)
            print(
                json.dumps(
                    {
                        "status": execution.report.status.value,
                        "semantic_model": execution.report.model_id,
                        "semantic_seed": execution.report.sampling_seed,
                        "repetition_count": execution.report.repetition_count,
                        "all_runs_complete": execution.report.all_runs_complete,
                        "provider_request_exact_match": (
                            execution.report.provider_request_exact_match
                        ),
                        "classification_request_exact_match": (
                            execution.report.classification_request_exact_match
                        ),
                        "assembly_request_exact_match": (
                            execution.report.assembly_request_exact_match
                        ),
                        "semantic_exact_match": (execution.report.semantic_exact_match),
                        "assembly_exact_match": (execution.report.assembly_exact_match),
                        "requirement_view_exact_match": (
                            execution.report.requirement_view_exact_match
                        ),
                        "decision_skeleton_exact_match": (
                            execution.report.decision_skeleton_exact_match
                        ),
                        "decision_review_required": (
                            execution.report.decision_review_required
                        ),
                        "non_decision_variance_only": (
                            execution.report.non_decision_variance_only
                        ),
                        "mismatch_codes": [
                            item.value for item in execution.report.mismatch_codes
                        ],
                        "unstable_semantic_decision_target_count": len(
                            execution.report.unstable_semantic_decision_target_structure_node_ids
                        ),
                        "unstable_semantic_grounding_target_count": len(
                            execution.report.unstable_semantic_grounding_target_structure_node_ids
                        ),
                        "unstable_requirement_view_target_count": len(
                            execution.report.unstable_requirement_view_target_structure_node_ids
                        ),
                        "unstable_decision_target_count": len(
                            execution.report.unstable_decision_target_structure_node_ids
                        ),
                        "report": str(destination),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return (
                0
                if execution.report.status is LlmRequirementRepeatabilityStatus.PASS
                else 4
            )

        semantic_provider = semantic_provider_factory()
        runtime_stage = "semantic_extraction"
        result = extract_requirements_with_provider(
            source_ir,
            tree,
            semantic_provider,
            scope_structure_node_ids=scope_structure_node_ids,
            images=images,
            max_images_per_classification_request=(
                args.max_semantic_images_per_request
            ),
        )
        runtime_stage = "report"
        payload = _safe_report(
            source_ir=source_ir,
            tree=tree,
            result=result,
            semantic_page_numbers=page_numbers,
            ocr_provider=ocr_provider,
            deterministic_semantics=deterministic_semantics,
            deterministic_view=deterministic_view,
            located_scope=located_scope,
            ocr_page_numbers=ocr_page_numbers,
            ocr_timeout_seconds=ocr_timeout_seconds,
            ocr_max_retries=ocr_max_retries,
            semantic_timeout_seconds=semantic_timeout_seconds,
            semantic_max_retries=semantic_max_retries,
            semantic_seed=semantic_seed,
        )
        _write_report_atomic(destination, payload)
    except Exception as exc:
        _safe_error(_safe_exception_code(exc), stage=runtime_stage)
        return 2

    unresolved_semantic_count = sum(
        item.semantic_class is SemanticClass.UNRESOLVED
        for item in result.semantics.annotations
    )
    unresolved_assembly_count = sum(
        item.status is RequirementAssemblyStatus.UNRESOLVED
        for item in result.assembly.items
    )
    provider_success = all(
        item.status is SemanticProviderRunStatus.SUCCESS
        for item in result.provider_runs
    )
    status = (
        "PASS"
        if provider_success
        and unresolved_semantic_count == 0
        and unresolved_assembly_count == 0
        else "PARTIAL"
    )
    print(
        json.dumps(
            {
                "status": status,
                "semantic_model": semantic_provider.model_id,
                "ocr_model": ocr_provider.provider_version if ocr_provider else None,
                "ocr_allowed_page_count": (
                    len(ocr_page_numbers) if ocr_page_numbers is not None else None
                ),
                "semantic_annotation_count": len(result.semantics.annotations),
                "scope_node_count": len(result.semantics.scope_structure_node_ids),
                "unresolved_semantic_count": unresolved_semantic_count,
                "unresolved_assembly_count": unresolved_assembly_count,
                "requirement_view_node_count": len(result.requirement_view.nodes),
                "report": str(destination),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if status == "PASS" else 4


def _load_bailian_settings() -> dict[str, str]:
    config = load_ai_copilot_config("aliyun")
    api_key = (
        _runtime_setting(
            "REQPILOT_EXTRACTION_DASHSCOPE_API_KEY",
            "REQPILOT_DASHSCOPE_API_KEY",
            "DASHSCOPE_API_KEY",
        )
        or str(config.dashscope_api_key or "").strip()
    )
    base_url = (
        _runtime_setting(
            "REQPILOT_EXTRACTION_DASHSCOPE_BASE_URL",
            "REQPILOT_DASHSCOPE_BASE_URL",
        )
        or str(config.dashscope_base_url or DEFAULT_DASHSCOPE_BASE_URL).strip()
    )
    return {"api_key": api_key, "base_url": base_url}


def _runtime_setting(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return ""


def _scope_locator_from_args(args: object) -> LlmExtractionScopeLocator | None:
    source_sha256 = str(getattr(args, "scope_source_sha256", None) or "").strip()
    raw_paths = tuple(getattr(args, "section_path", ()) or ())
    if not source_sha256 and not raw_paths:
        return None
    if not source_sha256 or not raw_paths:
        raise RuntimeError("llm_scope_locator_incomplete")
    try:
        selectors = tuple(
            LlmSectionScopeSelector(
                normalized_section_path=_section_path_from_arg(raw_path)
            )
            for raw_path in raw_paths
        )
        return LlmExtractionScopeLocator(
            source_sha256=source_sha256,
            section_selectors=selectors,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("llm_scope_locator_invalid") from exc


def _section_path_from_arg(raw_path: object) -> tuple[str, ...]:
    value = str(raw_path)
    parts = value.split("/")
    if not value or any(not part or part != part.strip() for part in parts):
        raise ValueError("section path contains an empty or padded segment")
    return tuple(parts)


def _ocr_page_numbers_from_args(
    args: object,
    *,
    scoped: bool,
) -> frozenset[int] | None:
    raw_pages = tuple(getattr(args, "ocr_page", ()) or ())
    disable_ocr = bool(getattr(args, "disable_ocr", False))
    has_other_ocr_options = (
        bool(raw_pages)
        or getattr(args, "ocr_model", None) is not None
        or getattr(args, "ocr_timeout_seconds", None) is not None
        or getattr(args, "ocr_max_retries", None) is not None
    )
    if disable_ocr and has_other_ocr_options:
        raise RuntimeError("conflicting_ocr_scope_options")
    if disable_ocr:
        return None
    if not raw_pages:
        raise RuntimeError(
            "scoped_ocr_pages_required" if scoped else "ocr_pages_required"
        )
    if len(raw_pages) > MAX_EXPLICIT_OCR_PAGES or any(
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
        for page_number in raw_pages
    ):
        raise RuntimeError("invalid_ocr_page_scope")
    normalized = frozenset(raw_pages)
    if len(normalized) != len(raw_pages):
        raise RuntimeError("duplicate_ocr_page")
    return normalized


def _ocr_runtime_from_args(args: object) -> tuple[float, int]:
    raw_timeout = getattr(args, "ocr_timeout_seconds", None)
    if raw_timeout is None:
        raw_timeout = (
            _runtime_setting("REQPILOT_EXTRACTION_OCR_TIMEOUT_SECONDS")
            or DEFAULT_OCR_TIMEOUT_SECONDS
        )
    raw_retries = getattr(args, "ocr_max_retries", None)
    if raw_retries is None:
        raw_retries = (
            _runtime_setting("REQPILOT_EXTRACTION_OCR_MAX_RETRIES")
            or DEFAULT_OCR_MAX_RETRIES
        )
    try:
        timeout_seconds = float(raw_timeout)
        max_retries = int(raw_retries)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid_ocr_runtime_limits") from exc
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0.0
        or timeout_seconds > 120.0
        or isinstance(raw_retries, bool)
        or max_retries < 0
        or max_retries > 2
        or str(raw_retries).strip() != str(max_retries)
    ):
        raise RuntimeError("invalid_ocr_runtime_limits")
    return timeout_seconds, max_retries


def _semantic_runtime_from_args(args: object) -> tuple[float, int]:
    raw_timeout = getattr(args, "semantic_timeout_seconds", None)
    if raw_timeout is None:
        raw_timeout = (
            _runtime_setting("REQPILOT_EXTRACTION_QWEN_TIMEOUT_SECONDS")
            or DEFAULT_SEMANTIC_TIMEOUT_SECONDS
        )
    raw_retries = getattr(args, "semantic_max_retries", None)
    if raw_retries is None:
        raw_retries = (
            _runtime_setting("REQPILOT_EXTRACTION_QWEN_MAX_RETRIES")
            or DEFAULT_SEMANTIC_MAX_RETRIES
        )
    try:
        timeout_seconds = float(raw_timeout)
        max_retries = int(raw_retries)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid_semantic_runtime_limits") from exc
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0.0
        or timeout_seconds > 180.0
        or isinstance(raw_retries, bool)
        or max_retries < 0
        or max_retries > 2
        or str(raw_retries).strip() != str(max_retries)
    ):
        raise RuntimeError("invalid_semantic_runtime_limits")
    return timeout_seconds, max_retries


def _semantic_seed_from_args(args: object) -> int:
    seed = getattr(args, "semantic_seed", DEFAULT_BAILIAN_SEMANTIC_SEED)
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not 0 <= seed <= 2**31 - 1
    ):
        raise RuntimeError("invalid_semantic_seed")
    return seed


def _repeatability_runs_from_args(args: object) -> int:
    runs = getattr(args, "repeatability_runs", 1)
    if (
        isinstance(runs, bool)
        or not isinstance(runs, int)
        or (runs != 1 and not 3 <= runs <= 10)
    ):
        raise RuntimeError("repeatability_run_count_invalid")
    return runs


def _validate_ocr_attempt_budget(
    page_numbers: frozenset[int] | None,
    *,
    timeout_seconds: float,
    max_retries: int,
) -> None:
    if page_numbers is None:
        return
    attempt_budget = len(page_numbers) * timeout_seconds * (max_retries + 1)
    if attempt_budget > MAX_OCR_ATTEMPT_BUDGET_SECONDS:
        raise RuntimeError("ocr_attempt_budget_exceeded")


def _validate_ocr_page_range(
    source: Path,
    page_numbers: frozenset[int] | None,
) -> None:
    if page_numbers is None:
        return
    if pymupdf is None:
        raise RuntimeError("pymupdf_unavailable")
    try:
        source_bytes = source.read_bytes()
        with pymupdf.open(stream=source_bytes, filetype="pdf") as document:
            page_count = document.page_count
    except Exception as exc:
        raise RuntimeError("pdf_page_count_unavailable") from exc
    if max(page_numbers) > page_count:
        raise RuntimeError("ocr_page_out_of_range")


def _preflight_source(source: Path) -> DocumentPreflightReport:
    return LocalDocumentPreflight().inspect(source)


def _semantic_page_numbers(
    source_ir: object,
    *,
    tree: object | None = None,
    scope_structure_node_ids: tuple[str, ...] | None = None,
) -> tuple[int, ...]:
    if scope_structure_node_ids is None:
        pages = {
            snapshot.page_number
            for snapshot in getattr(source_ir, "visual_text_snapshots", ())
        }
        pages.update(
            node.native_locator.page_number
            for node in getattr(source_ir, "nodes", ())
            if node.kind
            in {SourceNodeKind.TABLE, SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            and node.native_locator.page_number is not None
        )
        return tuple(sorted(pages))
    if tree is None:
        raise ValueError("scoped semantic pages require a StructureTree")
    scoped_ids = set(scope_structure_node_ids)
    scoped_refs = {
        source_ref
        for node in getattr(tree, "nodes", ())
        if node.structure_node_id in scoped_ids
        for source_ref in node.source_refs
    }
    source_node_ids = {
        item.source_id
        for item in scoped_refs
        if item.kind is StructureSourceRefKind.SOURCE_NODE
    }
    source_cell_ids = {
        item.source_id
        for item in scoped_refs
        if item.kind is StructureSourceRefKind.TABLE_CELL
    }
    visual_observation_ids = {
        item.source_id
        for item in scoped_refs
        if item.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
    }
    pages = {
        observation.page_number
        for snapshot in getattr(source_ir, "visual_text_snapshots", ())
        for observation in snapshot.observations
        if observation.observation_id in visual_observation_ids
    }
    pages.update(
        node.native_locator.page_number
        for node in getattr(source_ir, "nodes", ())
        if node.source_node_id in source_node_ids
        and node.kind
        in {SourceNodeKind.TABLE, SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        and node.native_locator.page_number is not None
    )
    pages.update(
        cell.native_locator.page_number
        for node in getattr(source_ir, "nodes", ())
        if node.table_payload is not None
        for cell in node.table_payload.cells
        if cell.cell_id in source_cell_ids
        and cell.native_locator.page_number is not None
    )
    return tuple(sorted(pages))


def _scope_source_page_numbers(
    source_ir: object,
    *,
    tree: object,
    scope_structure_node_ids: tuple[str, ...],
) -> tuple[int, ...]:
    scoped_ids = set(scope_structure_node_ids)
    scoped_refs = {
        source_ref
        for node in getattr(tree, "nodes", ())
        if node.structure_node_id in scoped_ids
        for source_ref in node.source_refs
    }
    source_node_pages = {
        node.source_node_id: node.native_locator.page_number
        for node in getattr(source_ir, "nodes", ())
        if node.native_locator.page_number is not None
    }
    table_cell_pages = {
        cell.cell_id: cell.native_locator.page_number
        for node in getattr(source_ir, "nodes", ())
        if node.table_payload is not None
        for cell in node.table_payload.cells
        if cell.native_locator.page_number is not None
    }
    visual_pages = {
        observation.observation_id: observation.page_number
        for snapshot in getattr(source_ir, "visual_text_snapshots", ())
        for observation in snapshot.observations
    }
    pages = {
        page_number
        for source_ref in scoped_refs
        for page_number in (
            (
                source_node_pages.get(source_ref.source_id)
                if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
                else table_cell_pages.get(source_ref.source_id)
                if source_ref.kind is StructureSourceRefKind.TABLE_CELL
                else visual_pages.get(source_ref.source_id)
            ),
        )
        if page_number is not None
    }
    return tuple(sorted(pages))


def _render_page_images(
    source: Path,
    page_numbers: tuple[int, ...],
) -> tuple[SemanticImageInput, ...]:
    if not page_numbers:
        return ()
    if pymupdf is None:
        raise RuntimeError("pymupdf_unavailable")
    source_bytes = source.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    images: list[SemanticImageInput] = []
    with pymupdf.open(stream=source_bytes, filetype="pdf") as document:
        for page_number in page_numbers:
            if page_number > document.page_count:
                raise RuntimeError("semantic_page_number_out_of_range")
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
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


def _raise_for_visual_provider_runtime_failure(source_ir: object) -> None:
    if any(
        getattr(item, "code", None) in _VISUAL_FAILURE_CODES
        for item in getattr(source_ir, "diagnostics", ())
    ):
        raise RuntimeError("visual_text_provider_runtime_failure")


def _raise_for_scoped_visual_incompleteness(
    source_ir: object,
    scope_page_numbers: tuple[int, ...],
) -> None:
    scoped_pages = set(scope_page_numbers)
    if any(
        getattr(item, "code", None) in _SCOPED_VISUAL_INCOMPLETE_CODES
        and getattr(getattr(item, "native_locator", None), "page_number", None)
        in scoped_pages
        for item in getattr(source_ir, "diagnostics", ())
    ):
        raise RuntimeError("scoped_visual_text_incomplete")


def _safe_report(
    *,
    source_ir: object,
    tree: object,
    result: object,
    semantic_page_numbers: tuple[int, ...],
    ocr_provider: object | None,
    deterministic_semantics: object,
    deterministic_view: object,
    located_scope: LocatedLlmExtractionScope | None,
    ocr_page_numbers: frozenset[int] | None,
    ocr_timeout_seconds: float,
    ocr_max_retries: int,
    semantic_timeout_seconds: float,
    semantic_max_retries: int,
    semantic_seed: int,
) -> dict[str, object]:
    diagnostic_counts = Counter(
        str(item.code) for item in getattr(source_ir, "diagnostics", ())
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": {
            "source_sha256": source_ir.source_sha256,
            "source_ir_id": source_ir.source_ir_id,
            "adapter_id": source_ir.adapter_id,
            "adapter_version": source_ir.adapter_version,
            "status": source_ir.status.value,
            "node_count": len(source_ir.nodes),
            "asset_count": len(source_ir.assets),
            "visual_text_snapshot_count": len(source_ir.visual_text_snapshots),
            "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        },
        "structure": {
            "structure_snapshot_id": tree.structure_snapshot_id,
            "resolver_id": tree.resolver_id,
            "resolver_version": tree.resolver_version,
            "node_count": len(tree.nodes),
            "relation_count": len(tree.relations),
            "finding_count": len(tree.findings),
        },
        "semantic_page_numbers": list(semantic_page_numbers),
        "scope_locator": (
            {
                "locator_version": located_scope.locator_version,
                "locator_sha256": located_scope.locator_sha256,
                "root_section_structure_node_ids": list(
                    located_scope.root_section_structure_node_ids
                ),
                "scope_structure_node_ids": list(
                    located_scope.scope_structure_node_ids
                ),
            }
            if located_scope is not None
            else None
        ),
        "ocr_provider": (
            {
                "provider_id": ocr_provider.provider_id,
                "provider_version": ocr_provider.provider_version,
                "allowed_page_numbers": (
                    sorted(ocr_page_numbers) if ocr_page_numbers is not None else None
                ),
                "actual_snapshot_page_numbers": sorted(
                    snapshot.page_number for snapshot in source_ir.visual_text_snapshots
                ),
                "timeout_seconds": ocr_timeout_seconds,
                "max_retries": ocr_max_retries,
            }
            if ocr_provider is not None
            else None
        ),
        "provider_runs": [
            item.model_dump(mode="json") for item in result.provider_runs
        ],
        "semantic_runtime": {
            "seed": semantic_seed,
            "timeout_seconds": semantic_timeout_seconds,
            "max_retries": semantic_max_retries,
        },
        "semantics": result.semantics.model_dump(mode="json"),
        "assembly": result.assembly.model_dump(mode="json"),
        "requirement_view": result.requirement_view.model_dump(mode="json"),
        "deterministic_baseline_comparison": _baseline_comparison(
            deterministic_semantics,
            deterministic_view,
            result.semantics,
            result.requirement_view,
        ),
        "serving": {
            "projection_policy": result.requirement_view.projection_policy.value,
            "requirement_set_id": None,
            "serving_activation_gate": False,
        },
    }


def _baseline_comparison(
    deterministic_semantics: object,
    deterministic_view: object,
    model_semantics: object,
    model_view: object,
) -> dict[str, object]:
    baseline_annotations = {
        item.target_structure_node_id: item
        for item in deterministic_semantics.annotations
    }
    model_annotations = {
        item.target_structure_node_id: item for item in model_semantics.annotations
    }
    semantic_differences = []
    for target_id in model_semantics.scope_structure_node_ids:
        baseline = baseline_annotations[target_id]
        model = model_annotations[target_id]
        baseline_subtype = (
            baseline.requirement_subtype.value
            if baseline.requirement_subtype is not None
            else None
        )
        model_subtype = (
            model.requirement_subtype.value
            if model.requirement_subtype is not None
            else None
        )
        if (
            baseline.semantic_class is not model.semantic_class
            or baseline_subtype != model_subtype
        ):
            semantic_differences.append(
                {
                    "target_structure_node_id": target_id,
                    "deterministic_class": baseline.semantic_class.value,
                    "model_class": model.semantic_class.value,
                    "deterministic_subtype": baseline_subtype,
                    "model_subtype": model_subtype,
                }
            )
    return {
        "fallback_applied": False,
        "deterministic_classifier_id": deterministic_semantics.classifier_id,
        "deterministic_classifier_version": deterministic_semantics.classifier_version,
        "semantic_difference_count": len(semantic_differences),
        "semantic_differences": semantic_differences,
        "deterministic_view_node_count": len(deterministic_view.nodes),
        "model_view_node_count": len(model_view.nodes),
        "deterministic_role_counts": dict(
            sorted(
                Counter(item.role.value for item in deterministic_view.nodes).items()
            )
        ),
        "model_role_counts": dict(
            sorted(Counter(item.role.value for item in model_view.nodes).items())
        ),
    }


def _write_report_atomic(destination: Path, payload: dict[str, object]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_exception_code(exc: Exception) -> str:
    message = str(exc).strip()
    if message in _SAFE_EXCEPTION_REASON_CODES:
        return message
    if message in _SAFE_EXCEPTION_MESSAGE_CODES:
        return _SAFE_EXCEPTION_MESSAGE_CODES[message]
    code = getattr(exc, "code", None)
    code_value = getattr(code, "value", None)
    if code_value in _SAFE_EXCEPTION_REASON_CODES:
        return str(code_value)
    return type(exc).__name__


def _safe_error(reason_code: str, *, stage: str | None = None) -> None:
    payload = {"status": "ERROR", "reason_code": reason_code}
    if stage is not None:
        payload["stage"] = stage
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
