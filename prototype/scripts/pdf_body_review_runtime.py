"""Fail-closed runtime binding for controlled PDF body review artifacts.

The public review contract deliberately performs no I/O.  This module adds the
small runtime boundary needed by the extraction-case runner: content-addressed
artifact loading, strict external trust-policy loading, and exact materialized
READY-profile verification.  It never creates review or approval artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.pdf_body_review_artifacts import (
    PDF_BODY_REVIEW_EVALUATOR_ID,
    PdfBodyReviewContractError,
    canonical_json_bytes,
    finalize_pdf_body_review_artifacts,
    validate_pdf_body_review_draft,
)


TRUST_POLICY_SCHEMA_VERSION = "pdf_body_review_runtime_trust.v1"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TRUST_POLICY_BYTES = 256 * 1024
MAX_TRUSTED_REVIEWERS = 1024

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]*$")
_REMOTE_PROVIDER_IDS = {
    "bailian": "aliyun-bailian-qwen-ocr",
    "azure-document-intelligence": "azure-document-intelligence",
}

PdfBodyReviewArtifactResolver = Callable[[str], bytes]


class PdfBodyReviewRuntimeError(ValueError):
    """Fail-closed runtime error with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PdfBodyReviewTrustPolicy:
    """Externally governed reviewer trust and policy requirements."""

    expected_review_policy_version: str
    trusted_reviewer_ids: frozenset[str]
    minimum_human_reviewers: int = 1

    def __post_init__(self) -> None:
        policy_version = self.expected_review_policy_version
        if (
            not isinstance(policy_version, str)
            or len(policy_version) > 128
            or _SAFE_IDENTIFIER_PATTERN.fullmatch(policy_version) is None
        ):
            _fail("pdf_body_review_trust_policy_version_invalid")
        try:
            reviewer_ids = frozenset(self.trusted_reviewer_ids)
        except TypeError as exc:
            raise PdfBodyReviewRuntimeError(
                "pdf_body_review_trusted_reviewer_registry_invalid"
            ) from exc
        if not reviewer_ids:
            _fail("pdf_body_review_trusted_reviewer_registry_empty")
        if any(
            not isinstance(reviewer_id, str)
            or _SHA256_PATTERN.fullmatch(reviewer_id) is None
            for reviewer_id in reviewer_ids
        ):
            _fail("pdf_body_review_trusted_reviewer_id_invalid")
        if (
            type(self.minimum_human_reviewers) is not int
            or self.minimum_human_reviewers < 1
        ):
            _fail("pdf_body_review_minimum_human_reviewers_invalid")
        object.__setattr__(self, "trusted_reviewer_ids", reviewer_ids)


@dataclass(frozen=True)
class VerifiedPdfBodyReviewProfile:
    """A manifest READY profile bound to one validated external approval."""

    ready_profile: dict[str, Any]
    reviewed_profile: dict[str, Any]


@dataclass(frozen=True)
class ContentAddressedPdfBodyReviewResolver:
    """Read canonical JSON artifacts from an explicitly supplied directory."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    def __call__(self, artifact_sha256: str) -> bytes:
        digest = _require_sha256(
            artifact_sha256,
            "pdf_body_review_artifact_sha256_invalid",
        )
        try:
            root_metadata = self.root.stat(follow_symlinks=False)
        except OSError as exc:
            raise PdfBodyReviewRuntimeError(
                "pdf_body_review_artifact_unavailable"
            ) from exc
        if not stat.S_ISDIR(root_metadata.st_mode):
            _fail("pdf_body_review_artifact_unavailable")
        return _read_bounded_regular_file(
            self.root / f"{digest}.json",
            maximum_bytes=MAX_ARTIFACT_BYTES,
            unavailable_code="pdf_body_review_artifact_unavailable",
            oversized_code="pdf_body_review_artifact_too_large",
        )


def load_pdf_body_review_trust_policy(path: Path) -> PdfBodyReviewTrustPolicy:
    """Load a strict, text-free trust policy from controlled external storage."""

    try:
        raw = _read_bounded_regular_file(
            Path(path),
            maximum_bytes=MAX_TRUST_POLICY_BYTES,
            unavailable_code="pdf_body_review_trust_policy_unavailable_or_invalid",
            oversized_code="pdf_body_review_trust_policy_too_large",
        )
        payload = json.loads(raw.decode("utf-8"))
    except PdfBodyReviewRuntimeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PdfBodyReviewRuntimeError(
            "pdf_body_review_trust_policy_unavailable_or_invalid"
        ) from exc
    if not isinstance(payload, dict):
        _fail("pdf_body_review_trust_policy_not_object")
    expected_fields = {
        "schema_version",
        "review_policy_version",
        "minimum_human_reviewers",
        "trusted_reviewer_ids",
    }
    if set(payload) != expected_fields:
        _fail("pdf_body_review_trust_policy_fields_invalid")
    if payload["schema_version"] != TRUST_POLICY_SCHEMA_VERSION:
        _fail("pdf_body_review_trust_policy_schema_version_invalid")
    reviewer_values = payload["trusted_reviewer_ids"]
    if (
        not isinstance(reviewer_values, list)
        or len(reviewer_values) > MAX_TRUSTED_REVIEWERS
    ):
        _fail("pdf_body_review_trusted_reviewer_registry_invalid")
    try:
        unique_reviewer_values = set(reviewer_values)
    except TypeError as exc:
        raise PdfBodyReviewRuntimeError(
            "pdf_body_review_trusted_reviewer_registry_invalid"
        ) from exc
    if len(reviewer_values) != len(unique_reviewer_values):
        _fail("pdf_body_review_trusted_reviewer_registry_invalid")
    return PdfBodyReviewTrustPolicy(
        expected_review_policy_version=payload["review_policy_version"],
        trusted_reviewer_ids=frozenset(reviewer_values),
        minimum_human_reviewers=payload["minimum_human_reviewers"],
    )


def resolve_selected_pdf_body_review_profiles(
    *,
    manifest: Mapping[str, Any],
    selected_case_ids: Iterable[str],
    artifact_resolver: PdfBodyReviewArtifactResolver | None,
    trust_policy: PdfBodyReviewTrustPolicy | None,
    evaluator_id: str = PDF_BODY_REVIEW_EVALUATOR_ID,
) -> dict[tuple[str, str], VerifiedPdfBodyReviewProfile]:
    """Resolve READY profiles selected for this run; leave PENDING untouched."""

    verified: dict[tuple[str, str], VerifiedPdfBodyReviewProfile] = {}
    documents = manifest["documents"]
    cases = manifest["cases"]
    for case_id in selected_case_ids:
        case = cases[case_id]
        for assertion in case["assertions"]:
            evaluation = assertion.get("evaluation")
            if (
                not isinstance(evaluation, dict)
                or evaluation.get("evaluator_id") != evaluator_id
            ):
                continue
            assertion_id = str(assertion["assertion_id"])
            profiles = evaluation["reviewed_body_profiles_by_document"]
            for document_id in evaluation["applicable_document_ids"]:
                manifest_profile = profiles[document_id]
                if manifest_profile.get("status") == "PENDING":
                    continue
                if artifact_resolver is None:
                    _fail("pdf_body_review_artifact_resolver_required")
                if trust_policy is None:
                    _fail("pdf_body_review_trust_policy_required")
                verified[(assertion_id, document_id)] = (
                    resolve_verified_pdf_body_review_profile(
                        assertion_id=assertion_id,
                        evaluator_id=evaluator_id,
                        document_id=document_id,
                        registered_source_sha256=documents[document_id][
                            "source_sha256"
                        ],
                        manifest_profile=manifest_profile,
                        artifact_resolver=artifact_resolver,
                        trust_policy=trust_policy,
                    )
                )
    return verified


def resolve_verified_pdf_body_review_profile(
    *,
    assertion_id: str,
    evaluator_id: str,
    document_id: str,
    registered_source_sha256: str,
    manifest_profile: Mapping[str, Any],
    artifact_resolver: PdfBodyReviewArtifactResolver,
    trust_policy: PdfBodyReviewTrustPolicy,
) -> VerifiedPdfBodyReviewProfile:
    """Resolve and validate the approval/draft chain for one READY profile."""

    if manifest_profile.get("status") != "READY":
        _fail("pdf_body_review_runtime_profile_not_ready")
    artifact_sha256 = _require_sha256(
        manifest_profile.get("review_artifact_sha256"),
        "pdf_body_review_artifact_sha256_invalid",
    )
    approval = _resolve_canonical_artifact(
        artifact_resolver,
        artifact_sha256,
    )
    draft_sha256 = _require_sha256(
        approval.get("draft_sha256"),
        "pdf_body_review_approval_draft_sha256_invalid",
    )
    draft = _resolve_canonical_artifact(artifact_resolver, draft_sha256)
    try:
        validated_draft = validate_pdf_body_review_draft(draft)
    except PdfBodyReviewContractError as exc:
        raise PdfBodyReviewRuntimeError(exc.code) from exc

    expected_scope = _expected_scope_from_registration_and_approval(
        assertion_id=assertion_id,
        evaluator_id=evaluator_id,
        document_id=document_id,
        registered_source_sha256=registered_source_sha256,
        manifest_profile=manifest_profile,
        approval=approval,
    )
    try:
        materialized_ready = finalize_pdf_body_review_artifacts(
            draft=validated_draft,
            approval=approval,
            expected_scope=expected_scope,
            trusted_reviewer_ids=trust_policy.trusted_reviewer_ids,
            expected_review_policy_version=(
                trust_policy.expected_review_policy_version
            ),
            minimum_human_reviewers=trust_policy.minimum_human_reviewers,
        )
    except PdfBodyReviewContractError as exc:
        raise PdfBodyReviewRuntimeError(exc.code) from exc
    try:
        manifest_bytes = canonical_json_bytes(dict(manifest_profile))
    except PdfBodyReviewContractError as exc:
        raise PdfBodyReviewRuntimeError(exc.code) from exc
    if canonical_json_bytes(materialized_ready) != manifest_bytes:
        _fail("pdf_body_review_manifest_ready_profile_mismatch")
    return VerifiedPdfBodyReviewProfile(
        ready_profile=materialized_ready,
        reviewed_profile=validated_draft["profile"],
    )


def _resolve_canonical_artifact(
    resolver: PdfBodyReviewArtifactResolver,
    artifact_sha256: str,
) -> dict[str, Any]:
    try:
        raw = resolver(artifact_sha256)
    except PdfBodyReviewRuntimeError:
        raise
    except Exception as exc:
        raise PdfBodyReviewRuntimeError(
            "pdf_body_review_artifact_unavailable"
        ) from exc
    if not isinstance(raw, bytes):
        _fail("pdf_body_review_artifact_not_bytes")
    if len(raw) > MAX_ARTIFACT_BYTES:
        _fail("pdf_body_review_artifact_too_large")
    if hashlib.sha256(raw).hexdigest() != artifact_sha256:
        _fail("pdf_body_review_artifact_hash_mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PdfBodyReviewRuntimeError(
            "pdf_body_review_artifact_json_invalid"
        ) from exc
    if not isinstance(payload, dict):
        _fail("pdf_body_review_artifact_not_object")
    try:
        canonical = canonical_json_bytes(payload)
    except PdfBodyReviewContractError as exc:
        raise PdfBodyReviewRuntimeError(exc.code) from exc
    if raw != canonical:
        _fail("pdf_body_review_artifact_not_canonical")
    return payload


def _expected_scope_from_registration_and_approval(
    *,
    assertion_id: str,
    evaluator_id: str,
    document_id: str,
    registered_source_sha256: str,
    manifest_profile: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    binding = approval.get("binding")
    if not isinstance(binding, dict):
        _fail("pdf_body_review_approval_binding_invalid")
    base_scope: dict[str, Any] = {
        "assertion_id": assertion_id,
        "evaluator_id": evaluator_id,
        "document_id": document_id,
        "source_sha256": registered_source_sha256,
        "body_page_start": manifest_profile.get("body_page_start"),
        "body_page_end": manifest_profile.get("body_page_end"),
        "review_mode": manifest_profile.get("review_mode"),
    }
    if base_scope["review_mode"] == "REMOTE_VISUAL":
        provider_name = manifest_profile.get("approved_visual_provider")
        provider_id = _REMOTE_PROVIDER_IDS.get(provider_name)
        if provider_id is None:
            _fail("pdf_body_review_registered_visual_provider_invalid")
        base_scope.update(
            {
                "approved_visual_provider": provider_name,
                "visual_provider_id": provider_id,
            }
        )
        text_version_sha256 = binding.get(
            "visual_text_provider_version_sha256"
        )
        table_version_sha256 = binding.get(
            "visual_table_provider_version_sha256"
        )
        _require_sha256(
            text_version_sha256,
            "pdf_body_review_approval_text_provider_version_sha256_invalid",
        )
        _require_sha256(
            table_version_sha256,
            "pdf_body_review_approval_table_provider_version_sha256_invalid",
        )
        expected_scope = {
            **base_scope,
            "visual_text_provider_version_sha256": text_version_sha256,
            "visual_table_provider_version_sha256": table_version_sha256,
        }
    else:
        expected_scope = base_scope
    if binding != expected_scope:
        _fail("pdf_body_review_approval_registered_scope_mismatch")
    return expected_scope


def _require_sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _read_bounded_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    unavailable_code: str,
    oversized_code: str,
) -> bytes:
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(unavailable_code)
        if metadata.st_size > maximum_bytes:
            _fail(oversized_code)
        with path.open("rb") as handle:
            payload = handle.read(maximum_bytes + 1)
    except PdfBodyReviewRuntimeError:
        raise
    except OSError as exc:
        raise PdfBodyReviewRuntimeError(unavailable_code) from exc
    if len(payload) > maximum_bytes:
        _fail(oversized_code)
    return payload


def _fail(code: str) -> None:
    raise PdfBodyReviewRuntimeError(code)


__all__ = [
    "ContentAddressedPdfBodyReviewResolver",
    "MAX_ARTIFACT_BYTES",
    "MAX_TRUSTED_REVIEWERS",
    "MAX_TRUST_POLICY_BYTES",
    "PdfBodyReviewArtifactResolver",
    "PdfBodyReviewRuntimeError",
    "PdfBodyReviewTrustPolicy",
    "TRUST_POLICY_SCHEMA_VERSION",
    "VerifiedPdfBodyReviewProfile",
    "load_pdf_body_review_trust_policy",
    "resolve_selected_pdf_body_review_profiles",
    "resolve_verified_pdf_body_review_profile",
]
