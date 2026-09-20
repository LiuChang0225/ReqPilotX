"""Pure contracts for preparing and finalizing PDF body review artifacts.

This module deliberately has no file, network, credential, or reviewer-registry
I/O.  ``prepare_pdf_body_review_draft`` can only create an untrusted DRAFT.
There is intentionally no function that creates an approval.  A controlled
human-review workflow must create the approval artifact, and the caller must
provide its externally governed trusted reviewer identifiers to finalization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any


DRAFT_SCHEMA_VERSION = "pdf_body_review_draft.v1"
APPROVAL_SCHEMA_VERSION = "pdf_body_review_approval.v1"
PROFILE_SCHEMA_VERSION = "pdf_body_review_profile.v1"
PDF_BODY_REVIEW_EVALUATOR_ID = "source_ir.pdf_reviewed_body_sequence/v1"
CONTROLLED_EXTERNAL = "CONTROLLED_EXTERNAL"
REVIEW_BASIS = "ORIGINAL_PDF_PAGES"
HUMAN_REVIEWER_ROLE = "INDEPENDENT_PDF_BODY_REVIEWER"
HMAC_ID_SCHEME = "HMAC_SHA256"
ALLOWED_REVIEW_MODES = frozenset({"NATIVE", "REMOTE_VISUAL"})
ALLOWED_VISUAL_PROVIDERS = frozenset(
    {"bailian", "azure-document-intelligence"}
)
IMAGE_DISPOSITIONS = frozenset({"CONTENT", "NON_CONTENT", "UNRESOLVED"})
IMAGE_NODE_KINDS = frozenset({"image", "figure"})
IMAGE_ASSET_KINDS = frozenset({"image", "figure", "page_render"})
TABLE_DISPOSITIONS = frozenset({"CONTENT", "NON_CONTENT", "UNRESOLVED"})

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DOCUMENT_ID_PATTERN = re.compile(r"^DOC-[A-Z0-9-]+$")
_ASSERTION_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]+$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]*$")
_MEDIA_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$"
)
_RFC3339_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
_PROFILE_REQUIRED_FIELDS = {
    "schema_version",
    "assertion_id",
    "evaluator_id",
    "document_id",
    "source_sha256",
    "body_page_start",
    "body_page_end",
    "review_mode",
    "pages",
}
_PROFILE_OPTIONAL_FIELDS = {
    "approved_visual_provider",
    "visual_provider_id",
    "visual_text_provider_version_sha256",
    "visual_table_provider_version_sha256",
}
_PAGE_REQUIRED_FIELDS = {
    "page_number",
    "expected_body_line_count",
    "expected_image_count",
    "image_occurrences",
    "image_content_multiset_sha256",
    "image_sequence_sha256",
    "table_occurrences",
    "table_content_multiset_sha256",
    "table_sequence_sha256",
    "expected_table_count",
    "expected_table_row_count",
    "expected_table_cell_count",
    "expected_merged_region_count",
    "content_multiset_sha256",
    "sequence_sha256",
}
_PAGE_OPTIONAL_FIELDS = {"page_image_sha256"}
_IMAGE_OCCURRENCE_IDENTITY_FIELDS = {
    "source_order",
    "node_kind",
    "asset_kind",
    "media_type",
    "content_sha256",
    "native_locator",
    "derivation",
}
_IMAGE_OCCURRENCE_FIELDS = _IMAGE_OCCURRENCE_IDENTITY_FIELDS | {
    "occurrence_sha256",
    "disposition",
}
_IMAGE_LOCATOR_FIELDS = {"format", "page_number", "block_index", "bbox"}
_IMAGE_DERIVATION_FIELDS = {
    "kind",
    "base_content_sha256",
    "mask_content_sha256",
}
_TABLE_OCCURRENCE_IDENTITY_FIELDS = {
    "provider_object_id_sha256",
    "provider_index",
    "page_number",
    "bbox",
    "row_count",
    "column_count",
    "cell_count",
    "merged_region_count",
    "topology_sha256",
}
_TABLE_OCCURRENCE_FIELDS = _TABLE_OCCURRENCE_IDENTITY_FIELDS | {
    "occurrence_sha256",
    "disposition",
}
_BINDING_REQUIRED_FIELDS = {
    "assertion_id",
    "evaluator_id",
    "document_id",
    "source_sha256",
    "body_page_start",
    "body_page_end",
    "review_mode",
}
_BINDING_OPTIONAL_FIELDS = {
    "approved_visual_provider",
    "visual_provider_id",
    "visual_text_provider_version_sha256",
    "visual_table_provider_version_sha256",
}


class PdfBodyReviewContractError(ValueError):
    """Fail-closed validation error with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical JSON representation used by all contract hashes."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PdfBodyReviewContractError("payload_not_canonical_json") from exc
    return rendered.encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON value with the contract's canonical encoding."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_pdf_image_occurrence_sha256(
    identity_payload: Mapping[str, Any],
) -> str:
    """Hash one exact PDF image occurrence identity, excluding disposition."""

    identity = _validate_image_occurrence_identity(
        identity_payload,
        prefix="image_occurrence_identity",
    )
    return canonical_json_sha256(identity)


def pdf_content_image_projection(
    image_occurrences: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Project only CONTENT occurrences into the existing runner image shape."""

    occurrences = _require_list(
        image_occurrences,
        "image_occurrences_not_array",
    )
    projection: list[dict[str, str]] = []
    for index, occurrence in enumerate(occurrences):
        validated = _validate_image_occurrence(
            occurrence,
            expected_page_number=None,
            prefix=f"image_occurrence_{index}",
        )
        if validated["disposition"] != "CONTENT":
            continue
        projection.append(
            {
                "node_kind": validated["node_kind"],
                "asset_kind": validated["asset_kind"],
                "media_type": validated["media_type"],
                "content_sha256": validated["content_sha256"],
            }
        )
    return projection


def summarize_pdf_content_image_occurrences(
    image_occurrences: list[Mapping[str, Any]],
) -> dict[str, int | str]:
    """Return count and image-only digests derived from CONTENT occurrences."""

    projection = pdf_content_image_projection(image_occurrences)
    content_multiset = sorted(projection, key=canonical_json_bytes)
    return {
        "expected_image_count": len(projection),
        "image_content_multiset_sha256": canonical_json_sha256(content_multiset),
        "image_sequence_sha256": canonical_json_sha256(projection),
    }


def canonical_pdf_table_occurrence_sha256(
    identity_payload: Mapping[str, Any],
) -> str:
    """Hash one exact table occurrence identity, excluding disposition."""

    identity = _validate_table_occurrence_identity(
        identity_payload,
        prefix="table_occurrence_identity",
    )
    return canonical_json_sha256(identity)


def pdf_content_table_projection(
    table_occurrences: list[Mapping[str, Any]],
) -> list[dict[str, int | str]]:
    """Project CONTENT tables into their ordered, text-free topology shape."""

    occurrences = _require_list(
        table_occurrences,
        "table_occurrences_not_array",
    )
    projection: list[dict[str, int | str]] = []
    for index, occurrence in enumerate(occurrences):
        validated = _validate_table_occurrence(
            occurrence,
            expected_page_number=None,
            prefix=f"table_occurrence_{index}",
        )
        if validated["disposition"] != "CONTENT":
            continue
        projection.append(_table_content_projection_item(validated))
    return projection


def summarize_pdf_content_table_occurrences(
    table_occurrences: list[Mapping[str, Any]],
) -> dict[str, int | str]:
    """Derive all table counts and digests from CONTENT occurrences only."""

    projection = pdf_content_table_projection(table_occurrences)
    return {
        "expected_table_count": len(projection),
        "expected_table_row_count": sum(
            int(table["row_count"]) for table in projection
        ),
        "expected_table_cell_count": sum(
            int(table["cell_count"]) for table in projection
        ),
        "expected_merged_region_count": sum(
            int(table["merged_region_count"]) for table in projection
        ),
        "table_content_multiset_sha256": canonical_json_sha256(
            sorted(projection, key=canonical_json_bytes)
        ),
        "table_sequence_sha256": canonical_json_sha256(projection),
    }


def prepare_pdf_body_review_draft(
    profile_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Create an explicitly non-Gold DRAFT from a text-free candidate profile."""

    profile = _validate_profile(profile_payload)
    return {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "status": "DRAFT",
        "location_class": CONTROLLED_EXTERNAL,
        "is_human_gold": False,
        "profile_sha256": canonical_json_sha256(profile),
        "profile": profile,
    }


def validate_pdf_body_review_draft(
    draft: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and return a detached canonical copy of a review draft."""

    draft_object = _require_object(draft, "draft_not_object")
    _require_exact_fields(
        draft_object,
        {
            "schema_version",
            "status",
            "location_class",
            "is_human_gold",
            "profile_sha256",
            "profile",
        },
        set(),
        "draft",
    )
    if draft_object["schema_version"] != DRAFT_SCHEMA_VERSION:
        _fail("draft_schema_version_mismatch")
    if draft_object["status"] != "DRAFT":
        _fail("draft_status_must_be_draft")
    if draft_object["location_class"] != CONTROLLED_EXTERNAL:
        _fail("draft_not_controlled_external")
    if draft_object["is_human_gold"] is not False:
        _fail("draft_cannot_claim_human_gold")
    profile_hash = _require_sha256(
        draft_object["profile_sha256"], "draft_profile_sha256_invalid"
    )
    profile = _validate_profile(draft_object["profile"])
    if profile_hash != canonical_json_sha256(profile):
        _fail("draft_profile_sha256_mismatch")
    return _json_clone(draft_object)


def validate_pdf_body_review_approval(
    *,
    draft: Mapping[str, Any],
    approval: Mapping[str, Any],
    expected_scope: Mapping[str, Any],
    trusted_reviewer_ids: Collection[str],
    expected_review_policy_version: str,
    minimum_human_reviewers: int = 1,
) -> dict[str, Any]:
    """Validate an external approval against its draft and authoritative scope.

    Reviewer trust is deliberately caller-supplied.  Production callers must
    obtain that set from a permission boundary the draft-producing agent cannot
    modify; passing a self-authored allowlist would not establish independence.
    """

    validated_draft = validate_pdf_body_review_draft(draft)
    profile = validated_draft["profile"]
    authoritative_scope = _validate_binding(expected_scope, "expected_scope")
    profile_binding = _binding_from_profile(profile)
    if profile_binding != authoritative_scope:
        _fail("draft_scope_mismatch")

    approval_object = _require_object(approval, "approval_not_object")
    _require_exact_fields(
        approval_object,
        {
            "schema_version",
            "status",
            "location_class",
            "draft_sha256",
            "profile_sha256",
            "binding",
            "review_basis",
            "review_policy_version",
            "reviews",
        },
        set(),
        "approval",
    )
    if approval_object["schema_version"] != APPROVAL_SCHEMA_VERSION:
        _fail("approval_schema_version_mismatch")
    if approval_object["status"] != "APPROVED":
        _fail("approval_status_must_be_approved")
    if approval_object["location_class"] != CONTROLLED_EXTERNAL:
        _fail("approval_not_controlled_external")
    if approval_object["review_basis"] != REVIEW_BASIS:
        _fail("approval_review_basis_invalid")

    draft_hash = _require_sha256(
        approval_object["draft_sha256"], "approval_draft_sha256_invalid"
    )
    if draft_hash != canonical_json_sha256(validated_draft):
        _fail("approval_draft_sha256_mismatch")
    profile_hash = _require_sha256(
        approval_object["profile_sha256"], "approval_profile_sha256_invalid"
    )
    if profile_hash != validated_draft["profile_sha256"]:
        _fail("approval_profile_sha256_mismatch")

    approval_binding = _validate_binding(approval_object["binding"], "approval_binding")
    if approval_binding != authoritative_scope or approval_binding != profile_binding:
        _fail("approval_scope_mismatch")

    policy_version = _require_safe_identifier(
        approval_object["review_policy_version"],
        "approval_review_policy_version_invalid",
    )
    expected_policy = _require_safe_identifier(
        expected_review_policy_version,
        "expected_review_policy_version_invalid",
    )
    if policy_version != expected_policy:
        _fail("approval_review_policy_version_mismatch")

    if type(minimum_human_reviewers) is not int or minimum_human_reviewers < 1:
        _fail("minimum_human_reviewers_invalid")
    trusted_reviewers = _validate_trusted_reviewer_ids(trusted_reviewer_ids)
    reviews = _require_list(approval_object["reviews"], "approval_reviews_not_array")
    if len(reviews) < minimum_human_reviewers:
        _fail("approval_insufficient_human_reviewers")

    expected_pages = list(
        range(
            authoritative_scope["body_page_start"],
            authoritative_scope["body_page_end"] + 1,
        )
    )
    seen_reviewers: set[str] = set()
    for review_index, review in enumerate(reviews):
        review_object = _require_object(
            review, f"approval_review_{review_index}_not_object"
        )
        _require_exact_fields(
            review_object,
            {
                "reviewer_id",
                "reviewer_id_scheme",
                "actor_type",
                "reviewer_role",
                "independence_attested",
                "reviewed_at_utc",
                "review_tool",
                "page_approvals",
            },
            set(),
            f"approval_review_{review_index}",
        )
        reviewer_id = _require_sha256(
            review_object["reviewer_id"],
            f"approval_review_{review_index}_reviewer_id_invalid",
        )
        if reviewer_id in seen_reviewers:
            _fail("approval_duplicate_reviewer")
        seen_reviewers.add(reviewer_id)
        if reviewer_id not in trusted_reviewers:
            _fail("approval_reviewer_not_trusted")
        if review_object["reviewer_id_scheme"] != HMAC_ID_SCHEME:
            _fail("approval_reviewer_id_scheme_invalid")
        if review_object["actor_type"] != "HUMAN":
            _fail("approval_reviewer_must_be_human")
        if review_object["reviewer_role"] != HUMAN_REVIEWER_ROLE:
            _fail("approval_reviewer_role_invalid")
        if review_object["independence_attested"] is not True:
            _fail("approval_independence_not_attested")
        _require_rfc3339_utc(
            review_object["reviewed_at_utc"],
            f"approval_review_{review_index}_reviewed_at_invalid",
        )
        _validate_review_tool(review_object["review_tool"], review_index)
        _validate_page_approvals(
            review_object["page_approvals"],
            expected_pages=expected_pages,
            review_index=review_index,
        )

    _require_resolved_image_occurrences(profile)
    _require_resolved_table_occurrences(profile)
    return _json_clone(approval_object)


def finalize_pdf_body_review_artifacts(
    *,
    draft: Mapping[str, Any],
    approval: Mapping[str, Any],
    expected_scope: Mapping[str, Any],
    trusted_reviewer_ids: Collection[str],
    expected_review_policy_version: str,
    minimum_human_reviewers: int = 1,
) -> dict[str, Any]:
    """Return an existing-manifest-compatible READY profile after approval."""

    validated_draft = validate_pdf_body_review_draft(draft)
    validated_approval = validate_pdf_body_review_approval(
        draft=validated_draft,
        approval=approval,
        expected_scope=expected_scope,
        trusted_reviewer_ids=trusted_reviewer_ids,
        expected_review_policy_version=expected_review_policy_version,
        minimum_human_reviewers=minimum_human_reviewers,
    )
    profile = validated_draft["profile"]
    ready_profile: dict[str, Any] = {
        "status": "READY",
        "source_sha256": profile["source_sha256"],
        "body_page_start": profile["body_page_start"],
        "body_page_end": profile["body_page_end"],
        "review_mode": profile["review_mode"],
        "review_approved": True,
        "review_artifact_sha256": canonical_json_sha256(validated_approval),
        "pages": [_ready_page(page) for page in profile["pages"]],
    }
    if profile["review_mode"] == "REMOTE_VISUAL":
        ready_profile["approved_visual_provider"] = profile[
            "approved_visual_provider"
        ]
    return ready_profile


def _validate_profile(profile: Any) -> dict[str, Any]:
    profile_object = _require_object(profile, "profile_not_object")
    _require_exact_fields(
        profile_object,
        _PROFILE_REQUIRED_FIELDS,
        _PROFILE_OPTIONAL_FIELDS,
        "profile",
    )
    if profile_object["schema_version"] != PROFILE_SCHEMA_VERSION:
        _fail("profile_schema_version_mismatch")
    assertion_id = _require_string(
        profile_object["assertion_id"], "profile_assertion_id_invalid"
    )
    if _ASSERTION_ID_PATTERN.fullmatch(assertion_id) is None:
        _fail("profile_assertion_id_invalid")
    if profile_object["evaluator_id"] != PDF_BODY_REVIEW_EVALUATOR_ID:
        _fail("profile_evaluator_id_invalid")
    document_id = _require_string(
        profile_object["document_id"], "profile_document_id_invalid"
    )
    if _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None:
        _fail("profile_document_id_invalid")
    _require_sha256(
        profile_object["source_sha256"], "profile_source_sha256_invalid"
    )
    page_start = _require_nonnegative_int(
        profile_object["body_page_start"],
        "profile_body_page_start_invalid",
        minimum=1,
    )
    page_end = _require_nonnegative_int(
        profile_object["body_page_end"],
        "profile_body_page_end_invalid",
        minimum=1,
    )
    if page_start > page_end:
        _fail("profile_page_range_invalid")
    review_mode = profile_object["review_mode"]
    if review_mode not in ALLOWED_REVIEW_MODES:
        _fail("profile_review_mode_invalid")
    remote_fields = {
        "approved_visual_provider",
        "visual_provider_id",
        "visual_text_provider_version_sha256",
        "visual_table_provider_version_sha256",
    }
    present_remote_fields = remote_fields.intersection(profile_object)
    if review_mode == "NATIVE" and present_remote_fields:
        _fail("native_profile_forbids_visual_provider")
    if review_mode == "REMOTE_VISUAL":
        if present_remote_fields != remote_fields:
            _fail("remote_profile_requires_visual_provenance")
        if profile_object["approved_visual_provider"] not in ALLOWED_VISUAL_PROVIDERS:
            _fail("remote_profile_visual_provider_invalid")
        expected_provider_id = (
            "aliyun-bailian-qwen-ocr"
            if profile_object["approved_visual_provider"] == "bailian"
            else "azure-document-intelligence"
        )
        if profile_object["visual_provider_id"] != expected_provider_id:
            _fail("remote_profile_visual_provider_id_mismatch")
        _require_sha256(
            profile_object["visual_text_provider_version_sha256"],
            "remote_profile_visual_text_provider_version_sha256_invalid",
        )
        _require_sha256(
            profile_object["visual_table_provider_version_sha256"],
            "remote_profile_visual_table_provider_version_sha256_invalid",
        )

    pages = _require_list(profile_object["pages"], "profile_pages_not_array")
    expected_page_numbers = list(range(page_start, page_end + 1))
    observed_page_numbers: list[int] = []
    for page_index, page in enumerate(pages):
        page_object = _require_object(page, f"profile_page_{page_index}_not_object")
        _require_exact_fields(
            page_object,
            _PAGE_REQUIRED_FIELDS,
            _PAGE_OPTIONAL_FIELDS,
            f"profile_page_{page_index}",
        )
        page_number = _require_nonnegative_int(
            page_object["page_number"],
            f"profile_page_{page_index}_page_number_invalid",
            minimum=1,
        )
        observed_page_numbers.append(page_number)
        has_page_image = "page_image_sha256" in page_object
        if review_mode == "NATIVE" and has_page_image:
            _fail("native_profile_forbids_page_image_sha256")
        if review_mode == "REMOTE_VISUAL" and not has_page_image:
            _fail("remote_profile_requires_page_image_sha256")
        if has_page_image:
            _require_sha256(
                page_object["page_image_sha256"],
                f"profile_page_{page_index}_page_image_sha256_invalid",
            )
        counts = {
            field: _require_nonnegative_int(
                page_object[field], f"profile_page_{page_index}_{field}_invalid"
            )
            for field in (
                "expected_body_line_count",
                "expected_image_count",
                "expected_table_count",
                "expected_table_row_count",
                "expected_table_cell_count",
                "expected_merged_region_count",
            )
        }
        _require_sha256(
            page_object["content_multiset_sha256"],
            f"profile_page_{page_index}_content_multiset_sha256_invalid",
        )
        _require_sha256(
            page_object["sequence_sha256"],
            f"profile_page_{page_index}_sequence_sha256_invalid",
        )
        _require_sha256(
            page_object["image_content_multiset_sha256"],
            f"profile_page_{page_index}_image_content_multiset_sha256_invalid",
        )
        _require_sha256(
            page_object["image_sequence_sha256"],
            f"profile_page_{page_index}_image_sequence_sha256_invalid",
        )
        _require_sha256(
            page_object["table_content_multiset_sha256"],
            f"profile_page_{page_index}_table_content_multiset_sha256_invalid",
        )
        _require_sha256(
            page_object["table_sequence_sha256"],
            f"profile_page_{page_index}_table_sequence_sha256_invalid",
        )
        image_occurrences = _validate_image_occurrence_inventory(
            page_object["image_occurrences"],
            page_number=page_number,
            page_index=page_index,
        )
        image_summary = _summarize_validated_content_image_occurrences(
            image_occurrences
        )
        if counts["expected_image_count"] != image_summary["expected_image_count"]:
            _fail(f"profile_page_{page_index}_expected_image_count_mismatch")
        if (
            page_object["image_content_multiset_sha256"]
            != image_summary["image_content_multiset_sha256"]
        ):
            _fail(f"profile_page_{page_index}_image_content_multiset_mismatch")
        if (
            page_object["image_sequence_sha256"]
            != image_summary["image_sequence_sha256"]
        ):
            _fail(f"profile_page_{page_index}_image_sequence_mismatch")
        table_occurrences = _validate_table_occurrence_inventory(
            page_object["table_occurrences"],
            page_number=page_number,
            page_index=page_index,
        )
        table_summary = _summarize_validated_content_table_occurrences(
            table_occurrences
        )
        for count_field in (
            "expected_table_count",
            "expected_table_row_count",
            "expected_table_cell_count",
            "expected_merged_region_count",
        ):
            if counts[count_field] != table_summary[count_field]:
                _fail(f"profile_page_{page_index}_{count_field}_mismatch")
        if (
            page_object["table_content_multiset_sha256"]
            != table_summary["table_content_multiset_sha256"]
        ):
            _fail(f"profile_page_{page_index}_table_content_multiset_mismatch")
        if (
            page_object["table_sequence_sha256"]
            != table_summary["table_sequence_sha256"]
        ):
            _fail(f"profile_page_{page_index}_table_sequence_mismatch")
    if observed_page_numbers != expected_page_numbers:
        _fail("profile_pages_not_complete_and_ordered")
    return _json_clone(profile_object)


def _validate_image_occurrence_inventory(
    occurrences: Any,
    *,
    page_number: int,
    page_index: int,
) -> list[dict[str, Any]]:
    occurrence_values = _require_list(
        occurrences,
        f"profile_page_{page_index}_image_occurrences_not_array",
    )
    validated: list[dict[str, Any]] = []
    seen_occurrence_ids: set[str] = set()
    seen_locators: set[bytes] = set()
    previous_source_order = -1
    for occurrence_index, occurrence in enumerate(occurrence_values):
        prefix = f"profile_page_{page_index}_image_occurrence_{occurrence_index}"
        item = _validate_image_occurrence(
            occurrence,
            expected_page_number=page_number,
            prefix=prefix,
        )
        source_order = item["source_order"]
        if source_order <= previous_source_order:
            _fail(f"profile_page_{page_index}_image_occurrences_not_ordered")
        previous_source_order = source_order
        occurrence_id = item["occurrence_sha256"]
        if occurrence_id in seen_occurrence_ids:
            _fail(f"profile_page_{page_index}_duplicate_image_occurrence")
        seen_occurrence_ids.add(occurrence_id)
        locator_key = canonical_json_bytes(item["native_locator"])
        if locator_key in seen_locators:
            _fail(f"profile_page_{page_index}_duplicate_image_locator")
        seen_locators.add(locator_key)
        validated.append(item)
    return validated


def _validate_image_occurrence(
    occurrence: Any,
    *,
    expected_page_number: int | None,
    prefix: str,
) -> dict[str, Any]:
    occurrence_object = _require_object(occurrence, f"{prefix}_not_object")
    _require_exact_fields(
        occurrence_object,
        _IMAGE_OCCURRENCE_FIELDS,
        set(),
        prefix,
    )
    identity_payload = {
        field: occurrence_object[field]
        for field in _IMAGE_OCCURRENCE_IDENTITY_FIELDS
    }
    identity = _validate_image_occurrence_identity(
        identity_payload,
        prefix=f"{prefix}_identity",
    )
    if (
        expected_page_number is not None
        and identity["native_locator"]["page_number"] != expected_page_number
    ):
        _fail(f"{prefix}_page_number_mismatch")
    occurrence_sha256 = _require_sha256(
        occurrence_object["occurrence_sha256"],
        f"{prefix}_occurrence_sha256_invalid",
    )
    if occurrence_sha256 != canonical_json_sha256(identity):
        _fail(f"{prefix}_occurrence_sha256_mismatch")
    disposition = occurrence_object["disposition"]
    if disposition not in IMAGE_DISPOSITIONS:
        _fail(f"{prefix}_disposition_invalid")
    return {
        "occurrence_sha256": occurrence_sha256,
        **identity,
        "disposition": disposition,
    }


def _validate_image_occurrence_identity(
    identity_payload: Any,
    *,
    prefix: str,
) -> dict[str, Any]:
    identity = _require_object(identity_payload, f"{prefix}_not_object")
    _require_exact_fields(
        identity,
        _IMAGE_OCCURRENCE_IDENTITY_FIELDS,
        set(),
        prefix,
    )
    source_order = _require_nonnegative_int(
        identity["source_order"],
        f"{prefix}_source_order_invalid",
    )
    node_kind = identity["node_kind"]
    if node_kind not in IMAGE_NODE_KINDS:
        _fail(f"{prefix}_node_kind_invalid")
    asset_kind = identity["asset_kind"]
    if asset_kind not in IMAGE_ASSET_KINDS:
        _fail(f"{prefix}_asset_kind_invalid")
    media_type = _require_media_type(
        identity["media_type"],
        f"{prefix}_media_type_invalid",
    )
    content_sha256 = _require_sha256(
        identity["content_sha256"],
        f"{prefix}_content_sha256_invalid",
    )
    locator = _validate_image_locator(
        identity["native_locator"],
        prefix=f"{prefix}_native_locator",
    )
    derivation = _validate_image_derivation(
        identity["derivation"],
        prefix=f"{prefix}_derivation",
    )
    return {
        "source_order": source_order,
        "node_kind": node_kind,
        "asset_kind": asset_kind,
        "media_type": media_type,
        "content_sha256": content_sha256,
        "native_locator": locator,
        "derivation": derivation,
    }


def _validate_image_locator(locator: Any, *, prefix: str) -> dict[str, Any]:
    locator_object = _require_object(locator, f"{prefix}_not_object")
    _require_exact_fields(
        locator_object,
        _IMAGE_LOCATOR_FIELDS,
        set(),
        prefix,
    )
    if locator_object["format"] != "pdf":
        _fail(f"{prefix}_format_invalid")
    page_number = _require_nonnegative_int(
        locator_object["page_number"],
        f"{prefix}_page_number_invalid",
        minimum=1,
    )
    block_index = locator_object["block_index"]
    if block_index is not None:
        block_index = _require_nonnegative_int(
            block_index,
            f"{prefix}_block_index_invalid",
        )
    bbox_values = _require_list(locator_object["bbox"], f"{prefix}_bbox_not_array")
    if len(bbox_values) != 4:
        _fail(f"{prefix}_bbox_invalid")
    bbox = [
        _require_finite_number(value, f"{prefix}_bbox_invalid")
        for value in bbox_values
    ]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        _fail(f"{prefix}_bbox_invalid")
    return {
        "format": "pdf",
        "page_number": page_number,
        "block_index": block_index,
        "bbox": bbox,
    }


def _validate_image_derivation(
    derivation: Any,
    *,
    prefix: str,
) -> dict[str, str] | None:
    if derivation is None:
        return None
    derivation_object = _require_object(derivation, f"{prefix}_not_object")
    _require_exact_fields(
        derivation_object,
        _IMAGE_DERIVATION_FIELDS,
        set(),
        prefix,
    )
    if derivation_object["kind"] != "alpha_mask_composite":
        _fail(f"{prefix}_kind_invalid")
    return {
        "kind": "alpha_mask_composite",
        "base_content_sha256": _require_sha256(
            derivation_object["base_content_sha256"],
            f"{prefix}_base_content_sha256_invalid",
        ),
        "mask_content_sha256": _require_sha256(
            derivation_object["mask_content_sha256"],
            f"{prefix}_mask_content_sha256_invalid",
        ),
    }


def _validate_table_occurrence_inventory(
    occurrences: Any,
    *,
    page_number: int,
    page_index: int,
) -> list[dict[str, Any]]:
    occurrence_values = _require_list(
        occurrences,
        f"profile_page_{page_index}_table_occurrences_not_array",
    )
    validated: list[dict[str, Any]] = []
    seen_occurrence_ids: set[str] = set()
    seen_provider_object_ids: set[str] = set()
    seen_locators: set[bytes] = set()
    previous_provider_index = -1
    for occurrence_index, occurrence in enumerate(occurrence_values):
        prefix = f"profile_page_{page_index}_table_occurrence_{occurrence_index}"
        item = _validate_table_occurrence(
            occurrence,
            expected_page_number=page_number,
            prefix=prefix,
        )
        provider_index = item["provider_index"]
        if provider_index <= previous_provider_index:
            _fail(f"profile_page_{page_index}_table_occurrences_not_ordered")
        previous_provider_index = provider_index
        occurrence_id = item["occurrence_sha256"]
        if occurrence_id in seen_occurrence_ids:
            _fail(f"profile_page_{page_index}_duplicate_table_occurrence")
        seen_occurrence_ids.add(occurrence_id)
        provider_object_id = item["provider_object_id_sha256"]
        if provider_object_id in seen_provider_object_ids:
            _fail(f"profile_page_{page_index}_duplicate_table_provider_object")
        seen_provider_object_ids.add(provider_object_id)
        locator_key = canonical_json_bytes(
            {
                "page_number": item["page_number"],
                "bbox": item["bbox"],
            }
        )
        if locator_key in seen_locators:
            _fail(f"profile_page_{page_index}_duplicate_table_locator")
        seen_locators.add(locator_key)
        validated.append(item)
    return validated


def _validate_table_occurrence(
    occurrence: Any,
    *,
    expected_page_number: int | None,
    prefix: str,
) -> dict[str, Any]:
    occurrence_object = _require_object(occurrence, f"{prefix}_not_object")
    _require_exact_fields(
        occurrence_object,
        _TABLE_OCCURRENCE_FIELDS,
        set(),
        prefix,
    )
    identity_payload = {
        field: occurrence_object[field]
        for field in _TABLE_OCCURRENCE_IDENTITY_FIELDS
    }
    identity = _validate_table_occurrence_identity(
        identity_payload,
        prefix=f"{prefix}_identity",
    )
    if (
        expected_page_number is not None
        and identity["page_number"] != expected_page_number
    ):
        _fail(f"{prefix}_page_number_mismatch")
    occurrence_sha256 = _require_sha256(
        occurrence_object["occurrence_sha256"],
        f"{prefix}_occurrence_sha256_invalid",
    )
    if occurrence_sha256 != canonical_json_sha256(identity):
        _fail(f"{prefix}_occurrence_sha256_mismatch")
    disposition = occurrence_object["disposition"]
    if disposition not in TABLE_DISPOSITIONS:
        _fail(f"{prefix}_disposition_invalid")
    return {
        "occurrence_sha256": occurrence_sha256,
        **identity,
        "disposition": disposition,
    }


def _validate_table_occurrence_identity(
    identity_payload: Any,
    *,
    prefix: str,
) -> dict[str, Any]:
    identity = _require_object(identity_payload, f"{prefix}_not_object")
    _require_exact_fields(
        identity,
        _TABLE_OCCURRENCE_IDENTITY_FIELDS,
        set(),
        prefix,
    )
    provider_object_id_sha256 = _require_sha256(
        identity["provider_object_id_sha256"],
        f"{prefix}_provider_object_id_sha256_invalid",
    )
    provider_index = _require_nonnegative_int(
        identity["provider_index"],
        f"{prefix}_provider_index_invalid",
    )
    page_number = _require_nonnegative_int(
        identity["page_number"],
        f"{prefix}_page_number_invalid",
        minimum=1,
    )
    bbox_values = _require_list(identity["bbox"], f"{prefix}_bbox_not_array")
    if len(bbox_values) != 4:
        _fail(f"{prefix}_bbox_invalid")
    bbox = [
        _require_finite_number(value, f"{prefix}_bbox_invalid")
        for value in bbox_values
    ]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        _fail(f"{prefix}_bbox_invalid")
    row_count = _require_nonnegative_int(
        identity["row_count"],
        f"{prefix}_row_count_invalid",
        minimum=1,
    )
    column_count = _require_nonnegative_int(
        identity["column_count"],
        f"{prefix}_column_count_invalid",
        minimum=1,
    )
    cell_count = _require_nonnegative_int(
        identity["cell_count"],
        f"{prefix}_cell_count_invalid",
        minimum=1,
    )
    merged_region_count = _require_nonnegative_int(
        identity["merged_region_count"],
        f"{prefix}_merged_region_count_invalid",
    )
    if cell_count > row_count * column_count:
        _fail(f"{prefix}_table_counts_inconsistent")
    if merged_region_count > cell_count:
        _fail(f"{prefix}_table_counts_inconsistent")
    topology_sha256 = _require_sha256(
        identity["topology_sha256"],
        f"{prefix}_topology_sha256_invalid",
    )
    return {
        "provider_object_id_sha256": provider_object_id_sha256,
        "provider_index": provider_index,
        "page_number": page_number,
        "bbox": bbox,
        "row_count": row_count,
        "column_count": column_count,
        "cell_count": cell_count,
        "merged_region_count": merged_region_count,
        "topology_sha256": topology_sha256,
    }


def _table_content_projection_item(
    occurrence: Mapping[str, Any],
) -> dict[str, int | str]:
    return {
        "row_count": int(occurrence["row_count"]),
        "column_count": int(occurrence["column_count"]),
        "cell_count": int(occurrence["cell_count"]),
        "merged_region_count": int(occurrence["merged_region_count"]),
        "topology_sha256": str(occurrence["topology_sha256"]),
    }


def _summarize_validated_content_table_occurrences(
    occurrences: list[Mapping[str, Any]],
) -> dict[str, int | str]:
    projection = [
        _table_content_projection_item(occurrence)
        for occurrence in occurrences
        if occurrence["disposition"] == "CONTENT"
    ]
    return {
        "expected_table_count": len(projection),
        "expected_table_row_count": sum(
            int(table["row_count"]) for table in projection
        ),
        "expected_table_cell_count": sum(
            int(table["cell_count"]) for table in projection
        ),
        "expected_merged_region_count": sum(
            int(table["merged_region_count"]) for table in projection
        ),
        "table_content_multiset_sha256": canonical_json_sha256(
            sorted(projection, key=canonical_json_bytes)
        ),
        "table_sequence_sha256": canonical_json_sha256(projection),
    }


def _summarize_validated_content_image_occurrences(
    occurrences: list[Mapping[str, Any]],
) -> dict[str, int | str]:
    projection = [
        {
            "node_kind": occurrence["node_kind"],
            "asset_kind": occurrence["asset_kind"],
            "media_type": occurrence["media_type"],
            "content_sha256": occurrence["content_sha256"],
        }
        for occurrence in occurrences
        if occurrence["disposition"] == "CONTENT"
    ]
    return {
        "expected_image_count": len(projection),
        "image_content_multiset_sha256": canonical_json_sha256(
            sorted(projection, key=canonical_json_bytes)
        ),
        "image_sequence_sha256": canonical_json_sha256(projection),
    }


def _require_resolved_image_occurrences(profile: Mapping[str, Any]) -> None:
    if any(
        occurrence["disposition"] == "UNRESOLVED"
        for page in profile["pages"]
        for occurrence in page["image_occurrences"]
    ):
        _fail("approval_contains_unresolved_image_occurrence")


def _require_resolved_table_occurrences(profile: Mapping[str, Any]) -> None:
    if any(
        occurrence["disposition"] == "UNRESOLVED"
        for page in profile["pages"]
        for occurrence in page["table_occurrences"]
    ):
        _fail("approval_contains_unresolved_table_occurrence")


def _ready_page(page: Mapping[str, Any]) -> dict[str, Any]:
    external_review_fields = {
        "image_occurrences",
        "image_content_multiset_sha256",
        "image_sequence_sha256",
        "table_occurrences",
        "table_content_multiset_sha256",
        "table_sequence_sha256",
    }
    return _json_clone(
        {
            field: value
            for field, value in page.items()
            if field not in external_review_fields
        }
    )


def _binding_from_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    binding = {
        "assertion_id": profile["assertion_id"],
        "evaluator_id": profile["evaluator_id"],
        "document_id": profile["document_id"],
        "source_sha256": profile["source_sha256"],
        "body_page_start": profile["body_page_start"],
        "body_page_end": profile["body_page_end"],
        "review_mode": profile["review_mode"],
    }
    if profile["review_mode"] == "REMOTE_VISUAL":
        binding["approved_visual_provider"] = profile["approved_visual_provider"]
        binding["visual_provider_id"] = profile["visual_provider_id"]
        binding["visual_text_provider_version_sha256"] = profile[
            "visual_text_provider_version_sha256"
        ]
        binding["visual_table_provider_version_sha256"] = profile[
            "visual_table_provider_version_sha256"
        ]
    return binding


def _validate_binding(binding: Any, prefix: str) -> dict[str, Any]:
    binding_object = _require_object(binding, f"{prefix}_not_object")
    _require_exact_fields(
        binding_object,
        _BINDING_REQUIRED_FIELDS,
        _BINDING_OPTIONAL_FIELDS,
        prefix,
    )
    assertion_id = _require_string(
        binding_object["assertion_id"], f"{prefix}_assertion_id_invalid"
    )
    if _ASSERTION_ID_PATTERN.fullmatch(assertion_id) is None:
        _fail(f"{prefix}_assertion_id_invalid")
    if binding_object["evaluator_id"] != PDF_BODY_REVIEW_EVALUATOR_ID:
        _fail(f"{prefix}_evaluator_id_invalid")
    document_id = _require_string(
        binding_object["document_id"], f"{prefix}_document_id_invalid"
    )
    if _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None:
        _fail(f"{prefix}_document_id_invalid")
    _require_sha256(
        binding_object["source_sha256"], f"{prefix}_source_sha256_invalid"
    )
    page_start = _require_nonnegative_int(
        binding_object["body_page_start"],
        f"{prefix}_body_page_start_invalid",
        minimum=1,
    )
    page_end = _require_nonnegative_int(
        binding_object["body_page_end"],
        f"{prefix}_body_page_end_invalid",
        minimum=1,
    )
    if page_start > page_end:
        _fail(f"{prefix}_page_range_invalid")
    review_mode = binding_object["review_mode"]
    if review_mode not in ALLOWED_REVIEW_MODES:
        _fail(f"{prefix}_review_mode_invalid")
    remote_fields = {
        "approved_visual_provider",
        "visual_provider_id",
        "visual_text_provider_version_sha256",
        "visual_table_provider_version_sha256",
    }
    present_remote_fields = remote_fields.intersection(binding_object)
    if review_mode == "NATIVE" and present_remote_fields:
        _fail(f"{prefix}_forbids_visual_provider")
    if review_mode == "REMOTE_VISUAL":
        if present_remote_fields != remote_fields:
            _fail(f"{prefix}_requires_visual_provenance")
        if binding_object["approved_visual_provider"] not in ALLOWED_VISUAL_PROVIDERS:
            _fail(f"{prefix}_visual_provider_invalid")
        expected_provider_id = (
            "aliyun-bailian-qwen-ocr"
            if binding_object["approved_visual_provider"] == "bailian"
            else "azure-document-intelligence"
        )
        if binding_object["visual_provider_id"] != expected_provider_id:
            _fail(f"{prefix}_visual_provider_id_mismatch")
        _require_sha256(
            binding_object["visual_text_provider_version_sha256"],
            f"{prefix}_visual_text_provider_version_sha256_invalid",
        )
        _require_sha256(
            binding_object["visual_table_provider_version_sha256"],
            f"{prefix}_visual_table_provider_version_sha256_invalid",
        )
    return _json_clone(binding_object)


def _validate_trusted_reviewer_ids(
    trusted_reviewer_ids: Collection[str],
) -> set[str]:
    if isinstance(trusted_reviewer_ids, (str, bytes)):
        _fail("trusted_reviewer_registry_invalid")
    try:
        trusted = set(trusted_reviewer_ids)
    except TypeError as exc:
        raise PdfBodyReviewContractError("trusted_reviewer_registry_invalid") from exc
    if not trusted:
        _fail("trusted_reviewer_registry_empty")
    for reviewer_id in trusted:
        _require_sha256(reviewer_id, "trusted_reviewer_id_invalid")
    return trusted


def _validate_review_tool(review_tool: Any, review_index: int) -> None:
    tool = _require_object(
        review_tool, f"approval_review_{review_index}_review_tool_not_object"
    )
    _require_exact_fields(
        tool,
        {"tool_id", "tool_version"},
        set(),
        f"approval_review_{review_index}_review_tool",
    )
    _require_safe_identifier(
        tool["tool_id"], f"approval_review_{review_index}_review_tool_id_invalid"
    )
    _require_safe_identifier(
        tool["tool_version"],
        f"approval_review_{review_index}_review_tool_version_invalid",
    )


def _validate_page_approvals(
    page_approvals: Any,
    *,
    expected_pages: list[int],
    review_index: int,
) -> None:
    pages = _require_list(
        page_approvals, f"approval_review_{review_index}_page_approvals_not_array"
    )
    observed_pages: list[int] = []
    for page_index, page in enumerate(pages):
        page_object = _require_object(
            page,
            f"approval_review_{review_index}_page_approval_{page_index}_not_object",
        )
        _require_exact_fields(
            page_object,
            {"page_number", "channels"},
            set(),
            f"approval_review_{review_index}_page_approval_{page_index}",
        )
        observed_pages.append(
            _require_nonnegative_int(
                page_object["page_number"],
                f"approval_review_{review_index}_page_number_invalid",
                minimum=1,
            )
        )
        channels = _require_object(
            page_object["channels"],
            f"approval_review_{review_index}_channels_not_object",
        )
        _require_exact_fields(
            channels,
            {"text", "image", "table"},
            set(),
            f"approval_review_{review_index}_channels",
        )
        if any(
            channels[channel] != "APPROVED"
            for channel in ("text", "image", "table")
        ):
            _fail("approval_page_channel_not_approved")
    if observed_pages != expected_pages:
        _fail("approval_pages_not_complete_and_ordered")


def _require_exact_fields(
    value: Mapping[str, Any],
    required: set[str],
    optional: set[str],
    prefix: str,
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    if missing:
        _fail(f"{prefix}_missing_field:{missing[0]}")
    unknown = sorted(keys - required - optional)
    if unknown:
        _fail(f"{prefix}_unknown_field:{unknown[0]}")


def _require_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _require_list(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(code)
    return value


def _require_string(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(code)
    return value


def _require_sha256(value: Any, code: str) -> str:
    candidate = _require_string(value, code)
    if _SHA256_PATTERN.fullmatch(candidate) is None:
        _fail(code)
    return candidate


def _require_safe_identifier(value: Any, code: str) -> str:
    candidate = _require_string(value, code)
    if len(candidate) > 128 or _SAFE_IDENTIFIER_PATTERN.fullmatch(candidate) is None:
        _fail(code)
    return candidate


def _require_media_type(value: Any, code: str) -> str:
    candidate = _require_string(value, code)
    if len(candidate) > 160 or _MEDIA_TYPE_PATTERN.fullmatch(candidate) is None:
        _fail(code)
    return candidate


def _require_finite_number(value: Any, code: str) -> float:
    if type(value) not in {int, float}:
        _fail(code)
    candidate = float(value)
    if not math.isfinite(candidate):
        _fail(code)
    return 0.0 if candidate == 0.0 else candidate


def _require_nonnegative_int(value: Any, code: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(code)
    return value


def _require_rfc3339_utc(value: Any, code: str) -> str:
    candidate = _require_string(value, code)
    if _RFC3339_UTC_PATTERN.fullmatch(candidate) is None:
        _fail(code)
    try:
        parsed = datetime.fromisoformat(candidate[:-1] + "+00:00")
    except ValueError as exc:
        raise PdfBodyReviewContractError(code) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _fail(code)
    return candidate


def _json_clone(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _fail(code: str) -> None:
    raise PdfBodyReviewContractError(code)
