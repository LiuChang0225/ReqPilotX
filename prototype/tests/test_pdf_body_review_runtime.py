from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.pdf_body_review_artifacts import (
    APPROVAL_SCHEMA_VERSION,
    CONTROLLED_EXTERNAL,
    HMAC_ID_SCHEME,
    HUMAN_REVIEWER_ROLE,
    PDF_BODY_REVIEW_EVALUATOR_ID,
    PROFILE_SCHEMA_VERSION,
    REVIEW_BASIS,
    canonical_json_bytes,
    canonical_json_sha256,
    finalize_pdf_body_review_artifacts,
    prepare_pdf_body_review_draft,
    summarize_pdf_content_image_occurrences,
    summarize_pdf_content_table_occurrences,
)
from scripts.pdf_body_review_runtime import (
    MAX_ARTIFACT_BYTES,
    MAX_TRUST_POLICY_BYTES,
    TRUST_POLICY_SCHEMA_VERSION,
    ContentAddressedPdfBodyReviewResolver,
    PdfBodyReviewRuntimeError,
    PdfBodyReviewTrustPolicy,
    load_pdf_body_review_trust_policy,
    resolve_selected_pdf_body_review_profiles,
)


ASSERTION_ID = "FMT-005-A4"
CASE_ID = "FMT-005"
DOCUMENT_ID = "DOC-SYNTHETIC-PDF"
SOURCE_SHA256 = "a" * 64
PAGE_IMAGE_SHA256 = "b" * 64
PROVIDER_VERSION_SHA256 = "c" * 64
TABLE_PROVIDER_VERSION_SHA256 = "3" * 64
REVIEWER_ID = "d" * 64
SECOND_REVIEWER_ID = "e" * 64
POLICY_VERSION = "pdf-body-review-policy.v1"


class PdfBodyReviewRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = _reviewed_profile("NATIVE")
        self.draft = prepare_pdf_body_review_draft(self.profile)
        self.approval = _approval(self.draft)
        self.policy = PdfBodyReviewTrustPolicy(
            expected_review_policy_version=POLICY_VERSION,
            trusted_reviewer_ids=frozenset({REVIEWER_ID}),
        )
        self.ready = finalize_pdf_body_review_artifacts(
            draft=self.draft,
            approval=self.approval,
            expected_scope=_binding(self.profile),
            trusted_reviewer_ids=self.policy.trusted_reviewer_ids,
            expected_review_policy_version=POLICY_VERSION,
        )
        self.artifacts = _artifact_store(self.draft, self.approval)

    def assert_runtime_error(self, code: str):
        return self.assertRaisesRegex(
            PdfBodyReviewRuntimeError,
            rf"^{code}$",
        )

    def resolve(
        self,
        *,
        manifest: dict | None = None,
        resolver=None,
        policy: PdfBodyReviewTrustPolicy | None = None,
    ):
        selected_manifest = manifest or _manifest(self.ready)
        selected_resolver = (
            (lambda digest: self.artifacts[digest])
            if resolver is None
            else resolver
        )
        return resolve_selected_pdf_body_review_profiles(
            manifest=selected_manifest,
            selected_case_ids=(CASE_ID,),
            artifact_resolver=selected_resolver,
            trust_policy=self.policy if policy is None else policy,
        )

    def test_pending_profile_does_not_require_external_runtime(self) -> None:
        pending = {
            "status": "PENDING",
            "source_sha256": SOURCE_SHA256,
            "body_page_start": 2,
            "body_page_end": 2,
            "review_mode": "NATIVE",
            "review_approved": False,
        }

        resolved = resolve_selected_pdf_body_review_profiles(
            manifest=_manifest(pending),
            selected_case_ids=(CASE_ID,),
            artifact_resolver=None,
            trust_policy=None,
        )

        self.assertEqual(resolved, {})

    def test_ready_profile_requires_resolver_and_trust_policy(self) -> None:
        with self.assert_runtime_error("pdf_body_review_artifact_resolver_required"):
            resolve_selected_pdf_body_review_profiles(
                manifest=_manifest(self.ready),
                selected_case_ids=(CASE_ID,),
                artifact_resolver=None,
                trust_policy=self.policy,
            )

        with self.assert_runtime_error("pdf_body_review_trust_policy_required"):
            resolve_selected_pdf_body_review_profiles(
                manifest=_manifest(self.ready),
                selected_case_ids=(CASE_ID,),
                artifact_resolver=lambda digest: self.artifacts[digest],
                trust_policy=None,
            )

    def test_valid_chain_materializes_exact_manifest_profile(self) -> None:
        resolved = self.resolve()

        verified = resolved[(ASSERTION_ID, DOCUMENT_ID)]
        self.assertEqual(verified.ready_profile, self.ready)
        self.assertEqual(verified.reviewed_profile, self.profile)

    def test_arbitrary_manifest_hash_cannot_self_certify(self) -> None:
        forged = copy.deepcopy(self.ready)
        forged["review_artifact_sha256"] = "f" * 64

        with self.assert_runtime_error("pdf_body_review_artifact_unavailable"):
            self.resolve(manifest=_manifest(forged))

    def test_artifact_bytes_must_match_address_and_be_canonical(self) -> None:
        with self.assert_runtime_error("pdf_body_review_artifact_hash_mismatch"):
            self.resolve(resolver=lambda _digest: b"{}")

        noncanonical = json.dumps(self.approval, ensure_ascii=False).encode("utf-8")
        noncanonical_sha256 = _sha256(noncanonical)
        profile = copy.deepcopy(self.ready)
        profile["review_artifact_sha256"] = noncanonical_sha256
        artifacts = {**self.artifacts, noncanonical_sha256: noncanonical}
        with self.assert_runtime_error("pdf_body_review_artifact_not_canonical"):
            self.resolve(
                manifest=_manifest(profile),
                resolver=lambda digest: artifacts[digest],
            )

    def test_registered_scope_and_manifest_profile_are_exact(self) -> None:
        changed_approval = copy.deepcopy(self.approval)
        changed_approval["binding"]["source_sha256"] = "f" * 64
        changed_approval_sha256 = canonical_json_sha256(changed_approval)
        scope_changed_ready = copy.deepcopy(self.ready)
        scope_changed_ready["review_artifact_sha256"] = changed_approval_sha256
        changed_artifacts = {
            **self.artifacts,
            changed_approval_sha256: canonical_json_bytes(changed_approval),
        }
        with self.assert_runtime_error(
            "pdf_body_review_approval_registered_scope_mismatch"
        ):
            self.resolve(
                manifest=_manifest(scope_changed_ready),
                resolver=lambda digest: changed_artifacts[digest],
            )

        changed_manifest_ready = copy.deepcopy(self.ready)
        changed_manifest_ready["pages"][0]["expected_body_line_count"] += 1
        with self.assert_runtime_error(
            "pdf_body_review_manifest_ready_profile_mismatch"
        ):
            self.resolve(manifest=_manifest(changed_manifest_ready))

    def test_external_reviewer_registry_and_policy_are_enforced(self) -> None:
        wrong_reviewer_policy = PdfBodyReviewTrustPolicy(
            expected_review_policy_version=POLICY_VERSION,
            trusted_reviewer_ids=frozenset({SECOND_REVIEWER_ID}),
        )
        with self.assert_runtime_error("approval_reviewer_not_trusted"):
            self.resolve(policy=wrong_reviewer_policy)

        wrong_version_policy = PdfBodyReviewTrustPolicy(
            expected_review_policy_version="pdf-body-review-policy.v2",
            trusted_reviewer_ids=frozenset({REVIEWER_ID}),
        )
        with self.assert_runtime_error("approval_review_policy_version_mismatch"):
            self.resolve(policy=wrong_version_policy)

    def test_remote_profile_retains_approved_provider_version(self) -> None:
        profile = _reviewed_profile("REMOTE_VISUAL")
        draft = prepare_pdf_body_review_draft(profile)
        approval = _approval(draft)
        ready = finalize_pdf_body_review_artifacts(
            draft=draft,
            approval=approval,
            expected_scope=_binding(profile),
            trusted_reviewer_ids={REVIEWER_ID},
            expected_review_policy_version=POLICY_VERSION,
        )
        artifacts = _artifact_store(draft, approval)

        resolved = self.resolve(
            manifest=_manifest(ready),
            resolver=lambda digest: artifacts[digest],
        )

        reviewed = resolved[(ASSERTION_ID, DOCUMENT_ID)].reviewed_profile
        self.assertEqual(
            reviewed["visual_text_provider_version_sha256"],
            PROVIDER_VERSION_SHA256,
        )
        self.assertEqual(
            reviewed["visual_table_provider_version_sha256"],
            TABLE_PROVIDER_VERSION_SHA256,
        )

    def test_directory_resolver_and_strict_external_policy_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for digest, payload in self.artifacts.items():
                (root / f"{digest}.json").write_bytes(payload)
            policy_path = root / "trust.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": TRUST_POLICY_SCHEMA_VERSION,
                        "review_policy_version": POLICY_VERSION,
                        "minimum_human_reviewers": 1,
                        "trusted_reviewer_ids": [REVIEWER_ID],
                    }
                ),
                encoding="utf-8",
            )

            resolved = resolve_selected_pdf_body_review_profiles(
                manifest=_manifest(self.ready),
                selected_case_ids=(CASE_ID,),
                artifact_resolver=ContentAddressedPdfBodyReviewResolver(root),
                trust_policy=load_pdf_body_review_trust_policy(policy_path),
            )

            self.assertIn((ASSERTION_ID, DOCUMENT_ID), resolved)

            invalid_policy = root / "invalid-trust.json"
            invalid_policy.write_text(
                json.dumps(
                    {
                        "schema_version": TRUST_POLICY_SCHEMA_VERSION,
                        "review_policy_version": POLICY_VERSION,
                        "minimum_human_reviewers": 1,
                        "trusted_reviewer_ids": [REVIEWER_ID],
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assert_runtime_error(
                "pdf_body_review_trust_policy_fields_invalid"
            ):
                load_pdf_body_review_trust_policy(invalid_policy)

            oversized_policy = root / "oversized-trust.json"
            oversized_policy.write_bytes(b" " * (MAX_TRUST_POLICY_BYTES + 1))
            with self.assert_runtime_error(
                "pdf_body_review_trust_policy_too_large"
            ):
                load_pdf_body_review_trust_policy(oversized_policy)

    def test_artifact_loader_is_bounded_and_rejects_symlinks(self) -> None:
        with self.assert_runtime_error("pdf_body_review_artifact_too_large"):
            self.resolve(resolver=lambda _digest: b"x" * (MAX_ARTIFACT_BYTES + 1))

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            digest = self.ready["review_artifact_sha256"]
            target = root / "approval-target.json"
            target.write_bytes(self.artifacts[digest])
            (root / f"{digest}.json").symlink_to(target)

            with self.assert_runtime_error("pdf_body_review_artifact_unavailable"):
                ContentAddressedPdfBodyReviewResolver(root)(digest)


def _reviewed_profile(review_mode: str) -> dict:
    image_summary = summarize_pdf_content_image_occurrences([])
    table_summary = summarize_pdf_content_table_occurrences([])
    page = {
        "page_number": 2,
        "expected_body_line_count": 2,
        **image_summary,
        "image_occurrences": [],
        **table_summary,
        "table_occurrences": [],
        "content_multiset_sha256": "1" * 64,
        "sequence_sha256": "2" * 64,
    }
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "assertion_id": ASSERTION_ID,
        "evaluator_id": PDF_BODY_REVIEW_EVALUATOR_ID,
        "document_id": DOCUMENT_ID,
        "source_sha256": SOURCE_SHA256,
        "body_page_start": 2,
        "body_page_end": 2,
        "review_mode": review_mode,
        "pages": [page],
    }
    if review_mode == "REMOTE_VISUAL":
        profile.update(
            {
                "approved_visual_provider": "bailian",
                "visual_provider_id": "aliyun-bailian-qwen-ocr",
                "visual_text_provider_version_sha256": (
                    PROVIDER_VERSION_SHA256
                ),
                "visual_table_provider_version_sha256": (
                    TABLE_PROVIDER_VERSION_SHA256
                ),
            }
        )
        page["page_image_sha256"] = PAGE_IMAGE_SHA256
    return profile


def _binding(profile: dict) -> dict:
    binding = {
        field: profile[field]
        for field in (
            "assertion_id",
            "evaluator_id",
            "document_id",
            "source_sha256",
            "body_page_start",
            "body_page_end",
            "review_mode",
        )
    }
    if profile["review_mode"] == "REMOTE_VISUAL":
        binding.update(
            {
                field: profile[field]
                for field in (
                    "approved_visual_provider",
                    "visual_provider_id",
                    "visual_text_provider_version_sha256",
                    "visual_table_provider_version_sha256",
                )
            }
        )
    return binding


def _approval(draft: dict) -> dict:
    profile = draft["profile"]
    return {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "status": "APPROVED",
        "location_class": CONTROLLED_EXTERNAL,
        "draft_sha256": canonical_json_sha256(draft),
        "profile_sha256": draft["profile_sha256"],
        "binding": _binding(profile),
        "review_basis": REVIEW_BASIS,
        "review_policy_version": POLICY_VERSION,
        "reviews": [
            {
                "reviewer_id": REVIEWER_ID,
                "reviewer_id_scheme": HMAC_ID_SCHEME,
                "actor_type": "HUMAN",
                "reviewer_role": HUMAN_REVIEWER_ROLE,
                "independence_attested": True,
                "reviewed_at_utc": "2026-07-23T00:00:00Z",
                "review_tool": {
                    "tool_id": "controlled-pdf-review",
                    "tool_version": "v1",
                },
                "page_approvals": [
                    {
                        "page_number": page["page_number"],
                        "channels": {
                            "text": "APPROVED",
                            "image": "APPROVED",
                            "table": "APPROVED",
                        },
                    }
                    for page in profile["pages"]
                ],
            }
        ],
    }


def _artifact_store(draft: dict, approval: dict) -> dict[str, bytes]:
    return {
        canonical_json_sha256(draft): canonical_json_bytes(draft),
        canonical_json_sha256(approval): canonical_json_bytes(approval),
    }


def _manifest(profile: dict) -> dict:
    return {
        "documents": {DOCUMENT_ID: {"source_sha256": SOURCE_SHA256}},
        "cases": {
            CASE_ID: {
                "assertions": [
                    {
                        "assertion_id": ASSERTION_ID,
                        "evaluation": {
                            "evaluator_id": PDF_BODY_REVIEW_EVALUATOR_ID,
                            "applicable_document_ids": [DOCUMENT_ID],
                            "reviewed_body_profiles_by_document": {
                                DOCUMENT_ID: profile
                            },
                        },
                    }
                ]
            }
        },
    }


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    unittest.main()
