"""Contracts for a text-free, AI-only PDF visual pre-review artifact.

This artifact is deliberately separate from ``pdf_body_review_artifacts``.
It can record useful AI observations, but it can never represent human Gold,
formal approval, or a manifest-compatible READY profile.
"""

# The repository-local app path must be installed before application imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.extraction_case_runner import (
    CorpusPathError,
    require_report_destination_outside_corpus,
)
from scripts.pdf_visual_review_bundle import (
    PdfVisualReviewBundleRejected,
    verify_pdf_visual_review_bundle,
)
from scripts.storage_backup import BackupError, _rename_no_replace


SCHEMA_VERSION = "pdf_visual_ai_pre_review.v1"
STATUS = "AI_PRE_REVIEW"
LOCATION = "CONTROLLED_EXTERNAL"
ACTOR_TYPE = "AI"
REVIEW_LEVELS = ("FULL_PAGE", "HIGH_RISK_DETAIL")
DECISION_DIMENSIONS = ("text", "image", "table", "layout")
DECISIONS = ("PASS", "ISSUE", "NOT_EVALUATED")
_HIGH_RISK_DETAIL_REQUIRED_FLAGS = frozenset(
    {
        "EXCESSIVE_FIGURE_FRAGMENTATION",
        "PAGE_SPANNING_TABLE_REVIEW_REQUIRED",
    }
)

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_FINDING_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_RFC3339_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_MAX_INPUT_JSON_BYTES = 16 * 1024 * 1024

_ARTIFACT_FIELDS = {
    "schema_version",
    "status",
    "location",
    "is_human_gold",
    "is_formal_approval",
    "review_bundle_sha256",
    "artifact_sha256",
    "binding",
    "reviewer",
    "pages",
    "summary",
}
_BINDING_FIELDS = {
    "document_id",
    "source_sha256",
    "page_start",
    "page_end",
    "pdf_adapter",
    "structure_resolver",
    "preview",
    "page_presenter",
}
_COMPONENT_FIELDS = {"id", "version"}
_REVIEWER_FIELDS = {
    "actor_type",
    "model_id",
    "model_version",
    "tool_id",
    "tool_version",
    "reviewed_at_utc",
}
_PAGE_FIELDS = {
    "page_number",
    "artifact_sha256",
    "render_sha256",
    "annotated_sha256",
    "visited",
    "review_level",
    "decisions",
    "finding_codes",
}
_PAGE_REVIEW_FIELDS = {
    "page_number",
    "visited",
    "review_level",
    "decisions",
    "finding_codes",
}
_SUMMARY_FIELDS = {
    "page_count",
    "review_level_counts",
    "decision_counts",
    "pages_with_findings",
    "finding_count",
}


class PdfVisualAiPreReviewContractError(ValueError):
    """Fail-closed validation error with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 JSON representation used by this contract."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PdfVisualAiPreReviewContractError("payload_not_canonical_json") from exc
    return rendered.encode("utf-8")


def canonical_artifact_sha256(artifact: Mapping[str, Any]) -> str:
    """Hash a pre-review after excluding its self-referential artifact hash."""

    artifact_object = _require_object(artifact, "artifact_not_object")
    hash_payload = {
        key: value for key, value in artifact_object.items() if key != "artifact_sha256"
    }
    return hashlib.sha256(canonical_json_bytes(hash_payload)).hexdigest()


def canonical_pdf_visual_ai_pre_review_sha256(
    artifact: Mapping[str, Any],
) -> str:
    """Named alias for the canonical pre-review artifact hash API."""

    return canonical_artifact_sha256(artifact)


def prepare_pdf_visual_ai_pre_review(
    *,
    bundle_dir: Path,
    expected_review_bundle_sha256: str,
    reviewer: Mapping[str, Any],
    page_reviews: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create a detached AI pre-review from one verified full-document bundle.

    Bundle identity, pipeline binding, page range, and all three per-page
    artifact hashes are derived from files verified on disk. The caller only
    supplies AI decisions and an independently pinned bundle hash.
    """

    expected_bundle_hash = _require_sha256(
        expected_review_bundle_sha256,
        "expected_review_bundle_sha256_invalid",
    )
    bundle = _verify_full_document_bundle(
        bundle_dir,
        expected_bundle_sha256=expected_bundle_hash,
    )
    binding = _binding_from_bundle(bundle)
    pages = _pages_from_bundle_reviews(bundle, page_reviews)
    validated_reviewer = _validate_reviewer(reviewer)
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        "location": LOCATION,
        "is_human_gold": False,
        "is_formal_approval": False,
        "review_bundle_sha256": expected_bundle_hash,
        "artifact_sha256": "",
        "binding": binding,
        "reviewer": validated_reviewer,
        "pages": pages,
        "summary": recompute_summary(pages),
    }
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    return validate_pdf_visual_ai_pre_review(
        artifact,
        bundle_dir=bundle_dir,
        expected_artifact_sha256=artifact["artifact_sha256"],
        expected_review_bundle_sha256=expected_bundle_hash,
    )


def write_pdf_visual_ai_pre_review(
    *,
    bundle_dir: Path,
    expected_review_bundle_sha256: str,
    reviewer: Mapping[str, Any],
    page_reviews: list[Mapping[str, Any]],
    output_path: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    """Prepare, validate, and atomically publish one detached AI ledger.

    The destination must be outside the supplied corpus root and absent.  The
    writer never overwrites an earlier review and publishes only after the
    bundle-backed contract has validated in memory.
    """

    destination = require_report_destination_outside_corpus(
        Path(output_path),
        Path(corpus_root),
    )
    if destination.exists():
        _fail("artifact_output_exists")
    artifact = prepare_pdf_visual_ai_pre_review(
        bundle_dir=Path(bundle_dir),
        expected_review_bundle_sha256=expected_review_bundle_sha256,
        reviewer=reviewer,
        page_reviews=page_reviews,
    )
    payload = (
        json.dumps(
            artifact,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists():
            _fail("artifact_output_exists")
        try:
            _rename_no_replace(temporary, destination)
        except BackupError as exc:
            if destination.exists():
                raise PdfVisualAiPreReviewContractError(
                    "artifact_output_exists"
                ) from exc
            raise PdfVisualAiPreReviewContractError(
                "artifact_atomic_publish_failed"
            ) from exc
        _fsync_directory(destination.parent)
        return artifact
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_pdf_visual_ai_pre_review(
    artifact: Mapping[str, Any],
    *,
    bundle_dir: Path,
    expected_artifact_sha256: str,
    expected_review_bundle_sha256: str,
) -> dict[str, Any]:
    """Validate the contract against verified bundle files and external pins.

    Both expected hashes are mandatory. Internal self-hashes alone are not a
    trust decision and cannot prove that an artifact was reviewed.
    """

    expected_bundle_hash = _require_sha256(
        expected_review_bundle_sha256,
        "expected_review_bundle_sha256_invalid",
    )
    expected_artifact_hash = _require_sha256(
        expected_artifact_sha256,
        "expected_artifact_sha256_invalid",
    )
    bundle = _verify_full_document_bundle(
        bundle_dir,
        expected_bundle_sha256=expected_bundle_hash,
    )
    validated = _validate_pdf_visual_ai_pre_review_internal(artifact)
    if validated["artifact_sha256"] != expected_artifact_hash:
        _fail("expected_artifact_sha256_mismatch")
    if validated["review_bundle_sha256"] != expected_bundle_hash:
        _fail("expected_review_bundle_sha256_mismatch")
    if validated["binding"] != _binding_from_bundle(bundle):
        _fail("review_bundle_binding_mismatch")
    _validate_pages_against_bundle(validated["pages"], bundle)
    return validated


def _validate_pdf_visual_ai_pre_review_internal(
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    artifact_object = _require_object(artifact, "artifact_not_object")
    _require_exact_fields(artifact_object, _ARTIFACT_FIELDS, "artifact")

    if artifact_object["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version_mismatch")
    if artifact_object["status"] != STATUS:
        _fail("status_must_be_ai_pre_review")
    if artifact_object["location"] != LOCATION:
        _fail("location_must_be_controlled_external")
    if artifact_object["is_human_gold"] is not False:
        _fail("artifact_cannot_claim_human_gold")
    if artifact_object["is_formal_approval"] is not False:
        _fail("artifact_cannot_claim_formal_approval")

    _require_sha256(
        artifact_object["review_bundle_sha256"],
        "review_bundle_sha256_invalid",
    )
    provided_artifact_hash = _require_sha256(
        artifact_object["artifact_sha256"],
        "artifact_sha256_invalid",
    )
    binding = _validate_binding(artifact_object["binding"])
    _validate_reviewer(artifact_object["reviewer"])
    pages = _validate_pages(
        artifact_object["pages"],
        page_start=binding["page_start"],
        page_end=binding["page_end"],
    )
    summary = _validate_summary(artifact_object["summary"])
    if summary != recompute_summary(pages):
        _fail("summary_drift")

    computed_artifact_hash = canonical_artifact_sha256(artifact_object)
    if provided_artifact_hash != computed_artifact_hash:
        _fail("artifact_sha256_mismatch")
    return _json_clone(artifact_object)


def _verify_full_document_bundle(
    bundle_dir: Path,
    *,
    expected_bundle_sha256: str,
) -> dict[str, Any]:
    try:
        return verify_pdf_visual_review_bundle(
            Path(bundle_dir),
            expected_bundle_sha256=expected_bundle_sha256,
            require_full_document=True,
        )
    except PdfVisualReviewBundleRejected as exc:
        raise PdfVisualAiPreReviewContractError(
            f"review_bundle_rejected:{exc.code}"
        ) from exc


def _binding_from_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    document = _require_object(bundle.get("document"), "review_bundle_document_invalid")
    scope = _require_object(bundle.get("scope"), "review_bundle_scope_invalid")
    pipeline = _require_object(bundle.get("pipeline"), "review_bundle_pipeline_invalid")
    adapter = _require_object(
        pipeline.get("adapter"),
        "review_bundle_adapter_invalid",
    )
    resolver = _require_object(
        pipeline.get("resolver"),
        "review_bundle_resolver_invalid",
    )
    preview = _component_from_schema_version(
        pipeline.get("preview"),
        code="review_bundle_preview_invalid",
    )
    page_presenter = _component_from_schema_version(
        pipeline.get("page_comparison"),
        code="review_bundle_page_presenter_invalid",
    )
    return _validate_binding(
        {
            "document_id": document.get("document_id"),
            "source_sha256": document.get("source_sha256"),
            "page_start": scope.get("page_start"),
            "page_end": scope.get("page_end"),
            "pdf_adapter": {
                "id": adapter.get("id"),
                "version": adapter.get("version"),
            },
            "structure_resolver": {
                "id": resolver.get("id"),
                "version": resolver.get("version"),
            },
            "preview": preview,
            "page_presenter": page_presenter,
        }
    )


def _component_from_schema_version(
    value: Any,
    *,
    code: str,
) -> dict[str, str]:
    component = _require_object(value, code)
    schema_version = _require_safe_identifier(
        component.get("schema_version"),
        code,
    )
    if "/" not in schema_version:
        _fail(code)
    component_id, version = schema_version.rsplit("/", 1)
    return {
        "id": _require_safe_identifier(component_id, code),
        "version": _require_safe_identifier(version, code),
    }


def _pages_from_bundle_reviews(
    bundle: Mapping[str, Any],
    page_reviews: Any,
) -> list[dict[str, Any]]:
    bundle_pages = bundle.get("pages")
    if not isinstance(bundle_pages, list) or not bundle_pages:
        _fail("review_bundle_pages_invalid")
    if not isinstance(page_reviews, list) or not page_reviews:
        _fail("page_reviews_must_be_nonempty_array")
    if len(page_reviews) != len(bundle_pages):
        _fail("page_reviews_bundle_count_mismatch")
    pages: list[dict[str, Any]] = []
    for index, (bundle_page, page_review) in enumerate(
        zip(bundle_pages, page_reviews, strict=True)
    ):
        bundle_page_object = _require_object(
            bundle_page,
            f"review_bundle_page_{index}_invalid",
        )
        review_object = _require_object(
            page_review,
            f"page_review_{index}_not_object",
        )
        _require_exact_fields(
            review_object,
            _PAGE_REVIEW_FIELDS,
            f"page_review_{index}",
        )
        bundle_page_number = _require_positive_int(
            bundle_page_object.get("page_number"),
            f"review_bundle_page_{index}_number_invalid",
        )
        if review_object.get("page_number") != bundle_page_number:
            _fail(f"page_review_{index}_bundle_page_number_mismatch")
        if (
            _bundle_page_requires_high_risk_detail(bundle_page_object)
            and review_object.get("review_level") != "HIGH_RISK_DETAIL"
        ):
            _fail(f"page_review_{index}_high_risk_detail_required")
        artifact = _require_object(
            bundle_page_object.get("artifact"),
            f"review_bundle_page_{index}_artifact_invalid",
        )
        render = _require_object(
            bundle_page_object.get("render"),
            f"review_bundle_page_{index}_render_invalid",
        )
        annotated = _require_object(
            bundle_page_object.get("annotated"),
            f"review_bundle_page_{index}_annotated_invalid",
        )
        pages.append(
            {
                "page_number": bundle_page_number,
                "artifact_sha256": artifact.get("sha256"),
                "render_sha256": render.get("sha256"),
                "annotated_sha256": annotated.get("sha256"),
                "visited": review_object.get("visited"),
                "review_level": review_object.get("review_level"),
                "decisions": review_object.get("decisions"),
                "finding_codes": review_object.get("finding_codes"),
            }
        )
    binding = _binding_from_bundle(bundle)
    return _validate_pages(
        pages,
        page_start=binding["page_start"],
        page_end=binding["page_end"],
    )


def _validate_pages_against_bundle(
    pages: list[Mapping[str, Any]],
    bundle: Mapping[str, Any],
) -> None:
    bundle_pages = bundle.get("pages")
    if not isinstance(bundle_pages, list) or len(bundle_pages) != len(pages):
        _fail("review_bundle_page_coverage_mismatch")
    for index, (page, bundle_page) in enumerate(zip(pages, bundle_pages, strict=True)):
        bundle_page_object = _require_object(
            bundle_page,
            f"review_bundle_page_{index}_invalid",
        )
        if (
            _bundle_page_requires_high_risk_detail(bundle_page_object)
            and page.get("review_level") != "HIGH_RISK_DETAIL"
        ):
            _fail(f"page_{index}_high_risk_detail_required")
        expected = {
            "page_number": bundle_page_object.get("page_number"),
            "artifact_sha256": _require_object(
                bundle_page_object.get("artifact"),
                f"review_bundle_page_{index}_artifact_invalid",
            ).get("sha256"),
            "render_sha256": _require_object(
                bundle_page_object.get("render"),
                f"review_bundle_page_{index}_render_invalid",
            ).get("sha256"),
            "annotated_sha256": _require_object(
                bundle_page_object.get("annotated"),
                f"review_bundle_page_{index}_annotated_invalid",
            ).get("sha256"),
        }
        for field, expected_value in expected.items():
            if page.get(field) != expected_value:
                _fail(f"page_{index}_{field}_bundle_mismatch")


def _bundle_page_requires_high_risk_detail(
    bundle_page: Mapping[str, Any],
) -> bool:
    risk_flags = bundle_page.get("risk_flags")
    return isinstance(risk_flags, list) and bool(
        _HIGH_RISK_DETAIL_REQUIRED_FLAGS.intersection(
            flag for flag in risk_flags if isinstance(flag, str)
        )
    )


def recompute_summary(
    pages: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Recompute the exact deterministic summary for an already shaped ledger."""

    review_level_counts = {level: 0 for level in REVIEW_LEVELS}
    decision_counts = {
        dimension: {decision: 0 for decision in DECISIONS}
        for dimension in DECISION_DIMENSIONS
    }
    pages_with_findings = 0
    finding_count = 0
    for page in pages:
        review_level_counts[page["review_level"]] += 1
        for dimension in DECISION_DIMENSIONS:
            decision_counts[dimension][page["decisions"][dimension]] += 1
        page_findings = page["finding_codes"]
        if page_findings:
            pages_with_findings += 1
        finding_count += len(page_findings)
    return {
        "page_count": len(pages),
        "review_level_counts": review_level_counts,
        "decision_counts": decision_counts,
        "pages_with_findings": pages_with_findings,
        "finding_count": finding_count,
    }


def _validate_binding(binding: Any) -> dict[str, Any]:
    binding_object = _require_object(binding, "binding_not_object")
    _require_exact_fields(binding_object, _BINDING_FIELDS, "binding")
    document_id = _require_safe_identifier(
        binding_object["document_id"],
        "binding_document_id_invalid",
    )
    source_sha256 = _require_sha256(
        binding_object["source_sha256"],
        "binding_source_sha256_invalid",
    )
    page_start = _require_positive_int(
        binding_object["page_start"],
        "binding_page_start_invalid",
    )
    page_end = _require_positive_int(
        binding_object["page_end"],
        "binding_page_end_invalid",
    )
    if page_start > page_end:
        _fail("binding_page_range_invalid")
    return {
        "document_id": document_id,
        "source_sha256": source_sha256,
        "page_start": page_start,
        "page_end": page_end,
        "pdf_adapter": _validate_component(
            binding_object["pdf_adapter"], "binding_pdf_adapter"
        ),
        "structure_resolver": _validate_component(
            binding_object["structure_resolver"],
            "binding_structure_resolver",
        ),
        "preview": _validate_component(binding_object["preview"], "binding_preview"),
        "page_presenter": _validate_component(
            binding_object["page_presenter"],
            "binding_page_presenter",
        ),
    }


def _validate_component(component: Any, prefix: str) -> dict[str, str]:
    component_object = _require_object(component, f"{prefix}_not_object")
    _require_exact_fields(component_object, _COMPONENT_FIELDS, prefix)
    return {
        "id": _require_safe_identifier(component_object["id"], f"{prefix}_id_invalid"),
        "version": _require_safe_identifier(
            component_object["version"], f"{prefix}_version_invalid"
        ),
    }


def _validate_reviewer(reviewer: Any) -> dict[str, str]:
    reviewer_object = _require_object(reviewer, "reviewer_not_object")
    _require_exact_fields(reviewer_object, _REVIEWER_FIELDS, "reviewer")
    if reviewer_object["actor_type"] != ACTOR_TYPE:
        _fail("reviewer_actor_type_must_be_ai")
    reviewed_at_utc = _require_rfc3339_utc(
        reviewer_object["reviewed_at_utc"],
        "reviewer_reviewed_at_utc_invalid",
    )
    return {
        "actor_type": ACTOR_TYPE,
        "model_id": _require_safe_identifier(
            reviewer_object["model_id"], "reviewer_model_id_invalid"
        ),
        "model_version": _require_safe_identifier(
            reviewer_object["model_version"],
            "reviewer_model_version_invalid",
        ),
        "tool_id": _require_safe_identifier(
            reviewer_object["tool_id"], "reviewer_tool_id_invalid"
        ),
        "tool_version": _require_safe_identifier(
            reviewer_object["tool_version"],
            "reviewer_tool_version_invalid",
        ),
        "reviewed_at_utc": reviewed_at_utc,
    }


def _validate_pages(
    pages: Any,
    *,
    page_start: int,
    page_end: int,
) -> list[dict[str, Any]]:
    if not isinstance(pages, list) or not pages:
        _fail("pages_must_be_nonempty_array")
    validated_pages: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        page_object = _require_object(page, f"page_{index}_not_object")
        _require_exact_fields(page_object, _PAGE_FIELDS, f"page_{index}")
        page_number = _require_positive_int(
            page_object["page_number"],
            f"page_{index}_page_number_invalid",
        )
        if page_object["visited"] is not True:
            _fail(f"page_{index}_must_be_visited")
        review_level = page_object["review_level"]
        if review_level not in REVIEW_LEVELS:
            _fail(f"page_{index}_review_level_invalid")
        decisions = _validate_decisions(page_object["decisions"], index)
        finding_codes = _validate_finding_codes(page_object["finding_codes"], index)
        has_non_pass_decision = any(
            decision != "PASS" for decision in decisions.values()
        )
        if has_non_pass_decision and not finding_codes:
            _fail(f"page_{index}_non_pass_requires_finding")
        if not has_non_pass_decision and finding_codes:
            _fail(f"page_{index}_finding_without_non_pass")
        validated_pages.append(
            {
                "page_number": page_number,
                "artifact_sha256": _require_sha256(
                    page_object["artifact_sha256"],
                    f"page_{index}_artifact_sha256_invalid",
                ),
                "render_sha256": _require_sha256(
                    page_object["render_sha256"],
                    f"page_{index}_render_sha256_invalid",
                ),
                "annotated_sha256": _require_sha256(
                    page_object["annotated_sha256"],
                    f"page_{index}_annotated_sha256_invalid",
                ),
                "visited": True,
                "review_level": review_level,
                "decisions": decisions,
                "finding_codes": finding_codes,
            }
        )

    actual_page_numbers = [page["page_number"] for page in validated_pages]
    if len(actual_page_numbers) != len(set(actual_page_numbers)):
        _fail("pages_duplicate_page_number")
    if actual_page_numbers != sorted(actual_page_numbers):
        _fail("pages_out_of_order")
    expected_page_numbers = list(range(page_start, page_end + 1))
    if actual_page_numbers != expected_page_numbers:
        missing = set(expected_page_numbers).difference(actual_page_numbers)
        extra = set(actual_page_numbers).difference(expected_page_numbers)
        if missing and not extra:
            _fail("pages_missing_page")
        _fail("pages_range_mismatch")
    return validated_pages


def _validate_decisions(decisions: Any, page_index: int) -> dict[str, str]:
    decisions_object = _require_object(
        decisions, f"page_{page_index}_decisions_not_object"
    )
    _require_exact_fields(
        decisions_object,
        set(DECISION_DIMENSIONS),
        f"page_{page_index}_decisions",
    )
    validated: dict[str, str] = {}
    for dimension in DECISION_DIMENSIONS:
        decision = decisions_object[dimension]
        if decision not in DECISIONS:
            _fail(f"page_{page_index}_{dimension}_decision_invalid")
        validated[dimension] = decision
    return validated


def _validate_finding_codes(
    finding_codes: Any,
    page_index: int,
) -> list[str]:
    if not isinstance(finding_codes, list):
        _fail(f"page_{page_index}_finding_codes_not_array")
    validated: list[str] = []
    for code_index, code in enumerate(finding_codes):
        if not isinstance(code, str) or _FINDING_CODE_PATTERN.fullmatch(code) is None:
            _fail(f"page_{page_index}_finding_code_{code_index}_unsafe")
        validated.append(code)
    if len(validated) != len(set(validated)):
        _fail(f"page_{page_index}_finding_codes_duplicate")
    return validated


def _validate_summary(summary: Any) -> dict[str, Any]:
    summary_object = _require_object(summary, "summary_not_object")
    _require_exact_fields(summary_object, _SUMMARY_FIELDS, "summary")
    review_level_counts = _validate_counter(
        summary_object["review_level_counts"],
        expected_fields=set(REVIEW_LEVELS),
        prefix="summary_review_level_counts",
    )
    decision_counts_object = _require_object(
        summary_object["decision_counts"],
        "summary_decision_counts_not_object",
    )
    _require_exact_fields(
        decision_counts_object,
        set(DECISION_DIMENSIONS),
        "summary_decision_counts",
    )
    decision_counts = {
        dimension: _validate_counter(
            decision_counts_object[dimension],
            expected_fields=set(DECISIONS),
            prefix=f"summary_decision_counts_{dimension}",
        )
        for dimension in DECISION_DIMENSIONS
    }
    return {
        "page_count": _require_nonnegative_int(
            summary_object["page_count"],
            "summary_page_count_invalid",
        ),
        "review_level_counts": review_level_counts,
        "decision_counts": decision_counts,
        "pages_with_findings": _require_nonnegative_int(
            summary_object["pages_with_findings"],
            "summary_pages_with_findings_invalid",
        ),
        "finding_count": _require_nonnegative_int(
            summary_object["finding_count"],
            "summary_finding_count_invalid",
        ),
    }


def _validate_counter(
    value: Any,
    *,
    expected_fields: set[str],
    prefix: str,
) -> dict[str, int]:
    counter = _require_object(value, f"{prefix}_not_object")
    _require_exact_fields(counter, expected_fields, prefix)
    return {
        field: _require_nonnegative_int(
            counter[field],
            f"{prefix}_{field}_invalid",
        )
        for field in counter
    }


def _require_object(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _require_exact_fields(
    value: Mapping[str, Any],
    required: set[str],
    prefix: str,
) -> None:
    missing = sorted(required.difference(value))
    if missing:
        _fail(f"{prefix}_missing_field")
    unknown = sorted(set(value).difference(required))
    if unknown:
        _fail(f"{prefix}_unknown_field")


def _require_sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _require_safe_identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SAFE_IDENTIFIER_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _require_positive_int(value: Any, code: str) -> int:
    if type(value) is not int or value < 1:
        _fail(code)
    return value


def _require_nonnegative_int(value: Any, code: str) -> int:
    if type(value) is not int or value < 0:
        _fail(code)
    return value


def _require_rfc3339_utc(value: Any, code: str) -> str:
    if not isinstance(value, str) or _RFC3339_UTC_PATTERN.fullmatch(value) is None:
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PdfVisualAiPreReviewContractError(code) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _fail(code)
    return value


def _json_clone(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_json_bytes(value))


def _load_json_input(path: Path, *, code: str) -> Any:
    candidate = Path(path).expanduser().resolve()
    try:
        metadata = candidate.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 1
            or metadata.st_size > _MAX_INPUT_JSON_BYTES
        ):
            _fail(code)
        payload = candidate.read_text(encoding="utf-8")
        return json.loads(payload, object_pairs_hook=_reject_duplicate_json_pairs)
    except PdfVisualAiPreReviewContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PdfVisualAiPreReviewContractError(code) from exc


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fail(code: str) -> None:
    raise PdfVisualAiPreReviewContractError(code)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Write a bundle-bound AI PDF visual pre-review ledger. "
            "The result is never Human Gold or formal approval."
        )
    )
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--expected-review-bundle-sha256", required=True)
    parser.add_argument("--reviewer-json", required=True, type=Path)
    parser.add_argument("--page-reviews-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--corpus-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        reviewer = _load_json_input(
            args.reviewer_json,
            code="reviewer_json_invalid",
        )
        page_reviews = _load_json_input(
            args.page_reviews_json,
            code="page_reviews_json_invalid",
        )
        artifact = write_pdf_visual_ai_pre_review(
            bundle_dir=args.bundle_dir,
            expected_review_bundle_sha256=args.expected_review_bundle_sha256,
            reviewer=reviewer,
            page_reviews=page_reviews,
            output_path=args.output,
            corpus_root=args.corpus_root,
        )
    except CorpusPathError:
        print("artifact_output_inside_corpus", file=sys.stderr)
        return 2
    except PdfVisualAiPreReviewContractError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "WRITTEN",
                "artifact_sha256": artifact["artifact_sha256"],
                "review_bundle_sha256": artifact["review_bundle_sha256"],
                "page_count": artifact["summary"]["page_count"],
                "is_human_gold": False,
                "is_formal_approval": False,
            },
            sort_keys=True,
        )
    )
    return 0


# Short aliases keep the public API discoverable without implying integration
# with the formal PDF body-review finalizer.
prepare_ai_pre_review = prepare_pdf_visual_ai_pre_review
validate_ai_pre_review = validate_pdf_visual_ai_pre_review
canonical_hash = canonical_artifact_sha256


__all__ = [
    "ACTOR_TYPE",
    "DECISIONS",
    "DECISION_DIMENSIONS",
    "LOCATION",
    "PdfVisualAiPreReviewContractError",
    "REVIEW_LEVELS",
    "SCHEMA_VERSION",
    "STATUS",
    "canonical_artifact_sha256",
    "canonical_hash",
    "canonical_json_bytes",
    "canonical_pdf_visual_ai_pre_review_sha256",
    "prepare_ai_pre_review",
    "prepare_pdf_visual_ai_pre_review",
    "recompute_summary",
    "write_pdf_visual_ai_pre_review",
    "validate_ai_pre_review",
    "validate_pdf_visual_ai_pre_review",
]


if __name__ == "__main__":
    raise SystemExit(main())
