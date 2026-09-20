"""Strict AI-only ledger for non-PDF whole-document visual review.

The ledger contains verdicts and stable issue codes only.  It cannot contain
customer body text, cannot claim human or formal approval, and is accepted
only while both the bundle hash and its complete artifact inventory remain
pinned to externally supplied values.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.document_visual_review_bundle import (
    DocumentVisualReviewBundleRejected,
    canonical_json_bytes,
    verify_document_visual_review_bundle,
)


SCHEMA_VERSION = "document-visual-ai-pre-review/v1"
STATUS = "AI_PRE_REVIEW"
LOCATION = "CONTROLLED_EXTERNAL"
ARTIFACT_HASH_CONTRACT = "canonical-json-without-artifact-sha256/v1"
VERDICTS = frozenset({"PASS", "ISSUE", "NOT_EVALUATED"})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-/]{0,255}$")
_ISSUE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
ISSUE_CODES = frozenset(
    {
        "SOURCE_RENDER_CLIPPED",
        "SOURCE_RENDER_MISSING",
        "SOURCE_RENDER_UNREADABLE",
        "SOURCE_RENDER_LAYOUT_SHIFT",
        "PIPELINE_TEXT_OMISSION",
        "PIPELINE_TABLE_TOPOLOGY_MISMATCH",
        "PIPELINE_IMAGE_OMISSION",
        "PIPELINE_ORDER_MISMATCH",
        "PIPELINE_DUPLICATE_REQUIREMENT",
        "PIPELINE_FALSE_REQUIREMENT",
        "PIPELINE_NOT_RUN",
        "REMOTE_VISUAL_REQUIRED",
    }
)
NOT_EVALUATED_CODES = frozenset(
    {"PIPELINE_NOT_RUN", "REMOTE_VISUAL_REQUIRED"}
)
_PRIVACY = {
    "classification": "CUSTOMER_SENSITIVE",
    "handling": "CONTROLLED_LOCAL_REVIEW_ONLY",
    "contains_customer_text": False,
    "contains_source_path": False,
    "contains_source_filename": False,
}
_PROVENANCE = {
    "is_technical_draft": False,
    "is_human_review": False,
    "is_human_gold": False,
    "is_gold": False,
    "is_formal_approval": False,
    "is_serving_activation_gate": False,
}


class DocumentVisualAiPreReviewContractError(RuntimeError):
    """Stable, text-free contract rejection."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def canonical_artifact_sha256(artifact: Mapping[str, Any]) -> str:
    payload = dict(artifact)
    payload.pop("artifact_sha256", None)
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DocumentVisualAiPreReviewContractError(
            "artifact_not_canonical_json"
        ) from exc
    return hashlib.sha256(serialized).hexdigest()


def recompute_summary(pages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pass_count = sum(page.get("verdict") == "PASS" for page in pages)
    issue_count = sum(page.get("verdict") == "ISSUE" for page in pages)
    not_evaluated_count = sum(
        page.get("verdict") == "NOT_EVALUATED" for page in pages
    )
    pages_with_issue_codes = [
        int(page["page_number"])
        for page in pages
        if page.get("issue_codes")
    ]
    issue_code_count = sum(
        len(page.get("issue_codes", [])) for page in pages
    )
    if pages and pass_count == len(pages):
        verdict = "PASS"
    elif issue_count:
        verdict = "ISSUE"
    else:
        verdict = "NOT_EVALUATED"
    return {
        "page_count": len(pages),
        "pass_count": pass_count,
        "issue_count": issue_count,
        "not_evaluated_count": not_evaluated_count,
        "pages_with_issue_codes": pages_with_issue_codes,
        "issue_code_count": issue_code_count,
        "verdict": verdict,
    }


def prepare_document_visual_ai_pre_review(
    *,
    bundle_dir: Path,
    source: Path,
    document_format: str,
    expected_source_sha256: str,
    render_profile_path: Path,
    expected_render_profile_sha256: str,
    pipeline_profile_path: Path,
    expected_pipeline_profile_sha256: str,
    expected_review_bundle_sha256: str,
    expected_artifact_inventory_sha256: str,
    reviewer: Mapping[str, Any],
    page_reviews: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build an AI pre-review ledger from one strictly verified bundle."""

    bundle = _verify_bundle(
        bundle_dir=bundle_dir,
        source=source,
        document_format=document_format,
        expected_source_sha256=expected_source_sha256,
        render_profile_path=render_profile_path,
        expected_render_profile_sha256=expected_render_profile_sha256,
        pipeline_profile_path=pipeline_profile_path,
        expected_pipeline_profile_sha256=(
            expected_pipeline_profile_sha256
        ),
        expected_review_bundle_sha256=expected_review_bundle_sha256,
        expected_artifact_inventory_sha256=(
            expected_artifact_inventory_sha256
        ),
    )
    pages = _validated_pages(
        page_reviews,
        expected_page_count=bundle["document"]["page_count"],
    )
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_hash_contract": ARTIFACT_HASH_CONTRACT,
        "status": STATUS,
        "location": LOCATION,
        "review_bundle_sha256": _sha256(
            expected_review_bundle_sha256,
            "expected_review_bundle_sha256_invalid",
        ),
        "artifact_inventory_sha256": _sha256(
            expected_artifact_inventory_sha256,
            "expected_artifact_inventory_sha256_invalid",
        ),
        "binding": _bundle_binding(bundle),
        "reviewer": _validated_reviewer(reviewer),
        "pages": pages,
        "summary": recompute_summary(pages),
        "privacy": dict(_PRIVACY),
        "review_provenance": dict(_PROVENANCE),
    }
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    return artifact


def validate_document_visual_ai_pre_review(
    artifact: Mapping[str, Any],
    *,
    bundle_dir: Path,
    source: Path,
    document_format: str,
    expected_source_sha256: str,
    render_profile_path: Path,
    expected_render_profile_sha256: str,
    pipeline_profile_path: Path,
    expected_pipeline_profile_sha256: str,
    expected_review_bundle_sha256: str,
    expected_artifact_inventory_sha256: str,
    expected_artifact_sha256: str,
) -> dict[str, Any]:
    """Revalidate the ledger, its external pins, and the entire bundle tree."""

    root = _object(_json_clone(artifact), "artifact_not_object")
    _exact(
        root,
        {
            "schema_version",
            "artifact_hash_contract",
            "status",
            "location",
            "review_bundle_sha256",
            "artifact_inventory_sha256",
            "binding",
            "reviewer",
            "pages",
            "summary",
            "privacy",
            "review_provenance",
            "artifact_sha256",
        },
        "artifact",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version_mismatch")
    if root["artifact_hash_contract"] != ARTIFACT_HASH_CONTRACT:
        _fail("artifact_hash_contract_mismatch")
    if root["status"] != STATUS:
        _fail("status_must_be_ai_pre_review")
    if root["location"] != LOCATION:
        _fail("location_must_be_controlled_external")
    expected_bundle_hash = _sha256(
        expected_review_bundle_sha256,
        "expected_review_bundle_sha256_invalid",
    )
    expected_inventory_hash = _sha256(
        expected_artifact_inventory_sha256,
        "expected_artifact_inventory_sha256_invalid",
    )
    expected_ledger_hash = _sha256(
        expected_artifact_sha256,
        "expected_artifact_sha256_invalid",
    )
    if root["review_bundle_sha256"] != expected_bundle_hash:
        _fail("review_bundle_sha256_external_pin_mismatch")
    if root["artifact_inventory_sha256"] != expected_inventory_hash:
        _fail("artifact_inventory_sha256_external_pin_mismatch")
    provided_artifact_hash = _sha256(
        root["artifact_sha256"],
        "artifact_sha256_invalid",
    )
    if provided_artifact_hash != canonical_artifact_sha256(root):
        _fail("artifact_sha256_mismatch")
    if provided_artifact_hash != expected_ledger_hash:
        _fail("expected_artifact_sha256_mismatch")

    bundle = _verify_bundle(
        bundle_dir=bundle_dir,
        source=source,
        document_format=document_format,
        expected_source_sha256=expected_source_sha256,
        render_profile_path=render_profile_path,
        expected_render_profile_sha256=expected_render_profile_sha256,
        pipeline_profile_path=pipeline_profile_path,
        expected_pipeline_profile_sha256=(
            expected_pipeline_profile_sha256
        ),
        expected_review_bundle_sha256=expected_bundle_hash,
        expected_artifact_inventory_sha256=expected_inventory_hash,
    )
    expected_binding = _bundle_binding(bundle)
    if root["binding"] != expected_binding:
        _fail("binding_bundle_mismatch")
    reviewer = _validated_reviewer(root["reviewer"])
    if root["reviewer"] != reviewer:
        _fail("reviewer_normalization_mismatch")
    pages = _validated_pages(
        root["pages"],
        expected_page_count=bundle["document"]["page_count"],
    )
    if root["pages"] != pages:
        _fail("pages_normalization_mismatch")
    expected_summary = recompute_summary(pages)
    if root["summary"] != expected_summary:
        _fail("summary_mismatch")
    if root["privacy"] != _PRIVACY:
        _fail("privacy_contract_mismatch")
    if root["review_provenance"] != _PROVENANCE:
        _fail("review_provenance_contract_mismatch")
    return root


def _verify_bundle(
    *,
    bundle_dir: Path,
    source: Path,
    document_format: str,
    expected_source_sha256: str,
    render_profile_path: Path,
    expected_render_profile_sha256: str,
    pipeline_profile_path: Path,
    expected_pipeline_profile_sha256: str,
    expected_review_bundle_sha256: str,
    expected_artifact_inventory_sha256: str,
) -> dict[str, Any]:
    try:
        return verify_document_visual_review_bundle(
            bundle_dir,
            source=source,
            document_format=document_format,
            expected_source_sha256=expected_source_sha256,
            render_profile_path=render_profile_path,
            expected_render_profile_sha256=(
                expected_render_profile_sha256
            ),
            pipeline_profile_path=pipeline_profile_path,
            expected_pipeline_profile_sha256=(
                expected_pipeline_profile_sha256
            ),
            expected_bundle_sha256=expected_review_bundle_sha256,
            expected_artifact_inventory_sha256=(
                expected_artifact_inventory_sha256
            ),
        )
    except DocumentVisualReviewBundleRejected as exc:
        raise DocumentVisualAiPreReviewContractError(
            f"review_bundle_invalid:{exc.code}"
        ) from exc


def _bundle_binding(bundle: Mapping[str, Any]) -> dict[str, Any]:
    document = _object(bundle.get("document"), "bundle_document_invalid")
    profile_pins = _object(
        bundle.get("profile_pins"),
        "bundle_profile_pins_invalid",
    )
    render_pin = _object(
        profile_pins.get("render"),
        "bundle_render_pin_invalid",
    )
    pipeline_pin = _object(
        profile_pins.get("pipeline"),
        "bundle_pipeline_pin_invalid",
    )
    renderer = _object(bundle.get("renderer"), "bundle_renderer_invalid")
    pipeline = _object(bundle.get("pipeline"), "bundle_pipeline_invalid")
    return {
        "document_id": document["document_id"],
        "source_sha256": document["source_sha256"],
        "format": document["format"],
        "page_count": document["page_count"],
        "render_profile_file_sha256": render_pin["file_sha256"],
        "render_profile_sha256": render_pin["profile_sha256"],
        "renderer_id": renderer["id"],
        "renderer_version": renderer["version"],
        "renderer_config_sha256": renderer["config_sha256"],
        "pipeline_profile_file_sha256": pipeline_pin["file_sha256"],
        "pipeline_profile_sha256": pipeline_pin["profile_sha256"],
        "pipeline_fingerprint_sha256": (
            pipeline["pipeline_fingerprint_sha256"]
        ),
    }


def _validated_reviewer(value: Mapping[str, Any]) -> dict[str, Any]:
    reviewer = _object(value, "reviewer_invalid")
    _exact(
        reviewer,
        {
            "kind",
            "reviewer_id",
            "model_id",
            "model_version",
            "reviewed_at",
        },
        "reviewer",
    )
    if reviewer["kind"] != "AI":
        _fail("reviewer_kind_must_be_ai")
    result = {
        "kind": "AI",
        "reviewer_id": _safe_id(
            reviewer["reviewer_id"],
            "reviewer_id_invalid",
        ),
        "model_id": _safe_id(reviewer["model_id"], "reviewer_model_id_invalid"),
        "model_version": _safe_id(
            reviewer["model_version"],
            "reviewer_model_version_invalid",
        ),
        "reviewed_at": _timestamp(
            reviewer["reviewed_at"],
            "reviewer_reviewed_at_invalid",
        ),
    }
    return result


def _validated_pages(
    value: Sequence[Mapping[str, Any]],
    *,
    expected_page_count: int,
) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail("pages_invalid")
    pages: list[dict[str, Any]] = []
    page_numbers: list[int] = []
    for index, raw_page in enumerate(value):
        page = _object(raw_page, f"page_{index}_invalid")
        _exact(
            page,
            {"page_number", "verdict", "issue_codes"},
            f"page_{index}",
        )
        page_number = _positive_int(
            page["page_number"],
            f"page_{index}_number_invalid",
        )
        verdict = str(page["verdict"])
        if verdict not in VERDICTS:
            _fail(f"page_{index}_verdict_invalid")
        raw_codes = page["issue_codes"]
        if not isinstance(raw_codes, list):
            _fail(f"page_{index}_issue_codes_invalid")
        issue_codes: list[str] = []
        for code_index, raw_code in enumerate(raw_codes):
            if (
                not isinstance(raw_code, str)
                or _ISSUE_CODE.fullmatch(raw_code) is None
                or raw_code not in ISSUE_CODES
            ):
                _fail(
                    f"page_{index}_issue_code_{code_index}_invalid"
                )
            issue_codes.append(raw_code)
        if issue_codes != sorted(set(issue_codes)):
            _fail(f"page_{index}_issue_codes_not_sorted_unique")
        if verdict == "PASS" and issue_codes:
            _fail(f"page_{index}_pass_has_issue_codes")
        if verdict != "PASS" and not issue_codes:
            _fail(f"page_{index}_nonpass_missing_issue_code")
        if verdict == "ISSUE" and any(
            code in NOT_EVALUATED_CODES for code in issue_codes
        ):
            _fail(f"page_{index}_not_evaluated_code_requires_not_evaluated")
        if verdict == "NOT_EVALUATED" and any(
            code not in NOT_EVALUATED_CODES for code in issue_codes
        ):
            _fail(f"page_{index}_issue_code_requires_issue")
        pages.append(
            {
                "page_number": page_number,
                "verdict": verdict,
                "issue_codes": issue_codes,
            }
        )
        page_numbers.append(page_number)
    expected_pages = list(range(1, expected_page_count + 1))
    if page_numbers != expected_pages:
        if len(page_numbers) != len(set(page_numbers)):
            _fail("page_number_duplicate")
        if sorted(page_numbers) == expected_pages:
            _fail("pages_out_of_order")
        _fail("full_document_page_coverage_required")
    return pages


def _json_clone(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(canonical_json_bytes(value))
    except DocumentVisualReviewBundleRejected as exc:
        raise DocumentVisualAiPreReviewContractError(
            "artifact_not_canonical_json"
        ) from exc


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _exact(value: Mapping[str, Any], fields: set[str], prefix: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        _fail(f"{prefix}_unknown_field:{unknown[0]}")
    if missing:
        _fail(f"{prefix}_missing_field:{missing[0]}")


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(code)
    return value


def _safe_id(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_ID.fullmatch(value) is None
        or ".." in value
    ):
        _fail(code)
    return value


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(code)
    return value


def _timestamp(value: Any, code: str) -> str:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DocumentVisualAiPreReviewContractError(code) from exc
    if parsed.utcoffset() is None:
        _fail(code)
    return value


def _fail(code: str) -> None:
    raise DocumentVisualAiPreReviewContractError(code)


prepare_ai_pre_review = prepare_document_visual_ai_pre_review
validate_ai_pre_review = validate_document_visual_ai_pre_review
canonical_hash = canonical_artifact_sha256

__all__ = [
    "ARTIFACT_HASH_CONTRACT",
    "DocumentVisualAiPreReviewContractError",
    "ISSUE_CODES",
    "LOCATION",
    "NOT_EVALUATED_CODES",
    "SCHEMA_VERSION",
    "STATUS",
    "VERDICTS",
    "canonical_artifact_sha256",
    "canonical_hash",
    "prepare_ai_pre_review",
    "prepare_document_visual_ai_pre_review",
    "recompute_summary",
    "validate_ai_pre_review",
    "validate_document_visual_ai_pre_review",
]
