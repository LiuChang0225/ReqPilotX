#!/usr/bin/env python3
"""Build and evaluate one review-only PDF extraction checklist.

The report contains only identities, hashes, labels, counts, enum values, and
PASS/FAIL/BLOCKED reason codes.  Source text, OCR text, page images, and
SourceIR payloads are never written by this command.
"""

from __future__ import annotations

# The repository-local app path must be installed before application imports.
# ruff: noqa: E402

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.private_eval import private_eval_path

from app.adapters.documents.azure_document_intelligence_visual_text_provider import (
    AzureDocumentIntelligenceVisualTextProvider,
)
from app.adapters.documents.bailian_visual_text_provider import (
    BailianVisualTextProvider,
)
from app.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from app.adapters.documents.pdf_visual_text_runtime import (
    OCR_PROVIDER_AZURE,
    OCR_PROVIDER_BAILIAN,
    OCR_PROVIDER_BAILIAN_LAYOUT,
    build_pdf_visual_text_runtime_from_silver,
    build_remote_visual_text_provider,
    normalize_input_rotations_by_page,
    runtime_setting,
    source_sha256,
    validate_pdf_visual_text_capture,
    visual_text_provider_id,
)
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.classify_content import classify_content
from app.application.extraction.evaluate_pdf_shadow_silver import (
    DEFAULT_PDF_SHADOW_SILVER_PATH,
    DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
    PdfShadowSilverCheckStatus,
    evaluate_pdf_shadow_silver,
    evaluate_pdf_shadow_silver_subset,
    load_pdf_shadow_silver,
)
from app.application.extraction.project_requirements import project_requirements
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.requirement_view import RequirementProjectionPolicy
from app.domain.extraction.visual_text import (
    VISUAL_CONTENT_TYPES,
    VisualContentType,
)
from app.services.ai_copilot import load_ai_copilot_config


DEFAULT_EXTRACTION_MANIFEST_PATH = (
    private_eval_path("extraction_cases/manifest.json")
)
_SAFE_RUNTIME_ERROR_CODES = frozenset(
    {
        "azure_input_rotation_not_supported",
        "document_not_registered",
        "silver_projection_policy_mismatch",
        "source_hash_mismatch",
        "subset_page_not_authorized",
        "subset_page_scope_duplicate",
        "subset_page_scope_empty",
        "subset_page_scope_invalid",
        "page_layout_override_requires_bailian_default",
        "page_layout_override_requires_full_scope",
        "trusted_document_context_invalid",
        "trusted_document_context_route_unsupported",
        "trusted_document_context_source_mismatch",
        "trusted_document_context_unavailable",
        "visual_text_provider_identity_mismatch",
        "visual_text_provider_not_allowed",
        "visual_text_rotation_scope_mismatch",
        "pdf_visual_text_capture_page_ambiguous",
        "pdf_visual_text_capture_asset_hash_mismatch",
        "pdf_visual_text_capture_page_hash_mismatch",
        "pdf_visual_text_capture_page_not_authorized",
        "pdf_visual_text_capture_page_scope_mismatch",
        "pdf_visual_text_capture_provider_mismatch",
        "pdf_visual_text_capture_source_mismatch",
        "pdf_visual_text_out_of_scope_evidence_mismatch",
        "pdf_visual_text_required_page_not_authorized",
        "pdf_visual_text_source_hash_mismatch",
        "pdf_visual_table_capture_binding_mismatch",
        "pdf_visual_table_capture_scope_mismatch",
        "pdf_visual_table_capture_unexpected",
    }
)


class PdfShadowChecklistRejected(RuntimeError):
    """Content-free runtime rejection safe to expose in checklist output."""

    def __init__(
        self,
        code: str,
        *,
        failure_codes: tuple[str, ...] = (),
        failure_pages: tuple[int, ...] = (),
    ) -> None:
        self.code = str(code or "checklist_rejected")
        self.failure_codes = tuple(failure_codes)
        self.failure_pages = tuple(failure_pages)
        super().__init__(self.code)


def _page_argument(value: str) -> int:
    try:
        page_number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("subset_page_scope_invalid") from exc
    if page_number < 1 or str(page_number) != value.strip():
        raise argparse.ArgumentTypeError("subset_page_scope_invalid")
    return page_number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a source-bound, text-free PDF shadow checklist."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--ocr-provider",
        choices=(
            OCR_PROVIDER_BAILIAN,
            OCR_PROVIDER_BAILIAN_LAYOUT,
            OCR_PROVIDER_AZURE,
        ),
        default=OCR_PROVIDER_BAILIAN,
        help=(
            "Remote visual provider. Bailian OCR is the compatibility default; "
            "the reviewed Qwen page-layout route is explicit and has no fallback."
        ),
    )
    parser.add_argument(
        "--bailian-model",
        default=None,
        help="Optional selected Bailian provider model override; ignored for Azure.",
    )
    parser.add_argument(
        "--page",
        action="append",
        type=_page_argument,
        default=None,
        help=(
            "One authorized one-based page to evaluate. Repeat for a subset. "
            "Omit only for the backward-compatible full registered scope."
        ),
    )
    parser.add_argument(
        "--page-layout-page",
        action="append",
        type=_page_argument,
        default=None,
        help=(
            "One authorized page that must use the explicit Bailian page-layout "
            "provider while all other registered pages use --ocr-provider. "
            "Repeat as needed. Full-document scope and --ocr-provider bailian "
            "are required; there is no runtime fallback."
        ),
    )
    parser.add_argument(
        "--silver",
        type=Path,
        default=DEFAULT_PDF_SHADOW_SILVER_PATH,
    )
    parser.add_argument(
        "--silver-schema",
        type=Path,
        default=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_EXTRACTION_MANIFEST_PATH,
        help="Trusted document role and ingress-route registry.",
    )
    return parser


def _selected_page_numbers_from_args(
    raw_pages: list[int] | tuple[int, ...] | None,
    *,
    authorized_page_numbers: frozenset[int],
) -> frozenset[int] | None:
    """Return an explicit authorized subset or ``None`` for full compatibility."""

    if raw_pages is None:
        return None
    if not isinstance(raw_pages, (list, tuple)) or not raw_pages:
        raise PdfShadowChecklistRejected("subset_page_scope_empty")
    page_numbers = tuple(raw_pages)
    if any(
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
        for page_number in page_numbers
    ):
        raise PdfShadowChecklistRejected("subset_page_scope_invalid")
    if len(page_numbers) != len(set(page_numbers)):
        raise PdfShadowChecklistRejected("subset_page_scope_duplicate")
    selected = frozenset(page_numbers)
    if not selected.issubset(authorized_page_numbers):
        raise PdfShadowChecklistRejected("subset_page_not_authorized")
    return selected


def _page_layout_provider_overrides_from_args(
    raw_pages: list[int] | tuple[int, ...] | None,
    *,
    authorized_page_numbers: frozenset[int],
    selected_page_numbers: frozenset[int] | None,
    default_provider_name: str,
) -> dict[int, str]:
    if raw_pages is None:
        return {}
    if selected_page_numbers is not None:
        raise PdfShadowChecklistRejected(
            "page_layout_override_requires_full_scope"
        )
    if default_provider_name != OCR_PROVIDER_BAILIAN:
        raise PdfShadowChecklistRejected(
            "page_layout_override_requires_bailian_default"
        )
    routed_pages = _selected_page_numbers_from_args(
        raw_pages,
        authorized_page_numbers=authorized_page_numbers,
    )
    if routed_pages is None:
        raise AssertionError("explicit page-layout route lost its page scope")
    return {
        page_number: OCR_PROVIDER_BAILIAN_LAYOUT
        for page_number in sorted(routed_pages)
    }


def _expected_evaluated_content_types(
    provider_name: str,
) -> tuple[VisualContentType, ...]:
    if provider_name == OCR_PROVIDER_BAILIAN_LAYOUT:
        return VISUAL_CONTENT_TYPES
    return ("TEXT",)


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

    try:
        registry = load_pdf_shadow_silver(
            args.silver.resolve(),
            schema_path=args.silver_schema.resolve(),
        )
        silver_document = registry.documents.get(args.document_id)
        if silver_document is None:
            raise RuntimeError("document_not_registered")
        if _source_sha256(source) != silver_document.source_sha256:
            raise RuntimeError("source_hash_mismatch")
        projection_policy = _trusted_projection_policy(
            manifest_path=args.manifest.resolve(),
            document_id=args.document_id,
            source_sha256=silver_document.source_sha256,
        )
        if projection_policy is not silver_document.projection_policy:
            raise RuntimeError("silver_projection_policy_mismatch")
        authorized_page_numbers = frozenset(
            item.page_number for item in silver_document.page_assets
        )
        selected_page_numbers = _selected_page_numbers_from_args(
            args.page,
            authorized_page_numbers=authorized_page_numbers,
        )
        page_provider_names_by_page = _page_layout_provider_overrides_from_args(
            args.page_layout_page,
            authorized_page_numbers=authorized_page_numbers,
            selected_page_numbers=selected_page_numbers,
            default_provider_name=args.ocr_provider,
        )
        runtime = build_pdf_visual_text_runtime_from_silver(
            source=source,
            silver_document=silver_document,
            provider_name=args.ocr_provider,
            bailian_model=args.bailian_model,
            visual_text_page_numbers=(
                selected_page_numbers if selected_page_numbers is not None else None
            ),
            page_provider_names_by_page=page_provider_names_by_page,
        )
        source_ir = build_source_ir(
            source,
            adapters=(runtime.pdf_adapter,),
        )
        _raise_for_visual_provider_runtime_failure(source_ir)
        validate_pdf_visual_text_capture(source_ir, runtime)
        visual_text_provider = runtime.visual_text_provider
        if selected_page_numbers is None:
            structure_tree = build_structure_tree(source_ir)
            semantics = classify_content(source_ir, structure_tree)
            requirement_view = project_requirements(
                source_ir,
                structure_tree,
                semantics,
                projection_policy=projection_policy,
            )
            report = evaluate_pdf_shadow_silver(
                document_id=args.document_id,
                source_ir=source_ir,
                structure_tree=structure_tree,
                semantics=semantics,
                requirement_view=requirement_view,
                registry=registry,
            )
            artifact_scope = "FULL_DOCUMENT"
        else:
            if visual_text_provider is None:
                raise RuntimeError("visual_text_provider_identity_mismatch")
            report = evaluate_pdf_shadow_silver_subset(
                document_id=args.document_id,
                source_ir=source_ir,
                registry=registry,
                selected_page_numbers=tuple(sorted(selected_page_numbers)),
                provider_id=visual_text_provider.provider_id,
                provider_version=visual_text_provider.provider_version,
                expected_evaluated_content_types=(
                    _expected_evaluated_content_types(args.ocr_provider)
                ),
            )
            artifact_scope = "PAGE_SUBSET"
        _write_report_atomic(destination, report.model_dump(mode="json"))
    except PdfShadowChecklistRejected as exc:
        _safe_error(
            exc.code,
            failure_codes=exc.failure_codes,
            failure_pages=exc.failure_pages,
        )
        return 2
    except RuntimeError as exc:
        runtime_code = str(exc or "")
        _safe_error(
            runtime_code
            if runtime_code in _SAFE_RUNTIME_ERROR_CODES
            else type(exc).__name__
        )
        return 2
    except Exception as exc:
        _safe_error(type(exc).__name__)
        return 2

    print(
        json.dumps(
            _report_summary_payload(
                report=report,
                visual_text_provider=visual_text_provider,
                visual_text_page_count=len(runtime.visual_text_page_numbers),
                artifact_scope=artifact_scope,
                destination=destination,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.status is PdfShadowSilverCheckStatus.PASS else 4


def _report_summary_payload(
    *,
    report: object,
    visual_text_provider: object | None,
    visual_text_page_count: int,
    artifact_scope: str,
    destination: Path,
) -> dict[str, object]:
    """Return the content-free CLI rollup, keeping blocked checks separate."""

    return {
        "status": getattr(getattr(report, "status"), "value"),
        "passed_check_count": getattr(report, "passed_check_count"),
        "failed_check_count": getattr(report, "failed_check_count"),
        "blocked_check_count": getattr(report, "blocked_check_count", 0),
        "visual_text_provider": (
            getattr(visual_text_provider, "provider_id")
            if visual_text_provider is not None
            else None
        ),
        "visual_text_page_count": visual_text_page_count,
        "artifact_scope": artifact_scope,
        "report": str(destination),
    }


def _build_scoped_pdf_adapter(
    *,
    visual_text_page_numbers: frozenset[int],
    input_rotations_by_page: Mapping[int, int] | None = None,
    allowed_visual_provider_ids: frozenset[str],
    provider_name: str,
    bailian_model: str | None,
) -> tuple[
    PdfSourceIRAdapter,
    BailianVisualTextProvider | AzureDocumentIntelligenceVisualTextProvider | None,
]:
    """Build a native-only or exact-page remote OCR adapter.

    An empty registered scope is an explicit prohibition on remote OCR.  It
    therefore neither resolves credentials nor constructs a provider.  A
    non-empty scope validates the selected provider against the document's
    Silver policy before any credential lookup or remote-capable object is
    created.
    """

    normalized_rotations = _normalize_input_rotations_by_page(
        visual_text_page_numbers=visual_text_page_numbers,
        input_rotations_by_page=input_rotations_by_page,
    )

    if not visual_text_page_numbers:
        return (
            PdfSourceIRAdapter(
                visual_text_page_numbers=visual_text_page_numbers,
            ),
            None,
        )

    provider_id = _visual_text_provider_id(provider_name)
    if provider_id not in allowed_visual_provider_ids:
        raise RuntimeError("visual_text_provider_not_allowed")
    if provider_name == OCR_PROVIDER_AZURE and any(normalized_rotations.values()):
        raise RuntimeError("azure_input_rotation_not_supported")
    provider = _build_visual_text_provider(
        provider_name=provider_name,
        bailian_model=bailian_model,
        input_rotations_by_page=normalized_rotations,
    )
    if provider.provider_id != provider_id:
        raise RuntimeError("visual_text_provider_identity_mismatch")
    return (
        PdfSourceIRAdapter(
            visual_text_provider=provider,
            visual_text_page_numbers=visual_text_page_numbers,
        ),
        provider,
    )


def _visual_text_provider_id(provider_name: str) -> str:
    return visual_text_provider_id(provider_name)


def _build_visual_text_provider(
    *,
    provider_name: str,
    bailian_model: str | None,
    input_rotations_by_page: Mapping[int, int] | None = None,
) -> BailianVisualTextProvider | AzureDocumentIntelligenceVisualTextProvider:
    return build_remote_visual_text_provider(
        provider_name=provider_name,
        bailian_model=bailian_model,
        input_rotations_by_page=input_rotations_by_page,
        bailian_config_loader=load_ai_copilot_config,
        runtime_setting_loader=_runtime_setting,
        bailian_provider_type=BailianVisualTextProvider,
        azure_provider_type=AzureDocumentIntelligenceVisualTextProvider,
    )


def _normalize_input_rotations_by_page(
    *,
    visual_text_page_numbers: frozenset[int],
    input_rotations_by_page: Mapping[int, int] | None,
) -> dict[int, int]:
    return normalize_input_rotations_by_page(
        visual_text_page_numbers=visual_text_page_numbers,
        input_rotations_by_page=input_rotations_by_page,
    )


def _runtime_setting(*names: str) -> str:
    return runtime_setting(*names)


def _source_sha256(source: Path) -> str:
    return source_sha256(source)


def _trusted_projection_policy(
    *,
    manifest_path: Path,
    document_id: str,
    source_sha256: str,
) -> RequirementProjectionPolicy:
    """Resolve projection behavior only from the source-bound ingress registry."""

    try:
        payload = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
        documents = payload["documents"]
        document = documents[document_id]
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeError("trusted_document_context_unavailable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "1.2.0"
        or not isinstance(documents, dict)
        or not isinstance(document, dict)
    ):
        raise RuntimeError("trusted_document_context_invalid")
    if document.get("source_sha256") != source_sha256:
        raise RuntimeError("trusted_document_context_source_mismatch")

    ingress_route = document.get("ingress_route")
    if ingress_route == "requirement_source":
        return RequirementProjectionPolicy.SHADOW_CANDIDATES
    if ingress_route == "downstream_artifact":
        return RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT
    raise RuntimeError("trusted_document_context_route_unsupported")


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _raise_for_visual_provider_runtime_failure(source_ir: object) -> None:
    failure_codes = {
        "pdf_visual_text_unavailable",
        "pdf_visual_text_invalid_input",
        "pdf_visual_text_source_hash_mismatch",
        "pdf_visual_text_page_image_hash_mismatch",
        "pdf_visual_text_provider_output_invalid",
        "pdf_visual_text_provider_failed",
    }
    diagnostics = getattr(source_ir, "diagnostics", ())
    failures = [
        item for item in diagnostics if getattr(item, "code", None) in failure_codes
    ]
    if not failures:
        return
    observed_codes = tuple(sorted({str(getattr(item, "code")) for item in failures}))
    observed_pages = tuple(
        sorted(
            {
                int(page_number)
                for item in failures
                if (
                    (locator := getattr(item, "native_locator", None)) is not None
                    and isinstance(
                        page_number := getattr(locator, "page_number", None),
                        int,
                    )
                    and not isinstance(page_number, bool)
                    and page_number > 0
                )
            }
        )
    )
    raise PdfShadowChecklistRejected(
        "visual_text_provider_runtime_failure",
        failure_codes=observed_codes,
        failure_pages=observed_pages,
    )


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
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_error(
    reason_code: str,
    *,
    failure_codes: tuple[str, ...] = (),
    failure_pages: tuple[int, ...] = (),
) -> None:
    payload: dict[str, object] = {
        "status": "ERROR",
        "reason_code": reason_code,
    }
    if failure_codes:
        payload["failure_codes"] = list(failure_codes)
    if failure_pages:
        payload["failure_pages"] = list(failure_pages)
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
