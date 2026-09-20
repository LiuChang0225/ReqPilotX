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
from scripts.document_visual_ai_pre_review import (
    LOCATION,
    SCHEMA_VERSION,
    STATUS,
    DocumentVisualAiPreReviewContractError,
    canonical_artifact_sha256,
    prepare_document_visual_ai_pre_review,
    validate_document_visual_ai_pre_review,
)
from tests import test_document_visual_review_bundle as bundle_test_support


class DocumentVisualAiPreReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = bundle_test_support.DocumentVisualReviewBundleTests(
            methodName=(
                "test_four_formats_export_and_strictly_verify_full_document"
            )
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.inputs = self.fixture.make_inputs("docx")
        self.bundle_dir, self.bundle = self.fixture.export(
            self.inputs,
            destination_name="ledger-bundle",
        )
        self.reviewer = {
            "kind": "AI",
            "reviewer_id": "reqpilotx-ai-reviewer",
            "model_id": "gpt-visual-reviewer",
            "model_version": "2026-07-26",
            "reviewed_at": "2026-07-26T10:30:00+08:00",
        }
        self.page_reviews = [
            {
                "page_number": page_number,
                "verdict": "PASS",
                "issue_codes": [],
            }
            for page_number in range(1, 4)
        ]

    def assert_contract_error(self, code: str):
        return self.assertRaisesRegex(
            DocumentVisualAiPreReviewContractError,
            rf"^{re.escape(code)}$",
        )

    def prepare(self, *, page_reviews=None) -> dict:
        return prepare_document_visual_ai_pre_review(
            **self._bundle_arguments(),
            reviewer=self.reviewer,
            page_reviews=page_reviews or self.page_reviews,
        )

    def validate(
        self,
        artifact: dict,
        *,
        bundle_dir=None,
        expected_bundle_sha256=None,
        expected_inventory_sha256=None,
        expected_artifact_sha256=None,
    ) -> dict:
        arguments = self._bundle_arguments()
        arguments.update(
            {
                "bundle_dir": bundle_dir or self.bundle_dir,
                "expected_review_bundle_sha256": (
                    expected_bundle_sha256
                    or self.bundle["bundle_sha256"]
                ),
                "expected_artifact_inventory_sha256": (
                    expected_inventory_sha256
                    or self.bundle["artifact_inventory_sha256"]
                ),
                "expected_artifact_sha256": (
                    expected_artifact_sha256
                    or artifact["artifact_sha256"]
                ),
            }
        )
        return validate_document_visual_ai_pre_review(
            artifact,
            **arguments,
        )

    def _bundle_arguments(self) -> dict:
        return {
            "bundle_dir": self.bundle_dir,
            "source": self.inputs["source"],
            "document_format": self.inputs["format"],
            "expected_source_sha256": self.inputs["source_sha256"],
            "render_profile_path": self.inputs["render_profile_path"],
            "expected_render_profile_sha256": (
                self.inputs["render_profile_file_sha256"]
            ),
            "pipeline_profile_path": self.inputs["pipeline_profile_path"],
            "expected_pipeline_profile_sha256": (
                self.inputs["pipeline_profile_file_sha256"]
            ),
            "expected_review_bundle_sha256": (
                self.bundle["bundle_sha256"]
            ),
            "expected_artifact_inventory_sha256": (
                self.bundle["artifact_inventory_sha256"]
            ),
        }

    def test_happy_path_all_pages_pass_and_double_pin_bundle_inventory(
        self,
    ) -> None:
        artifact = self.prepare()

        self.assertEqual(artifact["schema_version"], SCHEMA_VERSION)
        self.assertEqual(artifact["status"], STATUS)
        self.assertEqual(artifact["location"], LOCATION)
        self.assertEqual(
            artifact["review_bundle_sha256"],
            self.bundle["bundle_sha256"],
        )
        self.assertEqual(
            artifact["artifact_inventory_sha256"],
            self.bundle["artifact_inventory_sha256"],
        )
        self.assertEqual(artifact["summary"]["verdict"], "PASS")
        self.assertEqual(artifact["summary"]["pass_count"], 3)
        self.assertEqual(
            artifact["artifact_sha256"],
            canonical_artifact_sha256(artifact),
        )
        self.assertEqual(self.validate(artifact), artifact)
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "document_visual_review"
            / "ai_pre_review.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(artifact)
        self.assertEqual(
            set(artifact["pages"][0]),
            {"page_number", "verdict", "issue_codes"},
        )
        self.assertFalse(artifact["privacy"]["contains_customer_text"])
        self.assertTrue(
            all(
                value is False
                for value in artifact["review_provenance"].values()
            )
        )

    def test_issue_or_not_evaluated_can_never_forge_summary_pass(self) -> None:
        page_reviews = copy.deepcopy(self.page_reviews)
        page_reviews[0] = {
            "page_number": 1,
            "verdict": "ISSUE",
            "issue_codes": ["SOURCE_RENDER_CLIPPED"],
        }
        page_reviews[1] = {
            "page_number": 2,
            "verdict": "NOT_EVALUATED",
            "issue_codes": ["REMOTE_VISUAL_REQUIRED"],
        }
        artifact = self.prepare(page_reviews=page_reviews)
        self.assertEqual(artifact["summary"]["verdict"], "ISSUE")

        forged = copy.deepcopy(artifact)
        forged["summary"]["verdict"] = "PASS"
        forged["artifact_sha256"] = canonical_artifact_sha256(forged)
        with self.assert_contract_error("summary_mismatch"):
            self.validate(forged)

    def test_missing_duplicate_and_out_of_order_pages_are_rejected(self) -> None:
        cases = (
            (
                self.page_reviews[:-1],
                "full_document_page_coverage_required",
            ),
            (
                [self.page_reviews[0], self.page_reviews[0], self.page_reviews[2]],
                "page_number_duplicate",
            ),
            (
                [self.page_reviews[1], self.page_reviews[0], self.page_reviews[2]],
                "pages_out_of_order",
            ),
        )
        for page_reviews, code in cases:
            with self.subTest(code=code), self.assert_contract_error(code):
                self.prepare(page_reviews=page_reviews)

    def test_external_bundle_and_inventory_pins_cannot_be_rebound(self) -> None:
        artifact = self.prepare()
        changed_bundle = copy.deepcopy(artifact)
        changed_bundle["review_bundle_sha256"] = "f" * 64
        changed_bundle["artifact_sha256"] = canonical_artifact_sha256(
            changed_bundle
        )
        with self.assert_contract_error(
            "review_bundle_sha256_external_pin_mismatch"
        ):
            self.validate(changed_bundle)

        changed_inventory = copy.deepcopy(artifact)
        changed_inventory["artifact_inventory_sha256"] = "e" * 64
        changed_inventory["artifact_sha256"] = canonical_artifact_sha256(
            changed_inventory
        )
        with self.assert_contract_error(
            "artifact_inventory_sha256_external_pin_mismatch"
        ):
            self.validate(changed_inventory)

    def test_bundle_artifact_drift_is_recomputed_before_ledger_acceptance(
        self,
    ) -> None:
        artifact = self.prepare()
        render = self.bundle_dir / "renders" / "page-0001.png"
        render.write_bytes(render.read_bytes() + b"tamper")

        with self.assert_contract_error(
            "review_bundle_invalid:bundle_inventory_0_hash_mismatch"
        ):
            self.validate(artifact)

    def test_provenance_tamper_and_customer_body_fields_are_rejected(
        self,
    ) -> None:
        artifact = self.prepare()
        provenance = copy.deepcopy(artifact)
        provenance["review_provenance"]["is_human_gold"] = True
        provenance["artifact_sha256"] = canonical_artifact_sha256(provenance)
        with self.assert_contract_error(
            "review_provenance_contract_mismatch"
        ):
            self.validate(provenance)

        customer_text = copy.deepcopy(artifact)
        customer_text["pages"][0]["notes"] = "customer body must not appear"
        customer_text["artifact_sha256"] = canonical_artifact_sha256(
            customer_text
        )
        with self.assert_contract_error("page_0_unknown_field:notes"):
            self.validate(customer_text)

    def test_issue_code_taxonomy_and_not_evaluated_semantics_are_frozen(
        self,
    ) -> None:
        unknown = copy.deepcopy(self.page_reviews)
        unknown[0] = {
            "page_number": 1,
            "verdict": "ISSUE",
            "issue_codes": ["ARBITRARY_REVIEW_NOTE"],
        }
        with self.assert_contract_error("page_0_issue_code_0_invalid"):
            self.prepare(page_reviews=unknown)

        not_run_as_issue = copy.deepcopy(self.page_reviews)
        not_run_as_issue[0] = {
            "page_number": 1,
            "verdict": "ISSUE",
            "issue_codes": ["PIPELINE_NOT_RUN"],
        }
        with self.assert_contract_error(
            "page_0_not_evaluated_code_requires_not_evaluated"
        ):
            self.prepare(page_reviews=not_run_as_issue)

        unreviewed_issue = copy.deepcopy(self.page_reviews)
        unreviewed_issue[0] = {
            "page_number": 1,
            "verdict": "NOT_EVALUATED",
            "issue_codes": ["PIPELINE_TEXT_OMISSION"],
        }
        with self.assert_contract_error(
            "page_0_issue_code_requires_issue"
        ):
            self.prepare(page_reviews=unreviewed_issue)
