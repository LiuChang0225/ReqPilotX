from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import jsonschema
import pymupdf
from PIL import Image

from scripts.pdf_body_review_artifacts import (
    PdfBodyReviewContractError,
    finalize_pdf_body_review_artifacts,
)
from scripts.extraction_case_runner import CorpusPathError
from scripts.pdf_visual_ai_pre_review import (
    LOCATION,
    SCHEMA_VERSION,
    STATUS,
    PdfVisualAiPreReviewContractError,
    canonical_artifact_sha256,
    prepare_pdf_visual_ai_pre_review,
    recompute_summary,
    validate_pdf_visual_ai_pre_review,
    write_pdf_visual_ai_pre_review,
)
from scripts.pdf_visual_review_bundle import export_pdf_visual_review_bundle


class PdfVisualAiPreReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.corpus_root = self.root / "corpus"
        self.corpus_root.mkdir()
        self.source = self.corpus_root / "source.pdf"
        document = pymupdf.open()
        for page_number in range(1, 4):
            page = document.new_page(width=420, height=595)
            page.insert_text(
                (48, 72),
                f"{page_number}. SYNTHETIC REVIEW PAGE",
                fontsize=14,
            )
        document.save(self.source)
        document.close()
        source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.bundle_dir = self.root / "review-bundle"
        self.bundle = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-AI-PRE-001",
            expected_source_sha256=source_sha256,
            page_start=1,
            page_end=3,
            output_dir=self.bundle_dir,
            corpus_root=self.corpus_root,
        )
        self.bundle_sha256 = self.bundle["bundle_sha256"]
        self.reviewer = _reviewer()
        self.page_reviews = _page_reviews()

    def assert_contract_error(self, code: str):
        return self.assertRaisesRegex(
            PdfVisualAiPreReviewContractError,
            rf"^{re.escape(code)}$",
        )

    def prepare(self):
        return prepare_pdf_visual_ai_pre_review(
            bundle_dir=self.bundle_dir,
            expected_review_bundle_sha256=self.bundle_sha256,
            reviewer=self.reviewer,
            page_reviews=self.page_reviews,
        )

    def validate(
        self,
        artifact: dict,
        *,
        expected_artifact_sha256: str | None = None,
        expected_review_bundle_sha256: str | None = None,
    ) -> dict:
        return validate_pdf_visual_ai_pre_review(
            artifact,
            bundle_dir=self.bundle_dir,
            expected_artifact_sha256=(
                expected_artifact_sha256 or artifact["artifact_sha256"]
            ),
            expected_review_bundle_sha256=(
                expected_review_bundle_sha256 or self.bundle_sha256
            ),
        )

    def test_happy_path_matches_schema_and_is_bundle_derived(self) -> None:
        artifact = self.prepare()

        self.assertEqual(artifact["schema_version"], SCHEMA_VERSION)
        self.assertEqual(artifact["status"], STATUS)
        self.assertEqual(artifact["location"], LOCATION)
        self.assertIs(artifact["is_human_gold"], False)
        self.assertIs(artifact["is_formal_approval"], False)
        self.assertEqual(artifact["review_bundle_sha256"], self.bundle_sha256)
        self.assertEqual(
            artifact["artifact_sha256"],
            canonical_artifact_sha256(artifact),
        )
        self.assertEqual(
            [page["page_number"] for page in artifact["pages"]],
            [1, 2, 3],
        )
        self.assertEqual(
            artifact["binding"]["document_id"],
            self.bundle["document"]["document_id"],
        )
        for artifact_page, bundle_page in zip(
            artifact["pages"],
            self.bundle["pages"],
            strict=True,
        ):
            self.assertEqual(
                artifact_page["artifact_sha256"],
                bundle_page["artifact"]["sha256"],
            )
            self.assertEqual(
                artifact_page["render_sha256"],
                bundle_page["render"]["sha256"],
            )
            self.assertEqual(
                artifact_page["annotated_sha256"],
                bundle_page["annotated"]["sha256"],
            )

        schema_path = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "pdf_visual_review"
            / "ai_pre_review.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(artifact)

        self.page_reviews[0]["decisions"]["text"] = "ISSUE"
        self.assertEqual(artifact["pages"][0]["decisions"]["text"], "PASS")

    def test_writer_is_external_atomic_and_never_overwrites(self) -> None:
        output = self.root / "controlled-review" / "ledger.json"
        artifact = write_pdf_visual_ai_pre_review(
            bundle_dir=self.bundle_dir,
            expected_review_bundle_sha256=self.bundle_sha256,
            reviewer=self.reviewer,
            page_reviews=self.page_reviews,
            output_path=output,
            corpus_root=self.corpus_root,
        )

        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            artifact,
        )
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assert_contract_error("artifact_output_exists"):
            write_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256=self.bundle_sha256,
                reviewer=self.reviewer,
                page_reviews=self.page_reviews,
                output_path=output,
                corpus_root=self.corpus_root,
            )
        with self.assertRaises(CorpusPathError):
            write_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256=self.bundle_sha256,
                reviewer=self.reviewer,
                page_reviews=self.page_reviews,
                output_path=self.corpus_root / "ledger.json",
                corpus_root=self.corpus_root,
            )

    def test_tamper_rejects_self_hash_external_pins_and_bundle_hashes(self) -> None:
        artifact = self.prepare()
        original_artifact_hash = artifact["artifact_sha256"]

        stale_hash = copy.deepcopy(artifact)
        stale_hash["pages"][0]["annotated_sha256"] = "f" * 64
        with self.assert_contract_error("artifact_sha256_mismatch"):
            self.validate(stale_hash)

        rebound_page = copy.deepcopy(stale_hash)
        rebound_page["artifact_sha256"] = canonical_artifact_sha256(rebound_page)
        with self.assert_contract_error("page_0_annotated_sha256_bundle_mismatch"):
            self.validate(rebound_page)
        with self.assert_contract_error("expected_artifact_sha256_mismatch"):
            self.validate(
                rebound_page,
                expected_artifact_sha256=original_artifact_hash,
            )

        rebound_bundle = copy.deepcopy(artifact)
        rebound_bundle["review_bundle_sha256"] = "5" * 64
        rebound_bundle["artifact_sha256"] = canonical_artifact_sha256(rebound_bundle)
        with self.assert_contract_error("expected_review_bundle_sha256_mismatch"):
            self.validate(rebound_bundle)

        with self.assert_contract_error(
            "review_bundle_rejected:bundle_expected_sha256_mismatch"
        ):
            prepare_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256="4" * 64,
                reviewer=self.reviewer,
                page_reviews=self.page_reviews,
            )

    def test_cannot_shrink_full_document_bundle_to_one_page(self) -> None:
        artifact = self.prepare()
        shrunk = copy.deepcopy(artifact)
        shrunk["binding"]["page_end"] = 1
        shrunk["pages"] = shrunk["pages"][:1]
        shrunk["summary"] = {
            "page_count": 1,
            "review_level_counts": {
                "FULL_PAGE": 1,
                "HIGH_RISK_DETAIL": 0,
            },
            "decision_counts": {
                dimension: {
                    "PASS": 1,
                    "ISSUE": 0,
                    "NOT_EVALUATED": 0,
                }
                for dimension in ("text", "image", "table", "layout")
            },
            "pages_with_findings": 0,
            "finding_count": 0,
        }
        shrunk["artifact_sha256"] = canonical_artifact_sha256(shrunk)

        with self.assert_contract_error("review_bundle_binding_mismatch"):
            self.validate(shrunk)

    def test_missing_duplicate_and_out_of_order_pages_are_rejected(self) -> None:
        artifact = self.prepare()

        missing = copy.deepcopy(artifact)
        missing["pages"].pop(1)
        with self.assert_contract_error("pages_missing_page"):
            self.validate(missing)

        duplicate = copy.deepcopy(artifact)
        duplicate["pages"][1]["page_number"] = 1
        with self.assert_contract_error("pages_duplicate_page_number"):
            self.validate(duplicate)

        out_of_order = copy.deepcopy(artifact)
        out_of_order["pages"][0], out_of_order["pages"][1] = (
            out_of_order["pages"][1],
            out_of_order["pages"][0],
        )
        with self.assert_contract_error("pages_out_of_order"):
            self.validate(out_of_order)

    def test_summary_drift_is_rejected_even_with_recomputed_artifact_hash(
        self,
    ) -> None:
        artifact = self.prepare()
        artifact["summary"]["finding_count"] += 1
        artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)

        with self.assert_contract_error("summary_drift"):
            self.validate(artifact)

    def test_findings_and_non_pass_decisions_must_agree(self) -> None:
        all_pass_with_finding = copy.deepcopy(self.page_reviews)
        all_pass_with_finding[0]["finding_codes"] = ["layout.clipped_footer"]
        with self.assert_contract_error("page_0_finding_without_non_pass"):
            prepare_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256=self.bundle_sha256,
                reviewer=self.reviewer,
                page_reviews=all_pass_with_finding,
            )

        issue_without_finding = copy.deepcopy(self.page_reviews)
        issue_without_finding[0]["decisions"]["layout"] = "ISSUE"
        with self.assert_contract_error("page_0_non_pass_requires_finding"):
            prepare_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256=self.bundle_sha256,
                reviewer=self.reviewer,
                page_reviews=issue_without_finding,
            )

    def test_ai_only_all_pass_is_never_gold_ready_or_formal_approval(self) -> None:
        artifact = self.prepare()

        self.assertEqual(artifact["status"], "AI_PRE_REVIEW")
        self.assertIs(artifact["is_human_gold"], False)
        self.assertIs(artifact["is_formal_approval"], False)
        self.assertNotIn("READY", json.dumps(artifact, sort_keys=True))
        self.assertNotIn("review_approved", artifact)

        human_reviewer = copy.deepcopy(self.reviewer)
        human_reviewer["actor_type"] = "HUMAN"
        with self.assert_contract_error("reviewer_actor_type_must_be_ai"):
            prepare_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256=self.bundle_sha256,
                reviewer=human_reviewer,
                page_reviews=self.page_reviews,
            )

        with self.assertRaises(PdfBodyReviewContractError):
            finalize_pdf_body_review_artifacts(
                draft=artifact,
                approval={},
                expected_scope={},
                trusted_reviewer_ids=set(),
                expected_review_policy_version="policy.v1",
            )

    def test_customer_text_and_unsafe_finding_codes_cannot_enter_artifact(
        self,
    ) -> None:
        customer_text = "客户原文：泵站压力必须达到七兆帕"
        page_with_note = copy.deepcopy(self.page_reviews)
        page_with_note[0][customer_text] = "must never reach stderr"
        with self.assert_contract_error("page_review_0_unknown_field") as raised:
            prepare_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256=self.bundle_sha256,
                reviewer=self.reviewer,
                page_reviews=page_with_note,
            )
        self.assertNotIn(customer_text, str(raised.exception))

        page_with_unsafe_code = copy.deepcopy(self.page_reviews)
        page_with_unsafe_code[0]["decisions"]["layout"] = "ISSUE"
        page_with_unsafe_code[0]["finding_codes"] = [customer_text]
        with self.assert_contract_error("page_0_finding_code_0_unsafe") as raised:
            prepare_pdf_visual_ai_pre_review(
                bundle_dir=self.bundle_dir,
                expected_review_bundle_sha256=self.bundle_sha256,
                reviewer=self.reviewer,
                page_reviews=page_with_unsafe_code,
            )
        self.assertNotIn(customer_text, str(raised.exception))

        artifact = self.prepare()
        serialized = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(customer_text, serialized)
        self.assertNotRegex(
            serialized,
            r'"(?:notes|excerpt|raw_text|ocr_text|customer_text)"\s*:',
        )

    def test_not_evaluated_decisions_are_recomputed_in_summary(self) -> None:
        self.page_reviews[0]["decisions"]["image"] = "NOT_EVALUATED"
        self.page_reviews[0]["finding_codes"] = ["image.remote_visual_required"]
        self.page_reviews[1]["decisions"]["table"] = "NOT_EVALUATED"
        self.page_reviews[1]["finding_codes"] = ["table.remote_visual_required"]
        self.page_reviews[2]["decisions"]["table"] = "NOT_EVALUATED"
        self.page_reviews[2]["finding_codes"] = ["table.remote_visual_required"]

        artifact = self.prepare()

        self.assertEqual(
            artifact["summary"]["decision_counts"]["image"],
            {"PASS": 2, "ISSUE": 0, "NOT_EVALUATED": 1},
        )
        self.assertEqual(
            artifact["summary"]["decision_counts"]["table"],
            {"PASS": 1, "ISSUE": 0, "NOT_EVALUATED": 2},
        )

    def test_flagged_fragmentation_page_requires_high_risk_detail(self) -> None:
        source = self.corpus_root / "fragmented-review.pdf"
        image_output = io.BytesIO()
        Image.new("RGB", (2, 2), "#2563eb").save(image_output, format="PNG")
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        for index in range(128):
            row, column = divmod(index, 16)
            x0 = 40.0 + column * 20.0
            y0 = 80.0 + row * 20.0
            page.insert_image(
                pymupdf.Rect(x0, y0, x0 + 5.0, y0 + 5.0),
                stream=image_output.getvalue(),
            )
        document.save(source)
        document.close()
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        bundle_dir = self.root / "fragmented-review-bundle"
        bundle = export_pdf_visual_review_bundle(
            source,
            document_id="DOC-AI-PRE-FRAGMENTED",
            expected_source_sha256=source_sha256,
            page_start=1,
            page_end=1,
            output_dir=bundle_dir,
            corpus_root=self.corpus_root,
        )
        self.assertIn(
            "EXCESSIVE_FIGURE_FRAGMENTATION",
            bundle["pages"][0]["risk_flags"],
        )
        page_reviews = [
            {
                "page_number": 1,
                "visited": True,
                "review_level": "FULL_PAGE",
                "decisions": {
                    "text": "PASS",
                    "image": "PASS",
                    "table": "PASS",
                    "layout": "PASS",
                },
                "finding_codes": [],
            }
        ]
        with self.assert_contract_error("page_review_0_high_risk_detail_required"):
            prepare_pdf_visual_ai_pre_review(
                bundle_dir=bundle_dir,
                expected_review_bundle_sha256=bundle["bundle_sha256"],
                reviewer=self.reviewer,
                page_reviews=page_reviews,
            )

        page_reviews[0]["review_level"] = "HIGH_RISK_DETAIL"
        artifact = prepare_pdf_visual_ai_pre_review(
            bundle_dir=bundle_dir,
            expected_review_bundle_sha256=bundle["bundle_sha256"],
            reviewer=self.reviewer,
            page_reviews=page_reviews,
        )
        artifact["pages"][0]["review_level"] = "FULL_PAGE"
        artifact["summary"] = recompute_summary(artifact["pages"])
        artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
        with self.assert_contract_error("page_0_high_risk_detail_required"):
            validate_pdf_visual_ai_pre_review(
                artifact,
                bundle_dir=bundle_dir,
                expected_artifact_sha256=artifact["artifact_sha256"],
                expected_review_bundle_sha256=bundle["bundle_sha256"],
            )


def _reviewer() -> dict:
    return {
        "actor_type": "AI",
        "model_id": "qwen3.7-plus",
        "model_version": "2026-05-26",
        "tool_id": "reqpilotx-ai-visual-review",
        "tool_version": "1.0.0",
        "reviewed_at_utc": "2026-07-26T03:04:05Z",
    }


def _page_reviews() -> list[dict]:
    reviews: list[dict] = []
    for page_number in range(1, 4):
        reviews.append(
            {
                "page_number": page_number,
                "visited": True,
                "review_level": (
                    "FULL_PAGE" if page_number != 2 else "HIGH_RISK_DETAIL"
                ),
                "decisions": {
                    "text": "PASS",
                    "image": "PASS",
                    "table": "PASS",
                    "layout": "PASS",
                },
                "finding_codes": [],
            }
        )
    return reviews


if __name__ == "__main__":
    unittest.main()
