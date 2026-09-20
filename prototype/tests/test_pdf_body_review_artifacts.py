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
import re
import unittest
from pathlib import Path

import jsonschema

from scripts.pdf_body_review_artifacts import (
    APPROVAL_SCHEMA_VERSION,
    CONTROLLED_EXTERNAL,
    DRAFT_SCHEMA_VERSION,
    HMAC_ID_SCHEME,
    HUMAN_REVIEWER_ROLE,
    PDF_BODY_REVIEW_EVALUATOR_ID,
    PROFILE_SCHEMA_VERSION,
    REVIEW_BASIS,
    PdfBodyReviewContractError,
    canonical_pdf_image_occurrence_sha256,
    canonical_pdf_table_occurrence_sha256,
    canonical_json_sha256,
    finalize_pdf_body_review_artifacts,
    pdf_content_image_projection,
    pdf_content_table_projection,
    prepare_pdf_body_review_draft,
    summarize_pdf_content_image_occurrences,
    summarize_pdf_content_table_occurrences,
    validate_pdf_body_review_approval,
    validate_pdf_body_review_draft,
)


SOURCE_SHA256 = "a" * 64
PAGE_IMAGE_SHA256 = "b" * 64
VISUAL_TEXT_PROVIDER_VERSION_SHA256 = "c" * 64
VISUAL_TABLE_PROVIDER_VERSION_SHA256 = "5" * 64
REVIEWER_ONE = "d" * 64
REVIEWER_TWO = "e" * 64
POLICY_VERSION = "pdf-body-review-policy.v1"


class PdfBodyReviewArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native_profile = _profile("NATIVE")
        self.native_draft = prepare_pdf_body_review_draft(self.native_profile)
        self.native_scope = _binding(self.native_profile)
        self.native_approval = _approval(self.native_draft)

    def assert_contract_error(self, code: str):
        return self.assertRaisesRegex(
            PdfBodyReviewContractError,
            rf"^{re.escape(code)}$",
        )

    def finalize(
        self,
        *,
        draft=None,
        approval=None,
        scope=None,
        trusted_reviewer_ids=None,
        minimum_human_reviewers: int = 1,
    ):
        return finalize_pdf_body_review_artifacts(
            draft=self.native_draft if draft is None else draft,
            approval=self.native_approval if approval is None else approval,
            expected_scope=self.native_scope if scope is None else scope,
            trusted_reviewer_ids={REVIEWER_ONE}
            if trusted_reviewer_ids is None
            else trusted_reviewer_ids,
            expected_review_policy_version=POLICY_VERSION,
            minimum_human_reviewers=minimum_human_reviewers,
        )

    def test_schemas_accept_valid_native_and_remote_artifacts(self) -> None:
        schema_root = (
            Path(__file__).resolve().parents[1] / "eval" / "pdf_body_review"
        )
        remote_draft = prepare_pdf_body_review_draft(_profile("REMOTE_VISUAL"))
        remote_approval = _approval(remote_draft)
        for mode, schema_name, artifact in (
            ("native", "draft.schema.json", self.native_draft),
            ("native", "approval.schema.json", self.native_approval),
            ("remote", "draft.schema.json", remote_draft),
            ("remote", "approval.schema.json", remote_approval),
        ):
            with self.subTest(mode=mode, schema_name=schema_name):
                schema = json.loads(
                    (schema_root / schema_name).read_text(encoding="utf-8")
                )
                jsonschema.Draft202012Validator.check_schema(schema)
                jsonschema.Draft202012Validator(
                    schema,
                    format_checker=jsonschema.FormatChecker(),
                ).validate(artifact)

    def test_prepare_is_detached_draft_and_cannot_claim_ready_or_gold(self) -> None:
        candidate = copy.deepcopy(self.native_profile)
        draft = prepare_pdf_body_review_draft(candidate)

        self.assertEqual(draft["schema_version"], DRAFT_SCHEMA_VERSION)
        self.assertEqual(draft["status"], "DRAFT")
        self.assertEqual(draft["location_class"], CONTROLLED_EXTERNAL)
        self.assertIs(draft["is_human_gold"], False)
        self.assertNotIn("review_approved", draft)
        self.assertNotIn("reviews", draft)
        self.assertEqual(
            draft["profile_sha256"], canonical_json_sha256(draft["profile"])
        )

        candidate["pages"][0]["expected_body_line_count"] = 999
        self.assertNotEqual(
            draft["profile"]["pages"][0]["expected_body_line_count"],
            999,
        )

        ready_like_candidate = copy.deepcopy(self.native_profile)
        ready_like_candidate["status"] = "READY"
        with self.assert_contract_error("profile_unknown_field:status"):
            prepare_pdf_body_review_draft(ready_like_candidate)

        forged_draft = copy.deepcopy(draft)
        forged_draft["status"] = "APPROVED"
        forged_draft["is_human_gold"] = True
        with self.assert_contract_error("draft_status_must_be_draft"):
            validate_pdf_body_review_draft(forged_draft)

    def test_valid_native_approval_materializes_existing_ready_shape(self) -> None:
        ready = self.finalize()

        self.assertEqual(
            set(ready),
            {
                "status",
                "source_sha256",
                "body_page_start",
                "body_page_end",
                "review_mode",
                "review_approved",
                "review_artifact_sha256",
                "pages",
            },
        )
        self.assertEqual(ready["status"], "READY")
        self.assertIs(ready["review_approved"], True)
        self.assertEqual(
            ready["review_artifact_sha256"],
            canonical_json_sha256(self.native_approval),
        )
        self.assertNotIn(
            REVIEWER_ONE,
            json.dumps(ready, ensure_ascii=False, sort_keys=True),
        )
        for page in ready["pages"]:
            self.assertNotIn("image_occurrences", page)
            self.assertNotIn("image_content_multiset_sha256", page)
            self.assertNotIn("image_sequence_sha256", page)
            self.assertNotIn("table_occurrences", page)
            self.assertNotIn("table_content_multiset_sha256", page)
            self.assertNotIn("table_sequence_sha256", page)
        _validate_existing_ready_profile_schema(ready)

    def test_valid_remote_approval_binds_provider_versions_and_page_images(self) -> None:
        profile = _profile("REMOTE_VISUAL")
        draft = prepare_pdf_body_review_draft(profile)
        scope = _binding(profile)
        approval = _approval(draft)

        ready = finalize_pdf_body_review_artifacts(
            draft=draft,
            approval=approval,
            expected_scope=scope,
            trusted_reviewer_ids={REVIEWER_ONE},
            expected_review_policy_version=POLICY_VERSION,
        )

        self.assertEqual(ready["review_mode"], "REMOTE_VISUAL")
        self.assertEqual(ready["approved_visual_provider"], "bailian")
        self.assertNotIn("visual_provider_id", ready)
        self.assertNotIn("visual_text_provider_version_sha256", ready)
        self.assertNotIn("visual_table_provider_version_sha256", ready)
        self.assertTrue(
            all("page_image_sha256" in page for page in ready["pages"])
        )
        _validate_existing_ready_profile_schema(ready)

    def test_canonical_hash_ignores_object_key_order_but_not_page_order(self) -> None:
        reversed_keys = dict(reversed(list(self.native_profile.items())))
        self.assertEqual(
            canonical_json_sha256(self.native_profile),
            canonical_json_sha256(reversed_keys),
        )

        reordered_pages = copy.deepcopy(self.native_profile)
        reordered_pages["pages"].reverse()
        self.assertNotEqual(
            canonical_json_sha256(self.native_profile),
            canonical_json_sha256(reordered_pages),
        )
        with self.assert_contract_error("profile_pages_not_complete_and_ordered"):
            prepare_pdf_body_review_draft(reordered_pages)

    def test_arbitrary_hashes_cannot_replace_real_draft_and_profile_bindings(self) -> None:
        approval = copy.deepcopy(self.native_approval)
        approval["draft_sha256"] = "f" * 64
        with self.assert_contract_error("approval_draft_sha256_mismatch"):
            self.finalize(approval=approval)

        approval = copy.deepcopy(self.native_approval)
        approval["profile_sha256"] = "f" * 64
        with self.assert_contract_error("approval_profile_sha256_mismatch"):
            self.finalize(approval=approval)

    def test_profile_and_draft_mutation_after_approval_fail_closed(self) -> None:
        draft = copy.deepcopy(self.native_draft)
        draft["profile"]["pages"][0]["expected_body_line_count"] += 1
        with self.assert_contract_error("draft_profile_sha256_mismatch"):
            self.finalize(draft=draft)

        draft["profile_sha256"] = canonical_json_sha256(draft["profile"])
        with self.assert_contract_error("approval_draft_sha256_mismatch"):
            self.finalize(draft=draft)

    def test_document_source_scope_assertion_and_evaluator_replay_fail(self) -> None:
        mutations = {
            "document": ("document_id", "DOC-OTHER"),
            "source": ("source_sha256", "f" * 64),
            "page_start": ("body_page_start", 1),
            "assertion": ("assertion_id", "FMT-006-A1"),
            "evaluator": ("evaluator_id", "source_ir.other/v1"),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label):
                scope = copy.deepcopy(self.native_scope)
                scope[field] = value
                with self.assertRaises(PdfBodyReviewContractError):
                    self.finalize(scope=scope)

    def test_remote_provider_alias_id_versions_and_page_hash_drift_fail(self) -> None:
        base_profile = _profile("REMOTE_VISUAL")
        variants = []

        missing_text_version = copy.deepcopy(base_profile)
        missing_text_version.pop("visual_text_provider_version_sha256")
        variants.append(("missing-text-version", missing_text_version))

        missing_table_version = copy.deepcopy(base_profile)
        missing_table_version.pop("visual_table_provider_version_sha256")
        variants.append(("missing-table-version", missing_table_version))

        wrong_provider_id = copy.deepcopy(base_profile)
        wrong_provider_id["visual_provider_id"] = "azure-document-intelligence"
        variants.append(("wrong-provider-id", wrong_provider_id))

        missing_page_hash = copy.deepcopy(base_profile)
        missing_page_hash["pages"][0].pop("page_image_sha256")
        variants.append(("missing-page-hash", missing_page_hash))

        for label, profile in variants:
            with self.subTest(label=label):
                with self.assertRaises(PdfBodyReviewContractError):
                    prepare_pdf_body_review_draft(profile)

        draft = prepare_pdf_body_review_draft(base_profile)
        approval = _approval(draft)
        for version_field in (
            "visual_text_provider_version_sha256",
            "visual_table_provider_version_sha256",
        ):
            with self.subTest(version_field=version_field):
                scope = _binding(base_profile)
                scope[version_field] = "f" * 64
                with self.assert_contract_error("draft_scope_mismatch"):
                    finalize_pdf_body_review_artifacts(
                        draft=draft,
                        approval=approval,
                        expected_scope=scope,
                        trusted_reviewer_ids={REVIEWER_ONE},
                        expected_review_policy_version=POLICY_VERSION,
                    )

    def test_native_profile_forbids_remote_provenance_and_page_hash(self) -> None:
        with_provider = copy.deepcopy(self.native_profile)
        with_provider.update(
            {
                "approved_visual_provider": "bailian",
                "visual_provider_id": "aliyun-bailian-qwen-ocr",
                "visual_text_provider_version_sha256": (
                    VISUAL_TEXT_PROVIDER_VERSION_SHA256
                ),
                "visual_table_provider_version_sha256": (
                    VISUAL_TABLE_PROVIDER_VERSION_SHA256
                ),
            }
        )
        with self.assert_contract_error("native_profile_forbids_visual_provider"):
            prepare_pdf_body_review_draft(with_provider)

        with_page_hash = copy.deepcopy(self.native_profile)
        with_page_hash["pages"][0]["page_image_sha256"] = PAGE_IMAGE_SHA256
        with self.assert_contract_error("native_profile_forbids_page_image_sha256"):
            prepare_pdf_body_review_draft(with_page_hash)

    def test_page_profile_requires_complete_ordered_range_and_consistent_counts(self) -> None:
        missing_page = copy.deepcopy(self.native_profile)
        missing_page["pages"].pop()
        with self.assert_contract_error("profile_pages_not_complete_and_ordered"):
            prepare_pdf_body_review_draft(missing_page)

        inconsistent_table = copy.deepcopy(self.native_profile)
        inconsistent_table["pages"][0]["expected_table_count"] = 0
        with self.assert_contract_error(
            "profile_page_0_expected_table_count_mismatch"
        ):
            prepare_pdf_body_review_draft(inconsistent_table)

        boolean_count = copy.deepcopy(self.native_profile)
        boolean_count["pages"][0]["expected_image_count"] = False
        with self.assert_contract_error(
            "profile_page_0_expected_image_count_invalid"
        ):
            prepare_pdf_body_review_draft(boolean_count)

        plaintext_field = copy.deepcopy(self.native_profile)
        plaintext_field["pages"][0]["raw_text"] = "must not enter artifacts"
        with self.assert_contract_error("profile_page_0_unknown_field:raw_text"):
            prepare_pdf_body_review_draft(plaintext_field)

        table_plaintext = copy.deepcopy(self.native_profile)
        table_plaintext["pages"][0]["table_occurrences"][0][
            "text"
        ] = "must not enter artifacts"
        with self.assert_contract_error(
            "profile_page_0_table_occurrence_0_unknown_field:text"
        ):
            prepare_pdf_body_review_draft(table_plaintext)

    def test_image_summary_is_derived_only_from_content_occurrences(self) -> None:
        page = self.native_profile["pages"][0]
        content_occurrence = page["image_occurrences"][0]

        self.assertEqual(
            pdf_content_image_projection(page["image_occurrences"]),
            [
                {
                    "node_kind": content_occurrence["node_kind"],
                    "asset_kind": content_occurrence["asset_kind"],
                    "media_type": content_occurrence["media_type"],
                    "content_sha256": content_occurrence["content_sha256"],
                }
            ],
        )
        self.assertEqual(
            summarize_pdf_content_image_occurrences(page["image_occurrences"]),
            {
                "expected_image_count": page["expected_image_count"],
                "image_content_multiset_sha256": page[
                    "image_content_multiset_sha256"
                ],
                "image_sequence_sha256": page["image_sequence_sha256"],
            },
        )

        stale_count = copy.deepcopy(self.native_profile)
        stale_count["pages"][0]["image_occurrences"][1][
            "disposition"
        ] = "CONTENT"
        with self.assert_contract_error(
            "profile_page_0_expected_image_count_mismatch"
        ):
            prepare_pdf_body_review_draft(stale_count)

        stale_multiset = copy.deepcopy(self.native_profile)
        stale_multiset["pages"][0]["image_content_multiset_sha256"] = "f" * 64
        with self.assert_contract_error(
            "profile_page_0_image_content_multiset_mismatch"
        ):
            prepare_pdf_body_review_draft(stale_multiset)

        stale_sequence = copy.deepcopy(self.native_profile)
        stale_sequence["pages"][0]["image_sequence_sha256"] = "f" * 64
        with self.assert_contract_error("profile_page_0_image_sequence_mismatch"):
            prepare_pdf_body_review_draft(stale_sequence)

    def test_table_summary_is_derived_only_from_content_occurrences(self) -> None:
        page = self.native_profile["pages"][0]
        content_occurrence = page["table_occurrences"][0]

        self.assertEqual(
            pdf_content_table_projection(page["table_occurrences"]),
            [
                {
                    "row_count": content_occurrence["row_count"],
                    "column_count": content_occurrence["column_count"],
                    "cell_count": content_occurrence["cell_count"],
                    "merged_region_count": content_occurrence[
                        "merged_region_count"
                    ],
                    "topology_sha256": content_occurrence["topology_sha256"],
                }
            ],
        )
        self.assertEqual(
            summarize_pdf_content_table_occurrences(page["table_occurrences"]),
            {
                "expected_table_count": page["expected_table_count"],
                "expected_table_row_count": page["expected_table_row_count"],
                "expected_table_cell_count": page["expected_table_cell_count"],
                "expected_merged_region_count": page[
                    "expected_merged_region_count"
                ],
                "table_content_multiset_sha256": page[
                    "table_content_multiset_sha256"
                ],
                "table_sequence_sha256": page["table_sequence_sha256"],
            },
        )

        stale_count = copy.deepcopy(self.native_profile)
        stale_count["pages"][0]["table_occurrences"][1][
            "disposition"
        ] = "CONTENT"
        with self.assert_contract_error(
            "profile_page_0_expected_table_count_mismatch"
        ):
            prepare_pdf_body_review_draft(stale_count)

        for field, error_code in (
            (
                "expected_table_row_count",
                "profile_page_0_expected_table_row_count_mismatch",
            ),
            (
                "expected_table_cell_count",
                "profile_page_0_expected_table_cell_count_mismatch",
            ),
            (
                "expected_merged_region_count",
                "profile_page_0_expected_merged_region_count_mismatch",
            ),
        ):
            with self.subTest(field=field):
                stale_summary = copy.deepcopy(self.native_profile)
                stale_summary["pages"][0][field] += 1
                with self.assert_contract_error(error_code):
                    prepare_pdf_body_review_draft(stale_summary)

        for field, error_code in (
            (
                "table_content_multiset_sha256",
                "profile_page_0_table_content_multiset_mismatch",
            ),
            (
                "table_sequence_sha256",
                "profile_page_0_table_sequence_mismatch",
            ),
        ):
            with self.subTest(field=field):
                stale_digest = copy.deepcopy(self.native_profile)
                stale_digest["pages"][0][field] = "0" * 64
                with self.assert_contract_error(error_code):
                    prepare_pdf_body_review_draft(stale_digest)

    def test_table_unresolved_is_allowed_in_draft_but_rejected_by_approval(
        self,
    ) -> None:
        profile = copy.deepcopy(self.native_profile)
        page = profile["pages"][0]
        occurrence = page["table_occurrences"][0]
        original_occurrence_sha256 = occurrence["occurrence_sha256"]
        occurrence["disposition"] = "UNRESOLVED"
        _refresh_table_summary(page)

        draft = prepare_pdf_body_review_draft(profile)
        self.assertEqual(
            draft["profile"]["pages"][0]["table_occurrences"][0][
                "occurrence_sha256"
            ],
            original_occurrence_sha256,
        )
        approval = _approval(draft)
        with self.assert_contract_error(
            "approval_contains_unresolved_table_occurrence"
        ):
            finalize_pdf_body_review_artifacts(
                draft=draft,
                approval=approval,
                expected_scope=_binding(profile),
                trusted_reviewer_ids={REVIEWER_ONE},
                expected_review_policy_version=POLICY_VERSION,
            )

    def test_prepare_allows_unresolved_but_approval_and_finalize_fail_closed(
        self,
    ) -> None:
        profile = copy.deepcopy(self.native_profile)
        page = profile["pages"][0]
        page["image_occurrences"][0]["disposition"] = "UNRESOLVED"
        _refresh_image_summary(page)

        draft = prepare_pdf_body_review_draft(profile)
        self.assertEqual(
            draft["profile"]["pages"][0]["image_occurrences"][0][
                "disposition"
            ],
            "UNRESOLVED",
        )
        approval = _approval(draft)
        with self.assert_contract_error(
            "approval_contains_unresolved_image_occurrence"
        ):
            finalize_pdf_body_review_artifacts(
                draft=draft,
                approval=approval,
                expected_scope=_binding(profile),
                trusted_reviewer_ids={REVIEWER_ONE},
                expected_review_policy_version=POLICY_VERSION,
            )

    def test_occurrence_identity_binds_bbox_content_and_derivation(self) -> None:
        mutations = {
            "bbox": lambda occurrence: occurrence["native_locator"]["bbox"].__setitem__(
                0, 11.0
            ),
            "content": lambda occurrence: occurrence.__setitem__(
                "content_sha256", "f" * 64
            ),
            "derivation": lambda occurrence: occurrence["derivation"].__setitem__(
                "mask_content_sha256", "f" * 64
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                profile = copy.deepcopy(self.native_profile)
                mutate(profile["pages"][0]["image_occurrences"][0])
                with self.assert_contract_error(
                    "profile_page_0_image_occurrence_0_occurrence_sha256_mismatch"
                ):
                    prepare_pdf_body_review_draft(profile)

    def test_table_identity_binds_provider_page_bbox_counts_and_topology(
        self,
    ) -> None:
        identity_mismatch = (
            "profile_page_0_table_occurrence_0_occurrence_sha256_mismatch"
        )
        mutations = {
            "provider-object": (
                lambda occurrence: occurrence.__setitem__(
                    "provider_object_id_sha256", "0" * 64
                ),
                identity_mismatch,
            ),
            "provider-index": (
                lambda occurrence: occurrence.__setitem__("provider_index", 8),
                identity_mismatch,
            ),
            "page": (
                lambda occurrence: occurrence.__setitem__("page_number", 3),
                "profile_page_0_table_occurrence_0_page_number_mismatch",
            ),
            "bbox": (
                lambda occurrence: occurrence["bbox"].__setitem__(0, 21.0),
                identity_mismatch,
            ),
            "rows": (
                lambda occurrence: occurrence.__setitem__("row_count", 3),
                identity_mismatch,
            ),
            "columns": (
                lambda occurrence: occurrence.__setitem__("column_count", 3),
                identity_mismatch,
            ),
            "cells": (
                lambda occurrence: occurrence.__setitem__("cell_count", 4),
                identity_mismatch,
            ),
            "merges": (
                lambda occurrence: occurrence.__setitem__(
                    "merged_region_count", 0
                ),
                identity_mismatch,
            ),
            "topology": (
                lambda occurrence: occurrence.__setitem__(
                    "topology_sha256", "0" * 64
                ),
                identity_mismatch,
            ),
        }
        for label, (mutate, error_code) in mutations.items():
            with self.subTest(label=label):
                profile = copy.deepcopy(self.native_profile)
                mutate(profile["pages"][0]["table_occurrences"][0])
                with self.assert_contract_error(error_code):
                    prepare_pdf_body_review_draft(profile)

    def test_table_inventory_rejects_reordering_and_duplicates(self) -> None:
        reordered = copy.deepcopy(self.native_profile)
        reordered["pages"][0]["table_occurrences"].reverse()
        with self.assert_contract_error(
            "profile_page_0_table_occurrences_not_ordered"
        ):
            prepare_pdf_body_review_draft(reordered)

        duplicate_provider_object = copy.deepcopy(self.native_profile)
        duplicate = duplicate_provider_object["pages"][0]["table_occurrences"][1]
        duplicate["provider_object_id_sha256"] = duplicate_provider_object[
            "pages"
        ][0]["table_occurrences"][0]["provider_object_id_sha256"]
        duplicate["occurrence_sha256"] = _table_occurrence_identity_sha256(
            duplicate
        )
        with self.assert_contract_error(
            "profile_page_0_duplicate_table_provider_object"
        ):
            prepare_pdf_body_review_draft(duplicate_provider_object)

        duplicate_locator = copy.deepcopy(self.native_profile)
        duplicate = duplicate_locator["pages"][0]["table_occurrences"][1]
        duplicate["bbox"] = copy.deepcopy(
            duplicate_locator["pages"][0]["table_occurrences"][0]["bbox"]
        )
        duplicate["occurrence_sha256"] = _table_occurrence_identity_sha256(
            duplicate
        )
        with self.assert_contract_error(
            "profile_page_0_duplicate_table_locator"
        ):
            prepare_pdf_body_review_draft(duplicate_locator)

    def test_rehashed_position_change_invalidates_prior_approval(self) -> None:
        profile = copy.deepcopy(self.native_profile)
        occurrence = profile["pages"][0]["image_occurrences"][0]
        occurrence["native_locator"]["bbox"][0] = 11.0
        occurrence["occurrence_sha256"] = _occurrence_identity_sha256(occurrence)
        moved_draft = prepare_pdf_body_review_draft(profile)

        with self.assert_contract_error("approval_draft_sha256_mismatch"):
            self.finalize(draft=moved_draft, approval=self.native_approval)

    def test_approved_inventory_rejects_omission_reordering_and_duplicates(
        self,
    ) -> None:
        omitted_from_approved_draft = copy.deepcopy(self.native_draft)
        omitted_from_approved_draft["profile"]["pages"][0][
            "image_occurrences"
        ].pop()
        with self.assert_contract_error("draft_profile_sha256_mismatch"):
            self.finalize(draft=omitted_from_approved_draft)

        table_omitted_from_approved_draft = copy.deepcopy(self.native_draft)
        table_omitted_from_approved_draft["profile"]["pages"][0][
            "table_occurrences"
        ].pop()
        with self.assert_contract_error("draft_profile_sha256_mismatch"):
            self.finalize(draft=table_omitted_from_approved_draft)

        reordered = copy.deepcopy(self.native_profile)
        reordered["pages"][0]["image_occurrences"].reverse()
        with self.assert_contract_error(
            "profile_page_0_image_occurrences_not_ordered"
        ):
            prepare_pdf_body_review_draft(reordered)

        duplicate_locator = copy.deepcopy(self.native_profile)
        duplicate = copy.deepcopy(
            duplicate_locator["pages"][0]["image_occurrences"][1]
        )
        duplicate["source_order"] += 1
        duplicate["occurrence_sha256"] = _occurrence_identity_sha256(duplicate)
        duplicate_locator["pages"][0]["image_occurrences"].append(duplicate)
        with self.assert_contract_error(
            "profile_page_0_duplicate_image_locator"
        ):
            prepare_pdf_body_review_draft(duplicate_locator)

    def test_every_reviewer_must_approve_all_three_channels_on_every_page(self) -> None:
        missing_page = copy.deepcopy(self.native_approval)
        missing_page["reviews"][0]["page_approvals"].pop()
        with self.assert_contract_error("approval_pages_not_complete_and_ordered"):
            self.finalize(approval=missing_page)

        reordered_pages = copy.deepcopy(self.native_approval)
        reordered_pages["reviews"][0]["page_approvals"].reverse()
        with self.assert_contract_error("approval_pages_not_complete_and_ordered"):
            self.finalize(approval=reordered_pages)

        pending_channel = copy.deepcopy(self.native_approval)
        pending_channel["reviews"][0]["page_approvals"][0]["channels"][
            "table"
        ] = "PENDING"
        with self.assert_contract_error("approval_page_channel_not_approved"):
            self.finalize(approval=pending_channel)

        missing_channel = copy.deepcopy(self.native_approval)
        missing_channel["reviews"][0]["page_approvals"][0]["channels"].pop(
            "image"
        )
        with self.assert_contract_error(
            "approval_review_0_channels_missing_field:image"
        ):
            self.finalize(approval=missing_channel)

    def test_human_provenance_and_external_trust_are_fail_closed(self) -> None:
        with self.assert_contract_error("trusted_reviewer_registry_empty"):
            self.finalize(trusted_reviewer_ids=set())

        with self.assert_contract_error("approval_reviewer_not_trusted"):
            self.finalize(trusted_reviewer_ids={REVIEWER_TWO})

        mutations = {
            "actor": ("actor_type", "AI"),
            "role": ("reviewer_role", "DEVELOPER"),
            "independence": ("independence_attested", False),
            "timestamp": ("reviewed_at_utc", "2026-07-23T01:02:03+08:00"),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label):
                approval = copy.deepcopy(self.native_approval)
                approval["reviews"][0][field] = value
                with self.assertRaises(PdfBodyReviewContractError):
                    self.finalize(approval=approval)

        date_only = copy.deepcopy(self.native_approval)
        date_only["reviews"][0]["reviewed_at_utc"] = "2026-07-22Z"
        with self.assert_contract_error("approval_review_0_reviewed_at_invalid"):
            self.finalize(approval=date_only)

        freeform_note = copy.deepcopy(self.native_approval)
        freeform_note["reviews"][0]["notes"] = "not allowed"
        with self.assert_contract_error("approval_review_0_unknown_field:notes"):
            self.finalize(approval=freeform_note)

    def test_distinct_reviewers_and_external_minimum_are_enforced(self) -> None:
        with self.assert_contract_error("approval_insufficient_human_reviewers"):
            self.finalize(
                trusted_reviewer_ids={REVIEWER_ONE, REVIEWER_TWO},
                minimum_human_reviewers=2,
            )

        duplicate = copy.deepcopy(self.native_approval)
        duplicate["reviews"].append(copy.deepcopy(duplicate["reviews"][0]))
        with self.assert_contract_error("approval_duplicate_reviewer"):
            self.finalize(
                approval=duplicate,
                trusted_reviewer_ids={REVIEWER_ONE},
                minimum_human_reviewers=2,
            )

        dual = copy.deepcopy(self.native_approval)
        second = copy.deepcopy(dual["reviews"][0])
        second["reviewer_id"] = REVIEWER_TWO
        second["reviewed_at_utc"] = "2026-07-23T02:03:04Z"
        dual["reviews"].append(second)
        validated = validate_pdf_body_review_approval(
            draft=self.native_draft,
            approval=dual,
            expected_scope=self.native_scope,
            trusted_reviewer_ids={REVIEWER_ONE, REVIEWER_TWO},
            expected_review_policy_version=POLICY_VERSION,
            minimum_human_reviewers=2,
        )
        self.assertEqual(len(validated["reviews"]), 2)

    def test_policy_and_review_tool_provenance_are_required(self) -> None:
        with self.assert_contract_error("approval_review_policy_version_mismatch"):
            validate_pdf_body_review_approval(
                draft=self.native_draft,
                approval=self.native_approval,
                expected_scope=self.native_scope,
                trusted_reviewer_ids={REVIEWER_ONE},
                expected_review_policy_version="pdf-body-review-policy.v2",
            )

        approval = copy.deepcopy(self.native_approval)
        approval["reviews"][0]["review_tool"].pop("tool_version")
        with self.assert_contract_error(
            "approval_review_0_review_tool_missing_field:tool_version"
        ):
            self.finalize(approval=approval)

    def test_validation_and_finalization_do_not_mutate_inputs(self) -> None:
        draft = copy.deepcopy(self.native_draft)
        approval = copy.deepcopy(self.native_approval)
        scope = copy.deepcopy(self.native_scope)
        originals = copy.deepcopy((draft, approval, scope))

        self.finalize(draft=draft, approval=approval, scope=scope)

        self.assertEqual((draft, approval, scope), originals)


def _profile(review_mode: str) -> dict:
    pages = [_page(2), _page(3)]
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "assertion_id": "FMT-005-A4",
        "evaluator_id": PDF_BODY_REVIEW_EVALUATOR_ID,
        "document_id": "DOC-SYNTHETIC-PDF",
        "source_sha256": SOURCE_SHA256,
        "body_page_start": 2,
        "body_page_end": 3,
        "review_mode": review_mode,
        "pages": pages,
    }
    if review_mode == "REMOTE_VISUAL":
        profile.update(
            {
                "approved_visual_provider": "bailian",
                "visual_provider_id": "aliyun-bailian-qwen-ocr",
                "visual_text_provider_version_sha256": (
                    VISUAL_TEXT_PROVIDER_VERSION_SHA256
                ),
                "visual_table_provider_version_sha256": (
                    VISUAL_TABLE_PROVIDER_VERSION_SHA256
                ),
            }
        )
        for page in pages:
            page["page_image_sha256"] = PAGE_IMAGE_SHA256
    return profile


def _page(page_number: int) -> dict:
    image_occurrences = [
        _image_occurrence(
            page_number=page_number,
            block_index=4,
            source_order=page_number * 100 + 4,
            content_sha256=("6" if page_number == 2 else "8") * 64,
            disposition="CONTENT",
            derivation=(
                {
                    "kind": "alpha_mask_composite",
                    "base_content_sha256": "1" * 64,
                    "mask_content_sha256": "2" * 64,
                }
                if page_number == 2
                else None
            ),
        ),
        _image_occurrence(
            page_number=page_number,
            block_index=9,
            source_order=page_number * 100 + 9,
            content_sha256=("7" if page_number == 2 else "9") * 64,
            disposition="NON_CONTENT",
        ),
    ]
    image_summary = summarize_pdf_content_image_occurrences(image_occurrences)
    table_occurrences = [
        _table_occurrence(
            page_number=page_number,
            provider_index=0,
            provider_object_id_sha256=("a" if page_number == 2 else "b") * 64,
            topology_sha256=("c" if page_number == 2 else "d") * 64,
            row_count=2,
            column_count=2,
            cell_count=3,
            merged_region_count=1,
            disposition="CONTENT",
        ),
        _table_occurrence(
            page_number=page_number,
            provider_index=1,
            provider_object_id_sha256=("e" if page_number == 2 else "f") * 64,
            topology_sha256=("1" if page_number == 2 else "2") * 64,
            row_count=3,
            column_count=2,
            cell_count=6,
            merged_region_count=0,
            disposition="NON_CONTENT",
        ),
    ]
    table_summary = summarize_pdf_content_table_occurrences(table_occurrences)
    return {
        "page_number": page_number,
        "expected_body_line_count": 5,
        **image_summary,
        "image_occurrences": image_occurrences,
        **table_summary,
        "table_occurrences": table_occurrences,
        "content_multiset_sha256": str(page_number) * 64,
        "sequence_sha256": str(page_number + 2) * 64,
    }


def _image_occurrence(
    *,
    page_number: int,
    block_index: int | None,
    source_order: int,
    content_sha256: str,
    disposition: str,
    derivation: dict | None = None,
) -> dict:
    identity = {
        "source_order": source_order,
        "node_kind": "image",
        "asset_kind": "image",
        "media_type": "image/png",
        "content_sha256": content_sha256,
        "native_locator": {
            "format": "pdf",
            "page_number": page_number,
            "block_index": block_index,
            "bbox": [
                10.0,
                float(block_index or 0) + 20.0,
                110.0,
                float(block_index or 0) + 80.0,
            ],
        },
        "derivation": derivation,
    }
    return {
        "occurrence_sha256": canonical_pdf_image_occurrence_sha256(identity),
        **identity,
        "disposition": disposition,
    }


def _refresh_image_summary(page: dict) -> None:
    page.update(summarize_pdf_content_image_occurrences(page["image_occurrences"]))


def _table_occurrence(
    *,
    page_number: int,
    provider_index: int,
    provider_object_id_sha256: str,
    topology_sha256: str,
    row_count: int,
    column_count: int,
    cell_count: int,
    merged_region_count: int,
    disposition: str,
) -> dict:
    identity = {
        "provider_object_id_sha256": provider_object_id_sha256,
        "provider_index": provider_index,
        "page_number": page_number,
        "bbox": [
            20.0,
            40.0 + provider_index * 100.0,
            180.0,
            120.0 + provider_index * 100.0,
        ],
        "row_count": row_count,
        "column_count": column_count,
        "cell_count": cell_count,
        "merged_region_count": merged_region_count,
        "topology_sha256": topology_sha256,
    }
    return {
        "occurrence_sha256": canonical_pdf_table_occurrence_sha256(identity),
        **identity,
        "disposition": disposition,
    }


def _refresh_table_summary(page: dict) -> None:
    page.update(summarize_pdf_content_table_occurrences(page["table_occurrences"]))


def _occurrence_identity_sha256(occurrence: dict) -> str:
    identity_fields = (
        "source_order",
        "node_kind",
        "asset_kind",
        "media_type",
        "content_sha256",
        "native_locator",
        "derivation",
    )
    return canonical_pdf_image_occurrence_sha256(
        {field: occurrence[field] for field in identity_fields}
    )


def _table_occurrence_identity_sha256(occurrence: dict) -> str:
    identity_fields = (
        "provider_object_id_sha256",
        "provider_index",
        "page_number",
        "bbox",
        "row_count",
        "column_count",
        "cell_count",
        "merged_region_count",
        "topology_sha256",
    )
    return canonical_pdf_table_occurrence_sha256(
        {field: occurrence[field] for field in identity_fields}
    )


def _binding(profile: dict) -> dict:
    fields = (
        "assertion_id",
        "evaluator_id",
        "document_id",
        "source_sha256",
        "body_page_start",
        "body_page_end",
        "review_mode",
        "approved_visual_provider",
        "visual_provider_id",
        "visual_text_provider_version_sha256",
        "visual_table_provider_version_sha256",
    )
    return {field: profile[field] for field in fields if field in profile}


def _approval(draft: dict, reviewer_ids=(REVIEWER_ONE,)) -> dict:
    profile = draft["profile"]
    page_numbers = [page["page_number"] for page in profile["pages"]]
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
                "reviewer_id": reviewer_id,
                "reviewer_id_scheme": HMAC_ID_SCHEME,
                "actor_type": "HUMAN",
                "reviewer_role": HUMAN_REVIEWER_ROLE,
                "independence_attested": True,
                "reviewed_at_utc": "2026-07-23T01:02:03Z",
                "review_tool": {
                    "tool_id": "reqpilotx-pdf-body-review",
                    "tool_version": "1.0.0",
                },
                "page_approvals": [
                    {
                        "page_number": page_number,
                        "channels": {
                            "text": "APPROVED",
                            "image": "APPROVED",
                            "table": "APPROVED",
                        },
                    }
                    for page_number in page_numbers
                ],
            }
            for reviewer_id in reviewer_ids
        ],
    }


def _validate_existing_ready_profile_schema(ready_profile: dict) -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "eval"
        / "extraction_cases"
        / "schema.json"
    )
    manifest_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": manifest_schema["$defs"],
        "$ref": "#/$defs/pdf_reviewed_body_profile",
    }
    jsonschema.Draft202012Validator(wrapper).validate(ready_profile)


if __name__ == "__main__":
    unittest.main()
